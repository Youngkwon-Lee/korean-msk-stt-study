import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_suite


class SuiteTests(unittest.TestCase):
    def test_input_fingerprint_detects_reference_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "staff_synthetic.txt"
            path.write_text("가나", encoding="utf-8")
            original = run_suite.input_digest(directory)
            path.write_text("가다", encoding="utf-8")
            self.assertNotEqual(original, run_suite.input_digest(directory))

    def test_busy_card_is_rejected(self):
        for raw in ("GPU, 123, 16384, 4096, 0", "GPU, 123, 16384, 500, 80"):
            with patch.object(run_suite.subprocess, "check_output", return_value=raw):
                with self.assertRaisesRegex(RuntimeError, "GPU busy"):
                    run_suite.gpu_snapshot(1)

    def test_sequential_suite_holds_lock_and_saves_aggregates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            wav = data / "staff_synthetic.wav"
            with wave.open(str(wav), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(bytes(32000))
            wav.with_suffix(".txt").write_text("가나", encoding="utf-8")
            lock = root / "slot.lock"
            lock.touch()
            config = root / "private-config.json"
            config.write_text(json.dumps({
                "expected_input_sha256": run_suite.input_digest(data),
                "models": [{"family": "whisper", "label": "synthetic", "python": sys.executable,
                            "path": str(root), "vad_conditions": [False, True]}]}))

            def inference(command, **kwargs):
                with lock.open("r+") as another:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(another, fcntl.LOCK_EX | fcntl.LOCK_NB)
                target = Path(command[command.index("--out") + 1])
                target.write_text(json.dumps({"sentences": 1, "reference_characters": 2, "cer": 0}))
                return SimpleNamespace(returncode=0)

            output = root / "new-run"
            argv = ["run_suite", "--config", str(config), "--data", str(data), "--out", str(output),
                    "--gpu", "1", "--lock", str(lock)]
            previous = os.umask(0o077)
            try:
                with patch.object(sys, "argv", argv), \
                     patch.object(run_suite, "gpu_snapshot", return_value={"used_mib": 500}), \
                     patch.object(run_suite, "runtime_versions", return_value={}), \
                     patch.object(run_suite, "model_artifacts", return_value=[]), \
                     patch.object(run_suite.subprocess, "run", side_effect=inference):
                    run_suite.main()
            finally:
                os.umask(previous)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(len(manifest["runs"]), 2)
            self.assertEqual((output / "manifest.json").stat().st_mode & 0o777, 0o600)
            self.assertNotIn("가나", json.dumps(manifest, ensure_ascii=False))
            with lock.open("r+") as released:
                fcntl.flock(released, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()
