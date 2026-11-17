#!/usr/bin/env python3
"""Explicitly authorized paired-audio comparison. All individual outputs stay private."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

from benchmark import collect_pairs, write_private_json
from clinical_terms import score_terms, summarize_terms
from metrics import summarize
from run_suite import input_digest


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def whisper(path, key):
    boundary = 'kinelo-' + uuid.uuid4().hex
    parts = []
    for name, value in {'model': 'whisper-1', 'language': 'ko', 'temperature': '0', 'response_format': 'json'}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(),
                  path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
    request = urllib.request.Request('https://api.openai.com/v1/audio/transcriptions', data=b''.join(parts),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'multipart/form-data; boundary=' + boundary})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=180) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise RuntimeError('API_HTTP_' + str(code)) from None
    except (OSError, ValueError):
        raise RuntimeError('API_REQUEST_FAILED') from None
    if not isinstance(result.get('text'), str):
        raise RuntimeError('INVALID_TRANSCRIPT')
    return result['text']


def main():
    parser = argparse.ArgumentParser()
    for name in ('data', 'annotations', 'out', 'expected-input-sha256'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--provider', choices=['whisper-1', 'qwen-service'], required=True)
    parser.add_argument('--external-audio-use-authorized', action='store_true')
    parser.add_argument('--service-source')
    parser.add_argument('--callers')
    parser.add_argument('--base-url', default='http://127.0.0.1:8112')
    args = parser.parse_args()
    os.umask(0o077)
    if Path(args.out).exists():
        parser.error('Private output already exists')
    if input_digest(args.data) != args.expected_input_sha256:
        parser.error('Input fingerprint mismatch')
    annotations_path = Path(args.annotations)
    annotations = json.loads(annotations_path.read_text())
    pairs = collect_pairs(args.data)
    if set(annotations) != {p[0].stem for p in pairs}:
        parser.error('Annotation/sample mismatch')
    if args.provider == 'whisper-1':
        key = os.environ.get('OPENAI_API_KEY', '')
        if not args.external_audio_use_authorized or not key:
            parser.error('External audio use authorization and an approved process key are required')
        transcribe = lambda path: whisper(path, key)
    else:
        if not args.service_source or not args.callers:
            parser.error('Existing service source and private caller file are required')
        sys.path.insert(0, args.service_source)
        from clients.python_client import SttClient
        callers = json.loads(Path(args.callers).read_text())
        client = SttClient(args.base_url, callers['physio_app_test'])
        transcribe = lambda path: client.transcribe(path.read_bytes(), timeout=600, poll_seconds=.1)['text']
    records, outcomes, scored = [], [], []
    started = time.monotonic()
    checked_at = datetime.now(timezone.utc).isoformat()
    for wav, group, reference, seconds in pairs:
        tick = time.monotonic()
        hypothesis = transcribe(wav)
        terms = score_terms(reference, hypothesis, annotations[wav.stem])
        outcomes.append(terms)
        scored.append((group, reference, hypothesis))
        records.append({'sample': wav.stem, 'group': group, 'reference': reference, 'hypothesis': hypothesis,
                        'audio_sha256': hashlib.sha256(wav.read_bytes()).hexdigest(),
                        'http_seconds': time.monotonic() - tick, 'term_outcomes': terms})
        print(json.dumps({'completed_samples': len(records), 'total_samples': len(pairs)}), flush=True)
    assert input_digest(args.data) == args.expected_input_sha256
    aggregate = {**summarize(scored), 'medical_terms': summarize_terms(outcomes),
                 'provider': args.provider, 'checked_at': checked_at, 'audio_seconds': sum(p[3] for p in pairs),
                 'http_total_seconds': time.monotonic() - started, 'input_sha256': args.expected_input_sha256,
                 'annotation_sha256': hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
                 'source_sha256': {name: hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                                   for name in ('compare_api.py', 'clinical_terms.py', 'metrics.py')}}
    if args.provider == 'whisper-1':
        aggregate['api_settings'] = {'model': 'whisper-1', 'language': 'ko', 'temperature': 0,
                                     'prompt': None, 'automatic_retries': 0}
    write_private_json(args.out, {'aggregate': aggregate, 'private_pairs': records})
    print(json.dumps(aggregate, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
