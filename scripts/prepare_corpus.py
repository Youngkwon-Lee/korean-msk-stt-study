#!/usr/bin/env python3
"""Prepare a private, deterministic corpus holdout; never infer speaker identity.

Only the summary is a publication candidate. Manifests and reference caches are
private. This script performs no inference, API upload or participant collection.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from metrics import normalize


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def index_files(root, suffix):
    found = {}
    for path in sorted(Path(root).rglob('*' + suffix)):
        if path.name.startswith('.') or path.is_symlink():
            continue
        if path.name in found:
            raise ValueError('Ambiguous duplicate source filename')
        found[path.name] = path
    if not found:
        raise ValueError('No source files')
    return found


def prepare(audio_root, text_root, exclusions, per_stratum, seed, *,
            references=None, hash_audio=False, drive_ids=False):
    if per_stratum < 0:
        raise ValueError('Invalid stratum sample size')
    audio, texts = index_files(audio_root, '.wav'), index_files(text_root, '.txt')
    refs = {}
    cache = None
    if references is not None:
        cache = {}
        for row in references:
            if row['name'] in cache:
                raise ValueError('Duplicate cached reference')
            cache[row['name']] = row['reference']
        if set(cache) != set(texts):
            raise ValueError('Reference cache/source mismatch')
    for name, path in texts.items():
        ref = (cache[name] if cache is not None else path.read_text(encoding='utf-8-sig')).lstrip('\ufeff').strip()
        refs[name] = (ref, normalize(ref))
    excluded_names = {x['source_name'] for x in exclusions}
    old_labels = {x['source_name'] for x in exclusions if x['kind'] == 'labels'}
    if not old_labels or not old_labels <= set(refs):
        raise ValueError('Development reference exclusion cannot be verified')
    excluded_text = {refs[name][1] for name in old_labels}
    groups, paired, excluded = defaultdict(list), Counter(), Counter()
    for name, path in sorted(audio.items()):
        label = path.with_suffix('.txt').name
        if label not in texts:
            continue
        code = path.stem.split('-', 1)[0]
        paired[code] += 1
        ref, normalized = refs[label]
        if not normalized or name in excluded_names or label in excluded_names or normalized in excluded_text:
            excluded[code] += 1
            continue
        groups[code].append({'sample_id': path.stem, 'source_code': code,
            'group': 'patient' if code.startswith('PA_') else 'staff',
            'audio_path': str(path.resolve()), 'reference_path': str(texts[label].resolve()),
            'reference': ref, 'reference_utf8_sha256': hashlib.sha256(ref.encode()).hexdigest(),
            'text_group': hashlib.sha256(normalized.encode()).hexdigest(),
            'audio_bytes': path.stat().st_size})
    chosen_rows, inventory = [], []
    for number, code in enumerate(sorted(paired), 1):
        order = sorted(groups[code], key=lambda r: hashlib.sha256(f'{seed}:{r["sample_id"]}'.encode()).hexdigest())
        seen, unique = set(), []
        for row in order:
            if row['text_group'] not in seen:
                seen.add(row['text_group'])
                unique.append(row)
        if per_stratum > len(unique):
            raise ValueError(f'Insufficient unique references in source stratum C{number:02}')
        chosen = unique[:per_stratum]
        latency = {r['sample_id'] for r in sorted(chosen, key=lambda r: hashlib.sha256(f'latency:{seed}:{r["sample_id"]}'.encode()).hexdigest())[:20]}
        review = {r['sample_id'] for r in sorted(chosen, key=lambda r: hashlib.sha256(f'review:{seed}:{r["sample_id"]}'.encode()).hexdigest())[:50]}
        for row in chosen:
            row['stratum'] = f'C{number:02}'
            row['latency_substudy'] = row['sample_id'] in latency
            row['reference_review_substudy'] = row['sample_id'] in review
            if hash_audio:
                row['audio_sha256'] = digest(row['audio_path'])
            if drive_ids:
                for key, pathkey in (('audio_drive_id', 'audio_path'), ('reference_drive_id', 'reference_path')):
                    row[key] = subprocess.check_output(['xattr', '-p', 'com.google.drivefs.item-id#S', row[pathkey]], text=True).strip()
            chosen_rows.append(row)
        inventory.append({'source_stratum': f'C{number:02}',
            'role_label': 'patient' if code.startswith('PA_') else 'staff',
            'paired_candidates': paired[code], 'development_or_same_text_excluded': excluded[code],
            'within_stratum_duplicate_text_excluded': len(order)-len(unique),
            'eligible_unique_texts': len(unique), 'selected': len(chosen)})
    summary = {'checked_at': datetime.now(timezone.utc).isoformat(),
        'audio_files': len(audio), 'reference_files': len(texts), 'paired_files': sum(paired.values()),
        'unpaired_audio': len(audio)-sum(paired.values()),
        'unpaired_references': len(texts)-sum(paired.values()),
        'empty_normalized_references': sum(not n for _, n in refs.values()),
        'source_strata': len(paired), 'verified_unique_speakers': None,
        'selected_samples': len(chosen_rows), 'selected_audio_bytes': sum(r['audio_bytes'] for r in chosen_rows),
        'selected_unique_text_groups': len({r['text_group'] for r in chosen_rows}),
        'latency_substudy_samples': sum(r['latency_substudy'] for r in chosen_rows),
        'reference_review_substudy_samples': sum(r['reference_review_substudy'] for r in chosen_rows),
        'selection_seed': seed, 'run_ready_audio_hashes': hash_audio,
        'reference_hash_scope': 'canonical stripped UTF-8 text; not original label file bytes',
        'selection': 'source-code-stratified utterance holdout; all normalized development references excluded',
        'inference_scope': 'observed public corpus only; speaker identity and clinical representativeness unverified',
        'by_source_stratum': inventory}
    return chosen_rows, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('audio-root', 'text-root', 'exclude-metadata', 'private-out'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--reference-cache')
    parser.add_argument('--per-stratum', type=int, default=300, help='0: inventory only')
    parser.add_argument('--seed', type=int, default=20261117)
    parser.add_argument('--hash-audio', action='store_true')
    parser.add_argument('--drive-ids', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    output = Path(args.private_out)
    if output.exists():
        parser.error('Output already exists')
    refs = json.loads(Path(args.reference_cache).read_text()) if args.reference_cache else None
    rows, summary = prepare(args.audio_root, args.text_root,
        json.loads(Path(args.exclude_metadata).read_text()), args.per_stratum, args.seed,
        references=refs, hash_audio=args.hash_audio, drive_ids=args.drive_ids)
    payload = json.dumps({'schema': 'corpus-manifest/v2', 'summary': summary, 'samples': rows}, ensure_ascii=False, indent=2)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (output/'manifest.json').write_text(payload)
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    (output/'manifest.sha256').write_text(fingerprint+'\n')
    (output/'summary.json').write_text(json.dumps({**summary, 'manifest_sha256': fingerprint}, ensure_ascii=False, indent=2))
    print(json.dumps({**summary, 'manifest_sha256': fingerprint}, ensure_ascii=False))


if __name__ == '__main__':
    main()
