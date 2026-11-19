"""Synthetic reference changes only; never create clinical review decisions."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from clinical_terms import annotate
from rescore_reviewed import (digest, load_manifest, load_outputs, manuscript_tables,
                             rescore, snapshot_db, validate_snapshot, write_bundle)
from review_app import Dataset, Store
from test_review_app import fixture, payload


class ReviewRescoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest, _, self.audio = fixture(self.root)
        data = json.loads(self.manifest.read_text())
        for row, text, role in zip(data['samples'], ['어깨 통증', '통증', '미선택 합성 자료'], ['staff', 'patient', 'staff']):
            row.update(reference=text, reference_utf8_sha256=hashlib.sha256(text.encode()).hexdigest(), group=role)
        self.manifest.write_text(json.dumps(data))
        self.sha = digest(self.manifest)
        self.rows = load_manifest(self.manifest, self.sha, 2)
        self.dataset = Dataset(self.manifest, self.sha, self.audio, 2)
        self.db = self.root / 'reviews.sqlite3'
        self.store = Store(self.db, self.dataset, 'reviewer-1')
        self.annotations = self.root / 'annotations.json'
        self.annotations.write_text(json.dumps({r['sample_id']: annotate(r['reference']) for r in data['samples']}))
        self.runs, self.records, self.hashes = {}, {}, {}
        for model, provider, texts in (
                ('qwen_1_7b', 'qwen-local', ['어깨 통증', '통증']),
                ('gpt_transcribe', 'gpt-transcribe', ['허리 통증', '통증 어깨 통증'])):
            root = self.root / model; root.mkdir(); self.runs[model] = root
            (root / 'run.json').write_text(json.dumps({'provider': provider, 'manifest_sha256': self.sha,
                'annotation_sha256': digest(self.annotations), 'condition_id': 'synthetic-only'}))
            for row, text in zip(self.rows, texts):
                record = {'sample_id': row['sample_id'], 'audio_sha256': row['audio_sha256'],
                          'status': 'success', 'hypothesis': text}
                (root / (hashlib.sha256(row['sample_id'].encode()).hexdigest() + '.json')).write_text(json.dumps(record))
            self.records[model], self.hashes[model] = load_outputs(root, self.sha, self.rows, provider, digest(self.annotations))

    def tearDown(self):
        self.temp.cleanup()

    def complete(self):
        self.store.save('R001', payload(transcript='허리 통증'))
        self.store.save('R002', payload(transcript='통증 어깨 통증'))
        return snapshot_db(self.db, self.sha, self.rows, 'reviewer-1')

    def command(self, out, *extra):
        return [sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'scripts/rescore_reviewed.py'),
            '--manifest', str(self.manifest), '--expected-manifest-sha256', self.sha,
            '--review-db', str(self.db), '--reviewer', 'reviewer-1', '--expected-count', '2',
            '--qwen-run', str(self.runs['qwen_1_7b']), '--gpt-run', str(self.runs['gpt_transcribe']),
            '--expected-qwen-sha256', self.hashes['qwen_1_7b'], '--expected-gpt-sha256', self.hashes['gpt_transcribe'],
            '--annotations', str(self.annotations), '--out', str(out), '--repetitions', '20', *extra]

    def test_gate_blocks_missing_draft_flag_and_separates_reviewers(self):
        out = self.root / 'must-not-exist'
        for changes in (None, {'status': 'draft'}, {'status': 'flagged', 'notes': '합성 보류'}):
            if changes:
                version = (self.store.record('R001') or {}).get('version', 0)
                self.store.save('R001', {**payload(version), **changes})
            run = subprocess.run(self.command(out), capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertIn('0/2', run.stdout)
            self.assertFalse(out.exists())
        self.assertFalse(validate_snapshot(snapshot_db(self.db, self.sha, self.rows, 'reviewer-2'), self.sha, self.rows)['ready'])
        with self.assertRaisesRegex(ValueError, 'no database was created'):
            snapshot_db(self.root / 'absent.sqlite3', self.sha, self.rows, 'reviewer-1')
        self.assertFalse((self.root / 'absent.sqlite3').exists())

    def test_corrected_text_changes_both_edit_and_term_denominators(self):
        snapshot = self.complete()
        before_source = self.manifest.read_bytes()
        result, private = rescore(self.rows, snapshot, self.records, self.sha, {}, 100, 7)
        old, new = (result['stages'][s]['models'] for s in ('provided_reference', 'reviewed_reference'))
        self.assertEqual(old['qwen_1_7b']['reference_characters'], 6)
        self.assertEqual(old['qwen_1_7b']['edit_errors'], 0)
        self.assertEqual(old['gpt_transcribe']['edit_errors'], 6)
        self.assertEqual(new['qwen_1_7b']['reference_characters'], 10)
        self.assertEqual(new['qwen_1_7b']['edit_errors'], 6)
        self.assertEqual(new['gpt_transcribe']['edit_errors'], 0)
        self.assertEqual(old['gpt_transcribe']['term_occurrences'], 3)
        self.assertEqual(new['gpt_transcribe']['term_occurrences'], 4)
        self.assertEqual(new['qwen_1_7b']['term_errors'], 2)
        self.assertEqual(new['gpt_transcribe']['term_errors'], 0)
        self.assertAlmostEqual(result['stages']['reviewed_reference']['paired_difference']['cer_difference'], -.6)
        self.assertEqual(result['reference_review']['normalized_text_changed'], 2)
        self.assertFalse(result['reference_review']['consensus_adjudication_complete'])
        self.assertEqual(self.manifest.read_bytes(), before_source)
        self.assertEqual(private['common_success_indices'], [0, 1])
        again, _ = rescore(self.rows, snapshot, self.records, self.sha, {}, 100, 7)
        self.assertEqual(result['stages'], again['stages'])

    def test_common_success_set_and_no_term_zero_denominators(self):
        snapshot = self.complete()
        records = copy.deepcopy(self.records)
        records['qwen_1_7b'][self.rows[0]['sample_id']] = {'status': 'failed'}
        result, _ = rescore(self.rows, snapshot, records, self.sha, {}, 20)
        q, g = (result['stages']['reviewed_reference']['models'][m] for m in ('qwen_1_7b', 'gpt_transcribe'))
        self.assertEqual(q['sentences'], g['sentences'])
        self.assertEqual(q['reference_characters'], 6)
        self.assertEqual(q['technical_failures'], 1)
        self.assertIsNone(q['by_role']['staff']['cer'])
        self.assertIsNone(result['stages']['reviewed_reference']['paired_difference']['cer_descriptive_interval_95'])
        for item in snapshot['items']:
            item['review']['transcript'] = '합성 예문'
        result, _ = rescore(self.rows, snapshot, self.records, self.sha, {}, 20)
        self.assertIsNone(result['stages']['reviewed_reference']['models']['qwen_1_7b']['term_error_rate'])
        self.assertIsNone(result['stages']['reviewed_reference']['paired_difference']['literal_term_descriptive_interval_95'])
        self.assertEqual(manuscript_tables(result)[2][2], [])

    def test_rejects_manifest_audio_reference_order_and_listening_changes(self):
        snapshot = self.complete()
        for field, changed in (('id', 'R002'), ('audio_sha256', '0' * 64), ('provided_reference', '바뀐 자료')):
            bad = copy.deepcopy(snapshot); bad['items'][0][field] = changed
            with self.assertRaisesRegex(ValueError, 'identity/order'):
                validate_snapshot(bad, self.sha, self.rows)
        for field, changed in (('heard', False), ('played_seconds', 0), ('transcript', '...'), ('version', 0)):
            bad = copy.deepcopy(snapshot); bad['items'][0]['review'][field] = changed
            with self.assertRaisesRegex(ValueError, 'completed review'):
                validate_snapshot(bad, self.sha, self.rows)
        with self.assertRaisesRegex(ValueError, 'database/manifest'):
            snapshot_db(self.db, '0' * 64, self.rows, 'reviewer-1')

    def test_cli_end_to_end_private_outputs_aggregate_safety_and_no_overwrite(self):
        self.complete(); out = self.root / 'new-analysis'
        source = self.manifest.read_bytes()
        run = subprocess.run(self.command(out), capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(len(list(out.iterdir())), 7)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        for p in out.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        for name in ('aggregate.json', 'tables.md', 'tables.csv', 'tables.tex', 'methods.md'):
            text = (out / name).read_text()
            for private in ('fixture-0', 'reviewer-1', '허리', 'transcript', 'hypothesis', str(self.root)):
                self.assertNotIn(private, text)
        self.assertIn('허리', (out / 'review-snapshot.json').read_text())
        old = {p.name: digest(p) for p in out.iterdir()}
        retry = subprocess.run(self.command(out), capture_output=True, text=True)
        self.assertEqual(retry.returncode, 2)
        self.assertEqual(old, {p.name: digest(p) for p in out.iterdir()})
        self.assertEqual(source, self.manifest.read_bytes())

    def test_frozen_receipt_and_provider_mismatch_cannot_generate_tables(self):
        self.complete()
        root = self.runs['gpt_transcribe']
        name = hashlib.sha256(self.rows[0]['sample_id'].encode()).hexdigest() + '.json'
        data = json.loads((root / name).read_text()); data['hypothesis'] = '변경된 합성 출력'
        (root / name).write_text(json.dumps(data))
        out = self.root / 'changed-output'
        run = subprocess.run(self.command(out), capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertIn('fingerprint mismatch', run.stdout)
        self.assertFalse(out.exists())
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            load_outputs(root, self.sha, self.rows, 'qwen-local', digest(self.annotations))


if __name__ == '__main__':
    unittest.main()
