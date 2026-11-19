#!/usr/bin/env python3
"""Rescore frozen model receipts after complete, model-blind reference review.

No inference, network access or changes to the review database/source manifest.
All outputs, including aggregate publication candidates, start in private storage.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3

from analyze_manifest import percentile, read_run
from clinical_terms import LEXICON, annotate, score_terms
from metrics import edit_distance, normalize
from review_app import DB_ID

MODELS = ('qwen_1_7b', 'gpt_transcribe')
LABELS = {'qwen_1_7b': 'Qwen3-ASR 1.7B', 'gpt_transcribe': 'GPT-Transcribe'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_manifest(path, expected_sha, expected_count=300):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('Manifest fingerprint mismatch')
    manifest = json.loads(raw)
    if manifest.get('schema') != 'corpus-manifest/v2' or not manifest['summary']['run_ready_audio_hashes']:
        raise ValueError('Sealed source manifest required')
    rows = manifest['samples']
    ids = [r['sample_id'] for r in rows]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r'[A-Za-z0-9_-]+', s) for s in ids):
        raise ValueError('Invalid source identities')
    selected = [r for r in rows if r.get('reference_review_substudy') is True]
    if len(selected) != expected_count or not selected:
        raise ValueError('Frozen review subset count mismatch')
    for row in selected:
        reference = row['reference']
        if (not isinstance(reference, str) or not normalize(reference)
                or hashlib.sha256(reference.encode()).hexdigest() != row['reference_utf8_sha256']
                or not re.fullmatch(r'[0-9a-f]{64}', row['audio_sha256'])
                or row['group'] not in ('staff', 'patient')):
            raise ValueError('Invalid frozen reference metadata')
    return selected


def snapshot_db(path, manifest_sha, rows, reviewer):
    """Read one SQLite snapshot, including WAL commits; never initialize a DB."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Review database unavailable; no database was created')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,39}', reviewer):
        raise ValueError('Invalid reviewer identifier')
    with closing(sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if (db.execute('PRAGMA application_id').fetchone()[0] != DB_ID
                or db.execute('PRAGMA user_version').fetchone()[0] != 1):
            raise ValueError('Unrecognized review database')
        stored = db.execute("SELECT value FROM metadata WHERE key='manifest_sha256'").fetchone()
        if not stored or stored[0] != manifest_sha:
            raise ValueError('Review database/manifest mismatch')
        records = {r['item']: dict(r) for r in db.execute('SELECT * FROM reviews WHERE reviewer=?', (reviewer,))}
    ids = {f'R{i:03}' for i in range(1, len(rows) + 1)}
    if set(records) - ids:
        raise ValueError('Unexpected review items')
    return {'schema': 'review-rescore-snapshot/v1', 'manifest_sha256': manifest_sha,
            'reviewer': reviewer, 'items': [
                {'id': f'R{i:03}', 'audio_sha256': row['audio_sha256'],
                 'provided_reference': row['reference'], 'review': records.get(f'R{i:03}')}
                for i, row in enumerate(rows, 1)]}


def validate_snapshot(snapshot, manifest_sha, rows):
    if snapshot.get('manifest_sha256') != manifest_sha or len(snapshot['items']) != len(rows):
        raise ValueError('Review snapshot/manifest mismatch')
    statuses = Counter()
    for number, (item, row) in enumerate(zip(snapshot['items'], rows), 1):
        if (item['id'] != f'R{number:03}' or item['audio_sha256'] != row['audio_sha256']
                or item['provided_reference'] != row['reference']):
            raise ValueError('Review identity/order mismatch')
        record = item['review']
        status = record['status'] if record else 'unreviewed'
        if status not in ('unreviewed', 'draft', 'flagged', 'reviewed'):
            raise ValueError('Invalid review status')
        statuses[status] += 1
        if status == 'reviewed':
            seconds = record.get('played_seconds')
            if (not isinstance(record.get('transcript'), str) or not normalize(record['transcript'])
                    or not record.get('heard') or type(record.get('version')) is not int or record['version'] < 1
                    or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < .05):
                raise ValueError('Invalid completed review evidence')
    return {'total': len(rows), 'reviewed': statuses['reviewed'],
            'unreviewed': statuses['unreviewed'], 'draft': statuses['draft'],
            'flagged': statuses['flagged'], 'ready': statuses['reviewed'] == len(rows)}


def load_outputs(root, manifest_sha, rows, provider, annotation_sha):
    root = Path(root)
    config, records = read_run(root, manifest_sha, rows)
    if config.get('provider') != provider or config.get('annotation_sha256') != annotation_sha:
        raise ValueError('Frozen model/annotation identity mismatch')
    config_raw = (root / 'run.json').read_bytes()
    if json.loads(config_raw) != config:
        raise ValueError('Model run changed during read')
    receipt_hashes = []
    for row in rows:
        raw = (root / (hashlib.sha256(row['sample_id'].encode()).hexdigest() + '.json')).read_bytes()
        if json.loads(raw) != records[row['sample_id']]:
            raise ValueError('Model receipt changed during read')
        receipt_hashes.append(hashlib.sha256(raw).hexdigest())
    hashes = {'run_config': hashlib.sha256(config_raw).hexdigest(), 'receipts': receipt_hashes}
    return records, fingerprint(hashes)


def score_rows(rows, references, records):
    vectors = []
    for row, reference in zip(rows, references):
        record = records[row['sample_id']]
        spans = annotate(reference)  # Regenerate positions AND denominator on corrected text.
        errors = None if record['status'] == 'failed' else edit_distance(normalize(reference), normalize(record['hypothesis']))
        term_errors = None if errors is None else sum(not x['preserved'] for x in score_terms(reference, record['hypothesis'], spans))
        categories = {}
        outcomes = [] if errors is None else score_terms(reference, record['hypothesis'], spans)
        for category in sorted(set(s['category'] for s in spans)):
            categories[category] = {'occurrences': sum(s['category'] == category for s in spans),
                'errors': None if errors is None else sum(x['category'] == category and not x['preserved'] for x in outcomes)}
        vectors.append({'characters': len(normalize(reference)), 'edit_errors': errors,
                        'term_occurrences': len(spans), 'term_errors': term_errors,
                        'categories': categories})
    return vectors


def aggregate(vectors, indices):
    values = [vectors[i] for i in indices]
    chars = sum(v['characters'] for v in values)
    errors = sum(v['edit_errors'] for v in values)
    terms = sum(v['term_occurrences'] for v in values)
    term_errors = sum(v['term_errors'] for v in values)
    categories = {}
    for category in sorted({c for v in values for c in v['categories']}):
        n = sum(v['categories'].get(category, {}).get('occurrences', 0) for v in values)
        e = sum(v['categories'].get(category, {}).get('errors', 0) for v in values)
        categories[category] = {'occurrences': n, 'errors': e, 'error_rate': e / n if n else None}
    return {'sentences': len(values), 'reference_characters': chars, 'edit_errors': errors,
            'cer': errors / chars if chars else None,
            'exact_sentences': sum(v['edit_errors'] == 0 for v in values),
            'term_occurrences': terms, 'term_errors': term_errors,
            'term_error_rate': term_errors / terms if terms else None,
            'sentences_with_term_error': sum(v['term_errors'] > 0 for v in values),
            'by_category': categories}


def paired_difference(rows, vectors, indices, repetitions, seed):
    # Preserve original preselected phrase blocks even when corrected text differs.
    blocks = defaultdict(lambda: [0, 0, 0, 0])
    for i in indices:
        a, b = (vectors[m][i] for m in ('gpt_transcribe', 'qwen_1_7b'))
        for j, value in enumerate((a['edit_errors'] - b['edit_errors'], a['characters'],
                                   a['term_errors'] - b['term_errors'], a['term_occurrences'])):
            blocks[normalize(rows[i]['reference'])][j] += value
    values = list(blocks.values())
    total = [sum(v[i] for v in values) for i in range(4)]
    rng = random.Random(seed)
    cer_draws, term_draws = [], []
    if len(values) >= 2:
        for _ in range(repetitions):
            draw = [values[rng.randrange(len(values))] for _ in values]
            cer_draws.append(sum(v[0] for v in draw) / sum(v[1] for v in draw))
            n = sum(v[3] for v in draw)
            if n:
                term_draws.append(sum(v[2] for v in draw) / n)
    interval = lambda draws: [percentile(draws, .025), percentile(draws, .975)] if draws else None
    return {'direction': 'GPT minus Qwen', 'cer_difference': total[0] / total[1] if total[1] else None,
            'literal_term_error_rate_difference': total[2] / total[3] if total[3] else None,
            'cer_descriptive_interval_95': interval(cer_draws),
            'literal_term_descriptive_interval_95': interval(term_draws),
            'original_phrase_blocks': len(values), 'repetitions': repetitions, 'seed': seed,
            'term_resamples_with_nonzero_denominator': len(term_draws),
            'scope': 'Observed-corpus paired original-phrase-block percentile resampling; unknown speaker dependence NOT adjusted; not population confidence intervals; no p values'}


def rescore(rows, snapshot, records, manifest_sha, provenance, repetitions=2000, seed=20261117):
    if repetitions < 1:
        raise ValueError('Positive resampling repetitions required')
    progress = validate_snapshot(snapshot, manifest_sha, rows)
    if not progress['ready']:
        raise ValueError(f"Review incomplete: {progress['reviewed']}/{progress['total']}; drafts/holds are not complete")
    original = [r['reference'] for r in rows]
    corrected = [item['review']['transcript'] for item in snapshot['items']]
    common = [i for i, row in enumerate(rows) if all(records[m][row['sample_id']]['status'] == 'success' for m in MODELS)]
    failures = {m: sum(records[m][r['sample_id']]['status'] == 'failed' for r in rows) for m in MODELS}
    stages, private = {}, {}
    for stage, references in (('provided_reference', original), ('reviewed_reference', corrected)):
        vectors = {m: score_rows(rows, references, records[m]) for m in MODELS}
        private[stage] = vectors
        stages[stage] = {'paired_success_samples': len(common), 'models': {
            m: {**aggregate(vectors[m], common), 'technical_failures': failures[m],
                'by_role': {g: aggregate(vectors[m], [i for i in common if rows[i]['group'] == g])
                            for g in ('staff', 'patient')}} for m in MODELS},
            'paired_difference': paired_difference(rows, vectors, common, repetitions, seed)}
    output = {'schema': 'reviewed-reference-aggregate/v1',
        'created_at': datetime.now(timezone.utc).isoformat(), 'planned_samples': len(rows),
        'scope': 'Frozen reference-review subset only; original full-corpus results unchanged',
        'reference_review': {'complete': True, 'reviewer_count': 1, 'consensus_adjudication_complete': False,
            'raw_text_changed': sum(a != b for a, b in zip(original, corrected)),
            'normalized_text_changed': sum(normalize(a) != normalize(b) for a, b in zip(original, corrected)),
            'provided_reference_characters_all_selected': sum(len(normalize(r)) for r in original),
            'reviewed_reference_characters_all_selected': sum(len(normalize(r)) for r in corrected)},
        'provenance': {**provenance, 'manifest_sha256': manifest_sha, 'review_snapshot_sha256': fingerprint(snapshot),
                       'lexicon_sha256': fingerprint(LEXICON)},
        'analysis_set': 'Same both-model successful samples in every before/after and role/term comparison; failures counted separately',
        'term_definition': 'Frozen 25-type lexicon; aligned exact literal preservation; regenerated on each reference version; no contextual, semantic or severity adjudication',
        'independent_speakers': None, 'clinical_semantic_errors': None, 'clinical_editing_burden': None,
        'stages': stages}
    return output, {'schema': 'private-review-rescore/v1', 'review_snapshot_sha256': fingerprint(snapshot),
                    'vector_order': 'Selected manifest order', 'common_success_indices': common, 'vectors': private}


def percent(value):
    return 'NA' if value is None else f'{100 * value:.2f}'


def manuscript_tables(result):
    stages = result['stages']
    tables = []
    header = ['Reference', 'Model', 'Paired n', 'CER % (edits/chars)', 'Exact n/N', 'Literal errors % (n/N)', 'Technical failures n/N']
    rows = []
    for stage, label in (('provided_reference', 'Provided'), ('reviewed_reference', 'Reviewed')):
        for model in MODELS:
            s = stages[stage]['models'][model]
            rows.append([label, LABELS[model], str(s['sentences']),
                f"{percent(s['cer'])} ({s['edit_errors']}/{s['reference_characters']})",
                f"{s['exact_sentences']}/{s['sentences']}",
                f"{percent(s['term_error_rate'])} ({s['term_errors']}/{s['term_occurrences']})",
                f"{s['technical_failures']}/{result['planned_samples']}"])
    tables.append(('Table 1. Same-subset accuracy before and after reference review', header, rows))
    rows = []
    for role in ('staff', 'patient'):
        for model in MODELS:
            s = stages['reviewed_reference']['models'][model]['by_role'][role]
            rows.append([role, LABELS[model], str(s['sentences']), f"{percent(s['cer'])} ({s['edit_errors']}/{s['reference_characters']})",
                         f"{percent(s['term_error_rate'])} ({s['term_errors']}/{s['term_occurrences']})"])
    tables.append(('Table 2. Reviewed-reference accuracy by source role label', ['Role label', 'Model', 'Paired n', 'CER % (edits/chars)', 'Literal errors % (n/N)'], rows))
    models = stages['reviewed_reference']['models']
    rows = []
    for category in sorted(set(models[MODELS[0]]['by_category']) | set(models[MODELS[1]]['by_category'])):
        a, b = (models[m]['by_category'][category] for m in MODELS)
        rows.append([category, str(a['occurrences']), f"{a['errors']} ({percent(a['error_rate'])})", f"{b['errors']} ({percent(b['error_rate'])})"])
    tables.append(('Table 3. Reviewed-reference literal errors by lexicon category', ['Category', 'Occurrences', 'Qwen errors n (%)', 'GPT errors n (%)'], rows))
    rows = []
    for stage, label in (('provided_reference', 'Provided'), ('reviewed_reference', 'Reviewed')):
        p = stages[stage]['paired_difference']
        for metric, key, interval_key in (('CER', 'cer_difference', 'cer_descriptive_interval_95'),
                ('Literal term errors', 'literal_term_error_rate_difference', 'literal_term_descriptive_interval_95')):
            ci = p[interval_key]
            rows.append([label, metric, percent(p[key]), 'NA' if ci is None else f'{percent(ci[0])} to {percent(ci[1])}'])
    tables.append(('Table 4. Paired descriptive differences (GPT minus Qwen)', ['Reference', 'Metric', 'Difference pp', 'Descriptive 95% range pp'], rows))
    return tables


def write_bundle(out, result, private, snapshot):
    out = Path(out)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, value in (('aggregate.json', result), ('review-snapshot.json', snapshot), ('private-scores.json', private)):
        with (out / name).open('x') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
    tables = manuscript_tables(result)
    footnotes = [
        f"Frozen review subset: {result['planned_samples']} utterances; one completed reviewer record per item. A second review and consensus adjudication are not established by these files.",
        'CER is total normalized Levenshtein edits / total reference characters; spaces/punctuation removed, alphanumerics and precomposed Hangul retained, Latin lowercased; existing parenthesized dual notation resolved as in the original scorer.',
        'Literal errors use the frozen 25-type lexicon and global character alignment. Occurrence positions and denominators are regenerated separately for provided and reviewed references. Counts are occurrences, not unique types or validated clinical meaning errors.',
        'All accuracy rows use the same both-model success set; technical failures remain reported against the full selected subset. NA indicates a zero denominator or unavailable descriptive range.',
        'Role labels and phrase blocks do not establish independent speakers or patients. Table 4 gives conditional paired original-phrase-block resampling ranges, not population confidence intervals or significance tests. Zero-term-denominator replicates are omitted from the term range; retained counts are recorded in aggregate.json.',
        'Only the preselected review subset was rescored. Previously published full-corpus accuracy and separate latency measurements are unchanged. No model requests or latency remeasurements were made.',
        'Outputs are private publication candidates. Review aggregate content before copying selected files to a public repository; never publish review-snapshot.json or private-scores.json.']
    md = ['# Reference-review sensitivity analysis (manuscript draft)', '']
    tex = []
    with (out / 'tables.csv').open('x', newline='') as stream:
        writer = csv.writer(stream)
        for title, header, rows in tables:
            md += ['## ' + title, '', '| ' + ' | '.join(header) + ' |', '| ' + ' | '.join(['---'] * len(header)) + ' |']
            md += ['| ' + ' | '.join(row) + ' |' for row in rows]
            md += ['']
            writer.writerow([title]); writer.writerow(header); writer.writerows(rows); writer.writerow([])
            escape = lambda s: s.replace('\\', r'\textbackslash{}').replace('_', r'\_').replace('%', r'\%').replace('&', r'\&')
            tex += [r'\begin{table}[htbp]', r'\centering', r'\caption{' + escape(title) + '}',
                    r'\resizebox{\linewidth}{!}{%',
                    r'\begin{tabular}{' + 'l' * len(header) + '}', r'\hline']
            tex += [' & '.join(escape(c) for c in row) + r' \\' for row in [header] + rows]
            tex += [r'\hline', r'\end{tabular}', '}', r'\end{table}', '']
    md += ['## Notes', ''] + [f'{i}. {note}' for i, note in enumerate(footnotes, 1)]
    (out / 'tables.md').write_text('\n'.join(md) + '\n')
    (out / 'tables.tex').write_text('\n'.join(tex + ['% ' + n for n in footnotes]) + '\n')
    review = result['reference_review']
    methods = (f"# Reference-review analysis record\n\n"
        f"All {result['planned_samples']} preselected items had completed listening-confirmed records from one reviewer at the frozen snapshot. "
        f"Raw text changed in {review['raw_text_changed']} items; normalized text changed in {review['normalized_text_changed']}. "
        "These counts describe reference changes, not model errors or reviewer accuracy. "
        "Model outputs were kept outside the review application. The analysis reuses the original accuracy-phase receipts, never the serial latency repeats. "
        "This establishes completion of one review pass, not independent two-reviewer consensus or clinical validity.\n\n"
        + '\n\n'.join(footnotes[1:6]) + '\n\n## Provenance\n\n```json\n'
        + json.dumps(result['provenance'], indent=2) + '\n```\n')
    (out / 'methods.md').write_text(methods)


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'expected-manifest-sha256', 'review-db', 'reviewer'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--expected-count', type=int, default=300)
    p.add_argument('--check-ready', action='store_true')
    for name in ('qwen-run', 'gpt-run', 'annotations', 'expected-qwen-sha256', 'expected-gpt-sha256', 'out'):
        p.add_argument('--' + name)
    p.add_argument('--repetitions', type=int, default=2000)
    p.add_argument('--seed', type=int, default=20261117)
    args = p.parse_args()
    try:
        rows = load_manifest(args.manifest, args.expected_manifest_sha256, args.expected_count)
        snapshot = snapshot_db(args.review_db, args.expected_manifest_sha256, rows, args.reviewer)
        progress = validate_snapshot(snapshot, args.expected_manifest_sha256, rows)
        if args.check_ready:
            print(json.dumps(progress)); return 0 if progress['ready'] else 2
        if not progress['ready']:
            raise ValueError(f"검토 미완료: {progress['reviewed']}/{progress['total']}건. 초안·보류는 완료에 포함하지 않습니다.")
        if any(getattr(args, n.replace('-', '_')) is None for n in ('qwen-run', 'gpt-run', 'annotations', 'expected-qwen-sha256', 'expected-gpt-sha256', 'out')):
            raise ValueError('Completed review requires both frozen model runs, annotations, expected fingerprints and a new output directory')
        annotations = json.loads(Path(args.annotations).read_text())
        for row in rows:
            if annotations.get(row['sample_id']) != annotate(row['reference']):
                raise ValueError('Original annotation/lexicon mismatch')
        annotation_sha = digest(args.annotations)
        records, hashes = {}, {}
        for model, provider, root, expected in (
                ('qwen_1_7b', 'qwen-local', args.qwen_run, args.expected_qwen_sha256),
                ('gpt_transcribe', 'gpt-transcribe', args.gpt_run, args.expected_gpt_sha256)):
            records[model], hashes[model] = load_outputs(root, args.expected_manifest_sha256, rows, provider, annotation_sha)
            if hashes[model] != expected:
                raise ValueError('Frozen model receipt fingerprint mismatch')
        provenance = {'model_receipts_sha256': hashes, 'original_annotations_sha256': annotation_sha,
                      'analysis_source_sha256': {name: digest(Path(__file__).with_name(name))
                                                for name in ('rescore_reviewed.py', 'clinical_terms.py', 'metrics.py')}}
        result, private = rescore(rows, snapshot, records, args.expected_manifest_sha256, provenance, args.repetitions, args.seed)
        write_bundle(args.out, result, private, snapshot)
        print('재평가 완료: tables.md · tables.csv · tables.tex · aggregate.json · methods.md. 전체 결과는 비공개로 저장했습니다.')
        return 0
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        # File-not-found messages may contain private paths, so do not print exception bodies.
        message = str(error) if type(error) is ValueError else type(error).__name__
        print('재평가를 중단했습니다: ' + message)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
