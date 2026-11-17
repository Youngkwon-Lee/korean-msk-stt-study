#!/usr/bin/env python3
"""Paired corpus aggregates; descriptive phrase-cluster sensitivity intervals.

Intervals condition on the observed source and do not model unknown speaker
dependence. They are NOT population/clinical confidence intervals.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random

from metrics import edit_distance, normalize


def read_run(root, manifest_sha, rows):
    root = Path(root)
    config = json.loads((root/'run.json').read_text())
    if config.get('manifest_sha256') != manifest_sha:
        raise ValueError('Run/manifest mismatch')
    results = {}
    for row in rows:
        token = hashlib.sha256(row['sample_id'].encode()).hexdigest()
        path = root/(token+'.json')
        if not path.exists():
            raise ValueError('Incomplete run: missing receipt')
        item = json.loads(path.read_text())
        if item['sample_id'] != row['sample_id'] or item['audio_sha256'] != row['audio_sha256']:
            raise ValueError('Result identity mismatch')
        if item['status'] not in ('success', 'failed'):
            raise ValueError('Incomplete run: ambiguous request')
        if item['status'] == 'success' and not isinstance(item.get('hypothesis'), str):
            raise ValueError('Invalid successful receipt')
        results[row['sample_id']] = item
    return config, results


def percentile(values, proportion):
    ordered = sorted(values)
    location = (len(ordered)-1)*proportion
    low = int(location)
    high = min(low+1, len(ordered)-1)
    return ordered[low]+(ordered[high]-ordered[low])*(location-low)


def paired_summary(rows, a, b, *, repetitions=2000, seed=20261117):
    if repetitions < 1 or not rows:
        raise ValueError('Nonempty rows and positive repetitions required')
    if set(a) != {r['sample_id'] for r in rows} or set(a) != set(b):
        raise ValueError('Paired row sets differ')
    failures = [0, 0]
    common = []
    penalty_errors = [0, 0]
    total_chars = 0
    for row in rows:
        reference = normalize(row['reference'])
        if not reference:
            raise ValueError('Empty normalized reference')
        count = len(reference)
        total_chars += count
        records = [a[row['sample_id']], b[row['sample_id']]]
        errors = []
        for number, record in enumerate(records):
            if record['status'] == 'failed':
                failures[number] += 1
                errors.append(None)
                penalty_errors[number] += count
            elif record['status'] == 'success':
                distance = edit_distance(reference, normalize(record['hypothesis']))
                errors.append(distance)
                penalty_errors[number] += distance
            else:
                raise ValueError('Unresolved status')
        if all(v is not None for v in errors):
            common.append((row['text_group'], errors[0], errors[1], count))
    chars = sum(r[3] for r in common)
    model_errors = [sum(r[i] for r in common) for i in (1, 2)]
    groups = defaultdict(lambda: [0, 0, 0])
    for group, ea, eb, length in common:
        for i, value in enumerate((ea, eb, length)):
            groups[group][i] += value
    interval = None
    if len(groups) >= 2:
        blocks = list(groups.values())
        rng = random.Random(seed)
        contrasts = []
        for _ in range(repetitions):
            sample = [blocks[rng.randrange(len(blocks))] for _ in blocks]
            contrasts.append(sum(v[0]-v[1] for v in sample)/sum(v[2] for v in sample))
        interval = [percentile(contrasts, .025), percentile(contrasts, .975)]
    return {'planned_samples': len(rows), 'technical_failures_a': failures[0],
        'technical_failures_b': failures[1], 'failure_rate_a': failures[0]/len(rows),
        'failure_rate_b': failures[1]/len(rows), 'common_success_samples': len(common),
        'common_success_reference_characters': chars,
        'common_success_edit_errors_a': model_errors[0], 'common_success_edit_errors_b': model_errors[1],
        'common_success_cer_a': model_errors[0]/chars if chars else None,
        'common_success_cer_b': model_errors[1]/chars if chars else None,
        'common_success_cer_difference_a_minus_b': (model_errors[0]-model_errors[1])/chars if chars else None,
        'failure_as_full_reference_deletion_sensitivity_a': penalty_errors[0]/total_chars,
        'failure_as_full_reference_deletion_sensitivity_b': penalty_errors[1]/total_chars,
        'observed_phrase_groups_with_common_success': len(groups),
        'descriptive_phrase_cluster_interval_95': interval,
        'interval_method': 'paired percentile resampling of normalized-reference groups; observed corpus conditional; unknown speaker dependence NOT accounted for',
        'bootstrap_repetitions': repetitions, 'seed': seed,
        'clinical_semantic_errors': None, 'medical_term_errors': None,
        'interpretation': 'No population-level significance or clinical safety inference; term and semantic annotation pending'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'expected-manifest-sha256', 'run-a', 'run-b', 'public-out'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--repetitions', type=int, default=2000)
    parser.add_argument('--seed', type=int, default=20261117)
    args = parser.parse_args()
    raw = Path(args.manifest).read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.expected_manifest_sha256:
        parser.error('Manifest fingerprint mismatch')
    rows = json.loads(raw)['samples']
    config_a, a = read_run(args.run_a, args.expected_manifest_sha256, rows)
    config_b, b = read_run(args.run_b, args.expected_manifest_sha256, rows)
    summary = {**paired_summary(rows, a, b, repetitions=args.repetitions, seed=args.seed),
               'manifest_sha256': args.expected_manifest_sha256,
               'runner_source_sha256_a': config_a['source_sha256'],
               'runner_source_sha256_b': config_b['source_sha256']}
    output = Path(args.public_out)
    with output.open('x') as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print('Wrote aggregate-only publication candidate; manual review required.')


if __name__ == '__main__':
    main()
