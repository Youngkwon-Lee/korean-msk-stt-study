import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from metrics import normalize
from score_expanded import compare


class ExpandedScoringTests(unittest.TestCase):
    def fixture(self,root):
        rows=[{'reference':ref,'text_group':hashlib.sha256(normalize(ref).encode()).hexdigest()}
              for ref in ('어깨','허리','어깨')]
        path=root/'manifest.json'
        path.write_text(json.dumps({'samples':rows,'selection_manifest_sha256':'selection'}))
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        base={'manifest_sha256':sha,'annotation_sha256':'same','aggregate':{}}
        return path,{**base,'aggregate':{'execution':{'provider':'gpt-transcribe'}},'private_vectors':[[0,0,1]]*3},{**base,'aggregate':{'execution':{'provider':'qwen-local'}},'private_vectors':[[2,1,1]]*3}

    def test_paired_phrase_groups_preserve_shared_text_and_signed_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            path,a,b=self.fixture(Path(directory))
            result=compare(path,a,b,repetitions=100)
        self.assertEqual(result['paired']['normalized_phrase_groups'],2)
        self.assertEqual(result['paired']['reference_characters'],6)
        self.assertEqual(result['paired']['cer_difference_a_minus_b'],-1)
        self.assertEqual(result['paired']['descriptive_phrase_interval_95'],[-1,-1])
        self.assertEqual(result['paired']['literal_term_error_rate_difference_a_minus_b'],-1)
        self.assertNotIn('private_vectors',result)
        self.assertIsNone(result['clinical_semantic_errors'])

    def test_missing_rows_or_changed_annotations_cannot_be_paired(self):
        with tempfile.TemporaryDirectory() as directory:
            path,a,b=self.fixture(Path(directory))
            with self.assertRaisesRegex(ValueError,'Missing paired'):
                compare(path,a,{**b,'private_vectors':b['private_vectors'][:-1]})
            with self.assertRaisesRegex(ValueError,'Annotation fingerprint'):
                compare(path,a,{**b,'annotation_sha256':'different'})
            with self.assertRaisesRegex(ValueError,'Model identity/order'):
                compare(path,b,a)
            a['private_vectors'][0]=[None,None,1]
            result=compare(path,a,b,repetitions=20)
            self.assertEqual(result['paired']['technical_failures_a'],1)
            self.assertEqual(result['paired']['common_success_samples'],2)


if __name__=='__main__':unittest.main()
