from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from run_expanded import phase_api, phase_batches, receipt_path


class ExpandedExecutionTests(unittest.TestCase):
    def test_parallel_api_checkpoints_each_sample_and_resume_does_not_recall(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);out=root/'run';rows=[];calls=[]
            for i in range(7):
                path=root/f'synthetic-{i}.wav';path.write_bytes(bytes([i]))
                rows.append({'sample_id':str(i),'audio_path':str(path),'audio_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            def transcribe(path):
                calls.append(path)
                return {'text':'synthetic'}
            config={'manifest_sha256':'synthetic'}
            phase_api(rows,out,config,transcribe,4)
            phase_api(rows,out,config,transcribe,4)
            self.assertEqual(len(calls),7)
            self.assertTrue(all(json.loads(receipt_path(out,r).read_text())['status']=='success' for r in rows))

    def test_batched_local_outputs_preserve_order_and_bad_cardinality_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);rows=[]
            for i in range(5):
                path=root/f'synthetic-{i}.wav';path.write_bytes(bytes([i]))
                rows.append({'sample_id':str(i),'audio_path':str(path),'audio_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            out=root/'run';config={'manifest_sha256':'synthetic'}
            phase_batches(rows,out,config,lambda paths:[p.stem for p in paths],4)
            self.assertEqual([json.loads(receipt_path(out,r).read_text())['hypothesis'] for r in rows],[f'synthetic-{i}' for i in range(5)])
            phase_batches(rows,out,config,lambda paths:self.fail('Unexpected repeat inference'),4)
            failed=root/'failed'
            with self.assertRaisesRegex(RuntimeError,'BATCH_FAILED'):
                phase_batches(rows,failed,config,lambda paths:[],4)
            self.assertEqual(json.loads(receipt_path(failed,rows[0]).read_text())['status'],'failed')


if __name__=='__main__':unittest.main()
