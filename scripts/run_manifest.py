#!/usr/bin/env python3
"""Serial, private ASR runner with durable per-item receipts and safe resume.

No automatic retry of ambiguous requests. Explicit failed-item retry is limited
to one additional attempt. Successful items are immutable within a run.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from compare_api import api_settings, api_transcribe
from prepare_corpus import digest
from metrics import normalize


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.pending')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_manifest(path, expected):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Manifest fingerprint mismatch')
    manifest = json.loads(raw)
    rows = manifest['samples']
    if manifest.get('schema') != 'corpus-manifest/v2' or not rows:
        raise ValueError('Invalid or empty manifest')
    if not manifest['summary']['run_ready_audio_hashes']:
        raise ValueError('Audio bytes have not been sealed; selection is not run-ready')
    if len({row['sample_id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate sample identity')
    for row in rows:
        if hashlib.sha256(row['reference'].encode()).hexdigest() != row['reference_utf8_sha256']:
            raise ValueError('Reference fingerprint mismatch')
        if hashlib.sha256(normalize(row['reference']).encode()).hexdigest() != row['text_group']:
            raise ValueError('Reference group mismatch')
        if digest(row['audio_path']) != row['audio_sha256']:
            raise ValueError('Audio fingerprint mismatch')
    return manifest


def run_samples(rows, output, config, transcribe, *, retry_failed=False):
    """Caller holds a nonblocking run-directory lock. Returns counts, not text."""
    output = Path(output)
    receipt = output/'run.json'
    if receipt.exists():
        if json.loads(receipt.read_text()) != config:
            raise ValueError('Run settings changed; use a new run directory')
    else:
        atomic_json(receipt, config)
    completed, skipped, failed = 0, 0, 0
    for row in rows:
        token = hashlib.sha256(row['sample_id'].encode()).hexdigest()
        path = output/(token+'.json')
        previous = json.loads(path.read_text()) if path.exists() else None
        attempts = 1
        if previous:
            if previous['sample_id'] != row['sample_id'] or previous['audio_sha256'] != row['audio_sha256']:
                raise ValueError('Receipt identity mismatch')
            if previous['status'] == 'success':
                skipped += 1
                continue
            if previous['status'] != 'failed':
                raise RuntimeError('Ambiguous in-flight request; reconcile manually before resuming')
            if not retry_failed:
                skipped += 1
                failed += 1
                continue
            if previous['attempts'] >= 2:
                raise RuntimeError('Failed-item retry limit reached')
            attempts = previous['attempts'] + 1
        if digest(row['audio_path']) != row['audio_sha256']:
            raise ValueError('Audio changed before request')
        state = {'sample_id': row['sample_id'], 'audio_sha256': row['audio_sha256'],
                 'status': 'in_flight', 'attempts': attempts,
                 'started_at': datetime.now(timezone.utc).isoformat()}
        if previous:
            state['previous_attempts'] = previous.get('previous_attempts', []) + [
                {key: value for key, value in previous.items() if key != 'previous_attempts'}]
        atomic_json(path, state)
        tick = time.monotonic()
        try:
            result = transcribe(Path(row['audio_path']))
            if not isinstance(result.get('text'), str):
                raise RuntimeError('INVALID_TRANSCRIPT')
        except Exception as error:
            # Never persist provider bodies, signed links, key values or exception text.
            code = str(error)
            safe_code = code if code.startswith('API_HTTP_') and code[9:].isdigit() else 'REQUEST_FAILED'
            atomic_json(path, {**state, 'status': 'failed', 'error_code': safe_code,
                              'http_seconds': time.monotonic()-tick})
            raise RuntimeError(safe_code) from None
        atomic_json(path, {**state, 'status': 'success', 'hypothesis': result['text'],
            'response_model': result.get('response_model'),
            'processing_seconds': result.get('processing_seconds'),
            'http_seconds': time.monotonic()-tick})
        completed += 1
        print(json.dumps({'completed_this_invocation': completed, 'skipped_receipts': skipped,
                          'planned_samples': len(rows)}), flush=True)
    return {'completed_this_invocation': completed, 'skipped_receipts': skipped,
            'failed_receipts': failed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'expected-manifest-sha256', 'private-out'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--provider', choices=['gpt-transcribe', 'whisper-1', 'qwen-service'], required=True)
    parser.add_argument('--external-audio-use-authorized', action='store_true')
    parser.add_argument('--service-source')
    parser.add_argument('--callers')
    parser.add_argument('--base-url', default='http://127.0.0.1:8112')
    parser.add_argument('--condition-id', required=True, help='Predeclared model/configuration identity')
    parser.add_argument('--execution-metadata', required=True,
                        help='Private JSON with revisions, environment and timing conditions; no credentials')
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    manifest = load_manifest(args.manifest, args.expected_manifest_sha256)
    settings = None
    if args.provider == 'qwen-service':
        if not args.service_source or not args.callers:
            parser.error('Existing service source and private caller file are required')
        sys.path.insert(0, args.service_source)
        from clients.python_client import SttClient
        callers = json.loads(Path(args.callers).read_text())
        client = SttClient(args.base_url, callers['physio_app_test'])
        transcribe = lambda path: client.transcribe(path.read_bytes(), timeout=600, poll_seconds=.1)
    else:
        key = os.environ.get('OPENAI_API_KEY', '')
        if not args.external_audio_use_authorized or not key:
            parser.error('Expanded corpus external-use authorization and an approved process key are required')
        settings = api_settings(args.provider)
        transcribe = lambda path: api_transcribe(path, key, args.provider)
    config = {'schema': 'manifest-run/v1', 'manifest_sha256': args.expected_manifest_sha256,
              'provider': args.provider, 'condition_id': args.condition_id,
              'execution_metadata': json.loads(Path(args.execution_metadata).read_text()),
              'api_settings': settings, 'automatic_retries': 0,
              'timing': 'serial HTTP; includes first request; warmup not silently excluded',
              'source_sha256': {name: digest(Path(__file__).parent/name)
                  for name in ('run_manifest.py', 'compare_api.py', 'prepare_corpus.py', 'metrics.py')}}
    if args.provider == 'qwen-service':
        config['service_source_sha256'] = digest(Path(args.service_source)/'clients/python_client.py')
        config['base_url'] = args.base_url
    output = Path(args.private_out)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (output/'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another writer owns this run')
        result = run_samples(manifest['samples'], output, config, transcribe, retry_failed=args.retry_failed)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
