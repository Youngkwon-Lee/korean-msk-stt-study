"""Synthetic-only proof of gated STT reference release; no clinical data."""
from contextlib import redirect_stdout
from datetime import date, timedelta
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_app import Dataset, Store
from review_release_gate import (collect_review_state, load_approval, release_decisions,
                                 local_evaluation_bundle, main)
from test_review_app import fixture, payload


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest, self.sha, self.audio = fixture(self.root)
        source = json.loads(self.manifest.read_text())
        for row in source["samples"]:
            row["group"] = "staff"
        self.manifest.write_text(json.dumps(source))
        self.sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.dataset = Dataset(self.manifest, self.sha, self.audio, 2)
        self.db = self.root / 'reviews.sqlite3'
        self.first = Store(self.db, self.dataset, 'reviewer-1')
        self.second = Store(self.db, self.dataset, 'reviewer-2')
        self.private = self.root / 'private-runs'
        self.private.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def state(self, both=True):
        return collect_review_state(self.manifest, self.sha, self.db, 'reviewer-1',
                                    'reviewer-2' if both else None, expected_count=2)

    def complete(self):
        for key in ('R001', 'R002'):
            text = '합성 정답 ' + key
            self.first.save(key, payload(transcript=text))
            self.second.save(key, payload(transcript=text))

    def approval(self, state, **changes):
        value = {
            'schema': 'review-release-approval/v1',
            'approval_id': 'test-internal-01', 'approved_by_role': 'data-steward',
            'evidence_record_id': 'private-evidence-01',
            'approved_on': date.today().isoformat(),
            'expires_on': (date.today() + timedelta(days=1)).isoformat(),
            'manifest_sha256': self.sha,
            'review_state_sha256': state['report']['review_state_sha256'],
            'source_reuse_rights_verified': True,
            'privacy_review_passed': True,
            'reviewer_independence_verified': True,
            'expert_qa_signed_off': True,
            'external_processor_contract_verified': False,
            'model_training_rights_verified': False,
            'scopes': {'local_evaluation': True, 'external_review': False, 'model_training': False},
            **changes,
        }
        path = self.root / 'approval.json'
        path.write_text(json.dumps(value))
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_missing_reviews_or_approval_fail_closed_and_sanitized(self):
        state = self.state()
        report = state['report']
        self.assertEqual(report['first_review']['reviewed'], 0)
        self.assertEqual(report['both_reviewed'], 0)
        self.assertEqual(report['exact_agreements'], 0)
        decisions = release_decisions(state, None)
        self.assertFalse(decisions['local_evaluation']['ready'])
        self.assertIn('approval_missing', decisions['local_evaluation']['blockers'])
        for raw in ('합성 음성', 'fixture-0', '합성 표기'):
            self.assertNotIn(raw, json.dumps(report, ensure_ascii=False))
        output = io.StringIO()
        with redirect_stdout(output):
            rc = main(['--manifest', str(self.manifest), '--expected-manifest-sha256', self.sha,
                       '--review-db', str(self.db), '--reviewer', 'reviewer-1',
                       '--expected-count', '2'])
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(output.getvalue())['purposes']['local_evaluation']['ready'])
        self.assertEqual(len(list(self.private.iterdir())), 0)

    def test_completed_dual_consensus_requires_exact_pinned_attestation(self):
        self.complete()
        state = self.state()
        self.assertEqual(state['report']['exact_agreements'], 2)
        path, sha = self.approval(state)
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            load_approval(path, 'f' * 64, self.sha, state['report']['review_state_sha256'])
        a = load_approval(path, sha, self.sha, state['report']['review_state_sha256'])
        decision = release_decisions(state, a)
        self.assertTrue(decision['local_evaluation']['ready'])
        self.assertFalse(decision['external_review']['ready'])
        self.assertFalse(decision['model_training']['ready'])
        dest = self.private / 'qa-approved'
        evidence = local_evaluation_bundle(state, sha, dest, self.private)
        self.assertEqual(evidence['count'], 2)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o700)
        for name in ('references.jsonl', 'release-manifest.json'):
            self.assertEqual(stat.S_IMODE((dest / name).stat().st_mode), 0o600)
        items = [json.loads(line) for line in (dest / 'references.jsonl').read_text().splitlines()]
        self.assertEqual([item['reference'] for item in items], ['합성 정답 R001', '합성 정답 R002'])
        self.assertFalse(any('audio_path' in item for item in items))
        self.assertFalse(any('sample_id' in item for item in items))
        self.assertEqual(hashlib.sha256((dest / 'references.jsonl').read_bytes()).hexdigest(), evidence['references_sha256'])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            local_evaluation_bundle(state, sha, dest, self.private)

    def test_cli_end_to_end_approved_local_export_only(self):
        self.complete()
        state = self.state()
        path, sha = self.approval(state)
        dest = self.private / 'cli-evaluation'
        args = ['--manifest', str(self.manifest), '--expected-manifest-sha256', self.sha,
                '--review-db', str(self.db), '--reviewer', 'reviewer-1',
                '--second-reviewer', 'reviewer-2', '--expected-count', '2',
                '--approval-file', str(path), '--expected-approval-sha256', sha,
                '--private-out', str(dest), '--purpose', 'local_evaluation',
                '--audio-root', str(self.audio)]
        capture = io.StringIO()
        with redirect_stdout(capture):
            rc = main(args, private_root=self.private)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(capture.getvalue())['release']['count'], 2)
        self.assertTrue((dest / 'references.jsonl').exists())
        capture = io.StringIO()
        with redirect_stdout(capture):
            rc2 = main(args, private_root=self.private)
        self.assertEqual(rc2, 2)
        self.assertNotIn('합성 정답', capture.getvalue())

    def test_audio_changed_after_review_blocks_release(self):
        self.complete()
        state = self.state()
        path, sha = self.approval(state)
        (self.audio / 'fixture-0.wav').write_bytes(b'tampered')
        dest = self.private / 'failed-integrity'
        args = ['--manifest', str(self.manifest), '--expected-manifest-sha256', self.sha,
                '--review-db', str(self.db), '--reviewer', 'reviewer-1',
                '--second-reviewer', 'reviewer-2', '--expected-count', '2',
                '--approval-file', str(path), '--expected-approval-sha256', sha,
                '--private-out', str(dest), '--purpose', 'local_evaluation',
                '--audio-root', str(self.audio)]
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(args, private_root=self.private)
        self.assertEqual(code, 2)
        self.assertFalse(dest.exists())
        self.assertNotIn('fixture-0', output.getvalue())

    def test_disagreement_and_change_after_signoff_are_blocked(self):
        self.complete()
        clean = self.state()
        approval, sha = self.approval(clean)
        self.second.save('R002', payload(1, transcript='서로 다른 합성 정답'))
        changed = self.state()
        self.assertEqual(changed['report']['disagreements'], 1)
        self.assertFalse(release_decisions(changed, None)['local_evaluation']['ready'])
        with self.assertRaisesRegex(ValueError, 'current frozen'):
            load_approval(approval, sha, self.sha, changed['report']['review_state_sha256'])

    def test_unapproved_or_expired_rights_cannot_release(self):
        self.complete()
        state = self.state()
        path, sha = self.approval(state, source_reuse_rights_verified=False)
        a = load_approval(path, sha, self.sha, state['report']['review_state_sha256'])
        self.assertIn('source_reuse_rights_verified_missing', release_decisions(state, a)['local_evaluation']['blockers'])
        path, sha = self.approval(state, expires_on=(date.today() - timedelta(days=1)).isoformat())
        with self.assertRaisesRegex(ValueError, 'not currently valid'):
            load_approval(path, sha, self.sha, state['report']['review_state_sha256'])
        with self.assertRaisesRegex(ValueError, 'Independent reviewers'):
            collect_review_state(self.manifest, self.sha, self.db, 'reviewer-1', 'reviewer-1', 2)
        path, sha = self.approval(state, scopes={'local_evaluation': True, 'external_review': True, 'model_training': True},
                                  external_processor_contract_verified=True, model_training_rights_verified=True)
        a = load_approval(path, sha, self.sha, state['report']['review_state_sha256'])
        self.assertFalse(release_decisions(state, a)['external_review']['ready'])
        self.assertFalse(release_decisions(state, a)['model_training']['ready'])

    def test_private_only_and_no_implicit_external_release(self):
        self.complete()
        state = self.state()
        path, sha = self.approval(state)
        for dest in (self.root / 'outside', self.private / 'notcreated' / 'inner'):
            with self.assertRaisesRegex(ValueError, 'private-runs|parent directory'):
                local_evaluation_bundle(state, sha, dest, self.private)
            self.assertFalse(dest.exists())
        output = io.StringIO()
        with redirect_stdout(output):
            rc = main(['--manifest', str(self.manifest), '--expected-manifest-sha256', self.sha,
                       '--review-db', str(self.db), '--reviewer', 'reviewer-1',
                       '--second-reviewer', 'reviewer-2', '--expected-count', '2',
                       '--approval-file', str(path), '--expected-approval-sha256', sha,
                       '--purpose', 'external_review', '--private-out', str(self.private / 'leak')])
        self.assertEqual(rc, 2)
        self.assertFalse((self.private / 'leak').exists())
        self.assertNotIn('합성 정답', output.getvalue())


if __name__ == '__main__':
    unittest.main()
