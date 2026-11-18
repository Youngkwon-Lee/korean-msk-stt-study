import hashlib
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from review_app import Dataset, Store, ReviewError, byte_range, make_server

CANARY = 'PRIVATE_MODEL_OUTPUT_MUST_NEVER_LEAK'


def fixture(root):
    audio = root/'audio'; audio.mkdir()
    rows = []
    for number in range(3):
        path = audio/f'fixture-{number}.wav'
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0'*3200)
        reference = ['합성 음성 정답입니다.', '왼쪽 어깨를 확인합니다.', '<script>합성 표기</script>'][number]
        rows.append({'sample_id':path.stem, 'reference_review_substudy':number < 2,
            'reference':reference, 'reference_utf8_sha256':hashlib.sha256(reference.encode()).hexdigest(),
            'audio_sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'audio_seconds':.2,
            'source_code':CANARY, 'hypothesis':CANARY})
    manifest = root/'manifest.json'
    manifest.write_text(json.dumps({'schema':'corpus-manifest/v2','summary':{'run_ready_audio_hashes':True},'samples':rows}))
    sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    return manifest, sha, audio


def payload(version=0, status='reviewed', transcript='수정한 합성 정답입니다.'):
    return {'version':version,'status':status,'transcript':transcript,'notes':'','heard':True,'played_seconds':.2}


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.manifest,self.sha,self.audio = fixture(self.root)
        self.dataset = Dataset(self.manifest,self.sha,self.audio,2)
        self.db = self.root/'private'/'reviews.sqlite3'
        self.store = Store(self.db,self.dataset,'reviewer-1')

    def tearDown(self):
        self.temp.cleanup()

    def test_frozen_subset_hashes_and_unselected_audio(self):
        self.assertEqual(list(self.dataset.items),['R001','R002'])
        with self.assertRaisesRegex(ValueError,'fingerprint'):
            Dataset(self.manifest,'wrong',self.audio,2)
        (self.audio/'fixture-0.wav').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'audio fingerprint'):
            Dataset(self.manifest,self.sha,self.audio,2)

    def test_save_restart_audit_and_empty_draft_preserve_source(self):
        original = self.manifest.read_bytes()
        self.store.save('R001',payload())
        reopened = Store(self.db,self.dataset,'reviewer-1')
        self.assertEqual(reopened.record('R001')['transcript'],'수정한 합성 정답입니다.')
        self.assertEqual(reopened.summary()['reviewed'],1)
        reopened.save('R001',payload(1,'draft',''))
        self.assertEqual(reopened.record('R001')['transcript'],'')
        self.assertEqual(reopened.summary()['reviewed'],0)
        self.assertEqual(len(reopened.export()['revisions']),2)
        self.assertEqual(self.manifest.read_bytes(),original)
        self.assertEqual(self.db.stat().st_mode & 0o777,0o600)

    def test_two_reviewers_are_independent_and_conflicts_keep_existing_record(self):
        self.store.save('R001',payload())
        other = Store(self.db,self.dataset,'reviewer-2')
        self.assertIsNone(other.record('R001'))
        self.assertEqual(other.export()['revisions'],[])
        with self.assertRaises(ReviewError) as caught:
            self.store.save('R001',payload(0,transcript='충돌 입력'))
        self.assertEqual(caught.exception.status,409)
        self.assertEqual(self.store.record('R001')['version'],1)
        self.assertEqual(len(self.store.export()['revisions']),1)

    def test_completion_requires_explicit_listening_and_flags_require_reason(self):
        for changes in ({'heard':False},{'played_seconds':0},{'transcript':''},{'played_seconds':float('nan')},{'status':'flagged'}):
            with self.assertRaises(ReviewError):
                self.store.save('R001',{**payload(),**changes})
        self.assertIsNone(self.store.record('R001'))

    def test_foreign_or_different_dataset_database_is_preserved(self):
        path=self.root/'foreign.sqlite3'
        with closing(sqlite3.connect(path)) as db:db.execute('CREATE TABLE valuable (value TEXT)')
        original=path.read_bytes()
        with self.assertRaisesRegex(ValueError,'Unrecognized'):
            Store(path,self.dataset,'reviewer-1')
        self.assertEqual(path.read_bytes(),original)
        self.dataset.fingerprint='different'
        with self.assertRaisesRegex(ValueError,'different frozen'):
            Store(self.db,self.dataset,'reviewer-1')


class ReviewHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        manifest,sha,audio=fixture(self.root)
        self.dataset=Dataset(manifest,sha,audio,2)
        self.store=Store(self.root/'reviews.sqlite3',self.dataset,'reviewer-1')
        self.server=make_server(self.dataset,self.store,0,'stt-review-qa.localhost')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def request(self,path,method='GET',body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        payload=json.dumps(body) if body is not None else None
        conn.request(method,path,payload,headers or {})
        response=conn.getresponse();data=response.read();result=response.status,dict(response.getheaders()),data
        conn.close();return result

    def test_api_audio_seek_export_never_return_model_output_or_other_files(self):
        _,_,data=self.request('/api/session');session=json.loads(data)
        self.assertEqual(session['total'],2)
        for path in ('/api/session','/api/items/R001','/api/export'):
            status,headers,data=self.request(path)
            self.assertEqual(status,200)
            self.assertNotIn(CANARY.encode(),data)
            self.assertNotIn(b'hypothesis',data)
            self.assertEqual(headers['Cache-Control'],'no-store')
        status,headers,data=self.request('/audio/R001',headers={'Range':'bytes=0-43'})
        self.assertEqual(status,206);self.assertEqual(data[:4],b'RIFF');self.assertEqual(len(data),44)
        status,_,_=self.request('/audio/R001',headers={'Range':'bytes=999999-'})
        self.assertEqual(status,416)
        for path in ('/audio/fixture-2.wav','/api/items/R003','/../manifest.json','/results.json'):
            self.assertEqual(self.request(path)[0],404)
        token={'Content-Type':'application/json','X-Review-Token':session['csrf_token']}
        self.assertEqual(self.request('/api/items/R001','POST',payload(),token)[0],200)
        self.assertEqual(json.loads(self.request('/api/items/R001')[2])['review']['version'],1)

    def test_cross_origin_and_missing_token_cannot_mutate(self):
        token=json.loads(self.request('/api/session')[2])['csrf_token']
        for headers in ({'Content-Type':'application/json'},
                        {'Content-Type':'application/json','X-Review-Token':token,'Origin':'https://other.invalid'},
                        {'Host':'other.invalid','Content-Type':'application/json','X-Review-Token':token}):
            self.assertEqual(self.request('/api/items/R001','POST',payload(),headers)[0],403)
        self.assertIsNone(self.store.record('R001'))

    def test_single_byte_ranges_are_exact(self):
        self.assertEqual(byte_range('bytes=-4',10),(6,9,206))
        self.assertEqual(byte_range('bytes=0-99',10),(0,9,206))
        for header in ('bytes=-0','bytes=-','bytes=6-2','bytes=0-1,4-5'):
            with self.assertRaises(ReviewError):byte_range(header,10)


if __name__=='__main__':unittest.main()
