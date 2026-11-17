#!/usr/bin/env python3
"""Verify selected WAV bytes without reselection; write a NEW private manifest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import wave

from prepare_corpus import digest, index_files


def seal(path, expected, *, audio_root=None):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Selection fingerprint mismatch')
    manifest = json.loads(raw)
    if manifest.get('schema') != 'corpus-manifest/v2' or not manifest['samples']:
        raise ValueError('Invalid selection manifest')
    if manifest['summary']['run_ready_audio_hashes']:
        raise ValueError('Manifest already sealed')
    materialized = index_files(audio_root, '.wav') if audio_root else None
    total_seconds = 0
    for row in manifest['samples']:
        wav = materialized.get(row['sample_id']+'.wav') if materialized is not None else Path(row['audio_path'])
        if wav is None or wav.is_symlink() or not wav.is_file():
            raise ValueError('Missing or symlinked selected audio')
        if wav.stat().st_size != row['audio_bytes']:
            raise ValueError('Audio metadata size changed')
        fingerprint = digest(wav)
        with wave.open(str(wav), 'rb') as stream:
            if stream.getcomptype() != 'NONE' or not stream.getframerate() or not stream.getnframes():
                raise ValueError('Unsupported or empty WAV')
            row.update(audio_seconds=stream.getnframes()/stream.getframerate(),
                       sample_rate=stream.getframerate(), channels=stream.getnchannels(),
                       sample_width_bytes=stream.getsampwidth())
        if digest(wav) != fingerprint:
            raise ValueError('Audio changed during sealing')
        row.update(audio_path=str(wav.resolve()), audio_sha256=fingerprint)
        total_seconds += row['audio_seconds']
    manifest['selection_manifest_sha256'] = expected
    manifest['summary'].update(run_ready_audio_hashes=True, selected_audio_seconds=total_seconds)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'expected-selection-sha256', 'private-out'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--audio-root', help='Optional materialized copy with unchanged selected WAV basenames')
    args = parser.parse_args()
    os.umask(0o077)
    output = Path(args.private_out)
    if output.exists():
        parser.error('Output exists; never overwrite selection or prior seal')
    manifest = seal(args.manifest, args.expected_selection_sha256, audio_root=args.audio_root)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    with output.open('x') as stream:
        stream.write(payload)
    print(json.dumps({'run_manifest_sha256': hashlib.sha256(payload.encode()).hexdigest(),
                      'selection_manifest_sha256': args.expected_selection_sha256,
                      'selected_samples': len(manifest['samples']),
                      'selected_audio_seconds': manifest['summary']['selected_audio_seconds']}))


if __name__ == '__main__':
    main()
