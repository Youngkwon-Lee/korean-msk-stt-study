from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from prepare_corpus import prepare
from run_manifest import atomic_json, load_manifest, run_samples
from analyze_manifest import paired_summary, read_run
from seal_manifest import seal
from metrics import normalize


def synthetic_row(root, identity='sample-1', reference='어깨 통증'):
    path = root/(identity+'.wav')
    path.write_bytes(b'synthetic-audio')
    return {'sample_id': identity, 'reference': reference,
            'audio_path': str(path), 'audio_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'reference_utf8_sha256': hashlib.sha256(reference.encode()).hexdigest(),
            'text_group': hashlib.sha256(normalize(reference).encode()).hexdigest()}


class CorpusPreparationTests(unittest.TestCase):
    def test_holdout_excludes_development_text_in_every_source_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio, labels = root/'audio', root/'labels'
            audio.mkdir(); labels.mkdir()
            sources = {'HA_0001-1-M': '기존 문장', 'HA_0001-2-F': '새로운 문장',
                       'HA_0001-3-M': '새로운, 문장!', 'PA_0001-1-F': '기존,문장',
                       'PA_0001-2-F': '다른 문장'}
            for name, reference in sources.items():
                (audio/(name+'.wav')).write_bytes(b'synthetic')
                (labels/(name+'.txt')).write_text(reference)
            exclusions = [{'kind': 'labels', 'source_name': 'HA_0001-1-M.txt'}]
            a, summary = prepare(audio, labels, exclusions, 1, 123, hash_audio=True)
            b, _ = prepare(audio, labels, exclusions, 1, 123, hash_audio=True)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 2)
        self.assertTrue(all(normalize(r['reference']) != normalize('기존 문장') for r in a))
        self.assertIsNone(summary['verified_unique_speakers'])
        self.assertEqual(summary['source_strata'], 2)
        self.assertEqual(sum(r['within_stratum_duplicate_text_excluded'] for r in summary['by_source_stratum']), 1)

    def test_incomplete_reference_cache_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'one.wav').write_bytes(b'synthetic')
            (root/'one.txt').write_text('개발 문장')
            with self.assertRaisesRegex(ValueError, 'cache/source'):
                prepare(root, root, [{'kind': 'labels', 'source_name': 'one.txt'}], 0, 1, references=[])


class DurableRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.row = synthetic_row(self.root)
        self.output = self.root/'run'
        self.output.mkdir()
        self.config = {'manifest_sha256': 'synthetic', 'source_sha256': {}}

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, transcribe, **kwargs):
        with redirect_stdout(io.StringIO()):
            return run_samples([self.row], self.output, self.config, transcribe, **kwargs)

    def test_successful_resume_never_calls_provider_twice(self):
        calls = []
        def transcribe(path):
            calls.append(path)
            return {'text': '어깨 통증'}
        self.invoke(transcribe)
        self.invoke(transcribe)
        self.assertEqual(len(calls), 1)
        changed = {**self.config, 'new_setting': True}
        with self.assertRaisesRegex(ValueError, 'settings changed'):
            run_samples([self.row], self.output, changed, transcribe)

    def test_ambiguous_crash_is_not_automatically_repeated(self):
        def crash(path):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(crash)
        calls = []
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous'):
            self.invoke(lambda path: calls.append(path))
        self.assertEqual(calls, [])

    def test_failed_retry_is_explicit_bounded_and_does_not_leak_error_text(self):
        def fail(path):
            raise RuntimeError('synthetic-secret-provider-body')
        with self.assertRaisesRegex(RuntimeError, '^REQUEST_FAILED$'):
            self.invoke(fail)
        self.assertNotIn('synthetic-secret-provider-body', ''.join(p.read_text() for p in self.output.glob('*.json')))
        self.invoke(lambda path: self.fail('unexpected automatic retry'))
        with self.assertRaisesRegex(RuntimeError, '^REQUEST_FAILED$'):
            self.invoke(fail, retry_failed=True)
        receipt = next(p for p in self.output.glob('*.json') if p.name != 'run.json')
        self.assertEqual(json.loads(receipt.read_text())['previous_attempts'][0]['status'], 'failed')
        with self.assertRaisesRegex(RuntimeError, 'retry limit'):
            self.invoke(fail, retry_failed=True)

    def test_audio_mutation_prevents_provider_call(self):
        Path(self.row['audio_path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Audio changed'):
            self.invoke(lambda path: self.fail('unexpected provider call'))

    def test_selection_without_audio_hash_seal_cannot_run(self):
        value = {'schema': 'corpus-manifest/v2', 'summary': {'run_ready_audio_hashes': False}, 'samples': [self.row]}
        path = self.root/'manifest.json'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'not run-ready'):
            load_manifest(path, hashlib.sha256(path.read_bytes()).hexdigest())


class PairedAnalysisTests(unittest.TestCase):
    def test_shared_phrase_groups_stay_together_and_failure_is_not_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [synthetic_row(root, 'one', '어깨'), synthetic_row(root, 'two', '어깨'),
                    synthetic_row(root, 'three', '허리'), synthetic_row(root, 'four', '무릎')]
            a = {r['sample_id']: {'status': 'success', 'hypothesis': r['reference']} for r in rows}
            b = {r['sample_id']: {'status': 'success', 'hypothesis': ''} for r in rows}
            b['four'] = {'status': 'failed'}
            summary = paired_summary(rows, a, b, repetitions=100, seed=7)
        self.assertEqual(summary['technical_failures_b'], 1)
        self.assertEqual(summary['common_success_samples'], 3)
        self.assertEqual(summary['observed_phrase_groups_with_common_success'], 2)
        self.assertEqual(summary['common_success_cer_difference_a_minus_b'], -1)
        self.assertEqual(summary['descriptive_phrase_cluster_interval_95'], [-1, -1])
        self.assertEqual(summary['failure_as_full_reference_deletion_sensitivity_b'], 1)

    def test_missing_run_receipt_cannot_be_published_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = synthetic_row(root)
            atomic_json(root/'run.json', {'manifest_sha256': 'expected'})
            with self.assertRaisesRegex(ValueError, 'missing receipt'):
                read_run(root, 'expected', [row])


class AudioSealTests(unittest.TestCase):
    def test_audio_seal_keeps_selection_and_can_be_verified_by_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = synthetic_row(root)
            with wave.open(row['audio_path'], 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\x00\x00'*1600)
            row.pop('audio_sha256')
            row['audio_bytes'] = Path(row['audio_path']).stat().st_size
            selection = root/'selection.json'
            selection.write_text(json.dumps({'schema': 'corpus-manifest/v2',
                'summary': {'run_ready_audio_hashes': False}, 'samples': [row]}))
            fingerprint = hashlib.sha256(selection.read_bytes()).hexdigest()
            manifest = seal(selection, fingerprint)
            self.assertEqual(manifest['selection_manifest_sha256'], fingerprint)
            self.assertEqual(manifest['samples'][0]['sample_id'], row['sample_id'])
            self.assertAlmostEqual(manifest['summary']['selected_audio_seconds'], .1)
            ready = root/'ready.json'
            ready.write_text(json.dumps(manifest))
            load_manifest(ready, hashlib.sha256(ready.read_bytes()).hexdigest())
            self.assertFalse(json.loads(selection.read_text())['summary']['run_ready_audio_hashes'])

    def test_audio_size_drift_prevents_sealing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = synthetic_row(root)
            row['audio_bytes'] = 1
            path = root/'selection.json'
            path.write_text(json.dumps({'schema': 'corpus-manifest/v2',
                'summary': {'run_ready_audio_hashes': False}, 'samples': [row]}))
            with self.assertRaisesRegex(ValueError, 'size changed'):
                seal(path, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
