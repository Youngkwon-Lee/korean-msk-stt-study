import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from compare_api import api_transcribe, latency_summary


class ApiComparisonTests(unittest.TestCase):
    def test_gpt_request_uses_plural_language_without_answers_or_legacy_parameters(self):
        observed = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"text":"synthetic","model":"gpt-transcribe"}'
        class Opener:
            def open(self, request, timeout):
                observed.append(request)
                return Response()
        with tempfile.TemporaryDirectory() as directory, patch('compare_api.urllib.request.build_opener', return_value=Opener()):
            path = Path(directory) / 'sample.wav'
            path.write_bytes(b'synthetic-media')
            result = api_transcribe(path, 'synthetic-key', 'gpt-transcribe')
        body = observed[0].data
        self.assertIn(b'name="languages[]"\r\n\r\nko', body)
        for field in ('language', 'temperature', 'prompt', 'keywords'):
            self.assertNotIn(f'name="{field}"'.encode(), body)
        self.assertEqual(result, {'text': 'synthetic', 'response_model': 'gpt-transcribe'})

    def test_latency_uses_all_requests_and_nearest_rank_p95(self):
        result = latency_summary(list(range(1, 37)))
        self.assertEqual(result['requests'], 36)
        self.assertEqual(result['median_seconds'], 18.5)
        self.assertEqual(result['p95_seconds'], 35)
        self.assertEqual(result['total_seconds'], 666)


if __name__ == '__main__':
    unittest.main()
