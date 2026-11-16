import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from metrics import edit_distance, normalize, strip_nemotron_metadata, summarize
from benchmark import collect_pairs, evaluate, write_private_json


class MetricsTests(unittest.TestCase):
    def test_historical_normalization_and_spelling_label(self):
        self.assertEqual(normalize("(ABC)/(가나) 12!"), "가나12")
        self.assertEqual(normalize("A B-12"), "ab12")

    def test_insertion_deletion_substitution(self):
        self.assertEqual(edit_distance("abc", "ab"), 1)
        self.assertEqual(edit_distance("ab", "abc"), 1)
        self.assertEqual(edit_distance("abc", "axc"), 1)

    def test_corpus_weighting_and_grouping(self):
        result = summarize([("staff", "aaaaaaaaa", "aaaaaaaaa"), ("patient", "b", "c")])
        self.assertEqual(result["cer"], .1)  # Not the mean of per-sentence CERs (.5).
        self.assertEqual(result["cer_by_group"], {"staff": 0, "patient": 1})
        self.assertEqual(result["exact_sentences"], 1)

    def test_empty_reference_cannot_mask_errors(self):
        with self.assertRaises(ValueError):
            summarize([("other", "!!!", "text")])

    def test_metadata_removed_only_at_terminal_position(self):
        self.assertEqual(strip_nemotron_metadata("가나. <ko-KR>"), "가나.")
        self.assertEqual(strip_nemotron_metadata("<ko-KR> 가나"), "<ko-KR> 가나")


class RunnerTests(unittest.TestCase):
    def test_private_result_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.json"
            write_private_json(path, {"cer": .1})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_private_json(path, {"cer": .2})
            self.assertEqual(json.loads(path.read_text()), {"cer": .1})

    def test_synthetic_wav_to_aggregate_without_transcript_output(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "staff_synthetic.wav"
            with wave.open(str(audio), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(bytes(32000))
            audio.with_suffix(".txt").write_text("가나", encoding="utf-8")
            result = evaluate(collect_pairs(directory), lambda _: "가나")
            self.assertEqual(result["cer"], 0)
            self.assertEqual(result["audio_seconds"], 1)
            self.assertNotIn("가나", json.dumps(result, ensure_ascii=False))
            self.assertNotIn("staff_synthetic", json.dumps(result))

    def test_unpaired_input_fails_before_loading_model(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "synthetic.wav").write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "Unpaired"):
                collect_pairs(directory)


class PublishedResultsTests(unittest.TestCase):
    def test_gpu_rerun_publication_matches_execution_manifest(self):
        directory = ROOT / "results" / "2026-11-16"
        data = json.loads((directory / "results.json").read_text())
        manifest = json.loads((directory / "execution-manifest.json").read_text())
        with (directory / "results.csv").open(encoding="utf-8-sig") as stream:
            csv_rows = list(csv.DictReader(stream))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(len(manifest["runs"]), 7)
        self.assertEqual(len(data["results"]), 7)
        self.assertEqual(len(csv_rows), 7)
        for row, csv_row, run in zip(data["results"], csv_rows, manifest["runs"]):
            self.assertEqual(run["exit_code"], 0)
            self.assertEqual(row["model"], run["result"]["model"])
            self.assertEqual(row["edit_errors"], run["result"]["edit_errors"])
            self.assertEqual(row["reference_characters"], 504)
            self.assertEqual(row["sentences"], 36)
            self.assertAlmostEqual(row["cer_percent"], row["edit_errors"] / 504 * 100)
            self.assertEqual(row["exact_sentences"], run["result"]["exact_sentences"])
            for key, value in row.items():
                self.assertEqual(float(csv_row[key]) if isinstance(value, (float, int)) else csv_row[key], value)
        for name, digest in manifest["source_sha256"].items():
            self.assertEqual(hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest(), digest)

    def test_historical_csv_and_json_match(self):
        directory = ROOT / "results" / "2026-11-06"
        data = json.loads((directory / "results.json").read_text())
        with (directory / "results.csv").open(encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 7)
        self.assertEqual(len(data["results"]), 7)
        for csv_row, json_row in zip(rows, data["results"]):
            for key, value in json_row.items():
                if isinstance(value, (float, int)):
                    self.assertEqual(float(csv_row[key]), value)
                else:
                    self.assertEqual(csv_row[key], value)


if __name__ == "__main__":
    unittest.main()
