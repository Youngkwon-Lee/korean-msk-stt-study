#!/usr/bin/env python3
"""Run existing local ASR environments sequentially under an existing GPU lock.

The config contains private paths and must stay outside the public repository.
Outputs are private until reviewed. Logs may contain third-party diagnostics.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from benchmark import collect_pairs
from metrics import normalize


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def input_digest(directory):
    digest = hashlib.sha256()
    files = sorted(p for p in Path(directory).iterdir() if p.suffix in (".wav", ".txt"))
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def gpu_snapshot(gpu):
    raw = subprocess.check_output([
        "nvidia-smi", "-i", str(gpu),
        "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
        "--format=csv,noheader,nounits"], text=True, timeout=10)
    name, driver, total, used, utilization = [part.strip() for part in raw.strip().split(",")]
    result = {"name": name, "driver": driver, "total_mib": int(total),
              "used_mib": int(used), "utilization_percent": int(utilization)}
    # Admission only; whole-card samples include desktop and other processes.
    if result["used_mib"] > 2048 or result["utilization_percent"] > 10:
        raise RuntimeError("GPU busy; preserve the existing work")
    return result


def runtime_versions(python, env):
    code = """import importlib.metadata as m, json, platform
names = ['torch','torchaudio','transformers','qwen-asr','faster-whisper',
         'ctranslate2','nemo_toolkit','omegaconf','numpy','scipy',
         'nvidia-cudnn-cu12','nvidia-cublas-cu12']
versions = {}
for name in names:
    try: versions[name] = m.version(name)
    except m.PackageNotFoundError: pass
print(json.dumps({'python':platform.python_version(),'packages':versions}))
"""
    return json.loads(subprocess.check_output([python, "-B", "-c", code], env=env, text=True, timeout=60))


def model_environment(model):
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               HF_HUB_DISABLE_IMPLICIT_TOKEN="1", PYTHONDONTWRITEBYTECODE="1")
    if model.get("library_dirs"):
        env["LD_LIBRARY_PATH"] = ":".join(model["library_dirs"])
    if model.get("python_paths"):
        env["PYTHONPATH"] = ":".join(model["python_paths"])
    return env


def model_artifacts(path):
    path = Path(path)
    files = [path] if path.is_file() else sorted(
        p for p in path.iterdir() if p.is_file() and not p.name.startswith("."))
    return [{"file": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)} for p in files]


def now():
    return datetime.now(timezone.utc).isoformat()


def save_manifest(directory, manifest):
    temporary = directory / "manifest.pending"
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(directory / "manifest.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Private JSON containing existing model/runtime paths")
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True, help="New private directory, never reused")
    parser.add_argument("--gpu", type=int, required=True)
    parser.add_argument("--lock", required=True, help="Existing shared per-GPU lock; never forced")
    args = parser.parse_args()
    if args.gpu < 0:
        parser.error("GPU index must be nonnegative")
    os.umask(0o077)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    pairs = collect_pairs(args.data)
    fingerprint = input_digest(args.data)
    if fingerprint != config["expected_input_sha256"]:
        raise ValueError("Input fingerprint mismatch")
    # Opening an existing lock does not create a new scheduling mechanism.
    fd = os.open(args.lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gpu = gpu_snapshot(args.gpu)
        output = Path(args.out)
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        scripts = Path(__file__).resolve().parent
        manifest = {
            "started_at": now(), "status": "running", "physical_gpu": args.gpu,
            "initial_gpu": gpu, "input_sha256": fingerprint,
            "input_hash_algorithm": "sorted WAV/TXT filename UTF8 + binary SHA256(file), then SHA256",
            "sample_pairs": len(pairs), "audio_seconds": sum(p[3] for p in pairs),
            "reference_characters": sum(len(normalize(p[2])) for p in pairs),
            "source_sha256": {name: sha256(scripts / name) for name in
                              ("benchmark.py", "metrics.py", "model_backends.py", "run_suite.py")},
            "runtimes": {}, "model_artifacts": {}, "runs": []}
        save_manifest(output, manifest)
        try:
            for model in config["models"]:
                family, label = model["family"], model["label"]
                env = model_environment(model)
                manifest["runtimes"][family] = runtime_versions(model["python"], env)
                manifest["model_artifacts"][label] = model_artifacts(model["path"])
                save_manifest(output, manifest)
                for vad in model.get("vad_conditions", [False]):
                    preflight = gpu_snapshot(args.gpu)
                    index = len(manifest["runs"]) + 1
                    target = output / f"result-{index:02d}.json"
                    command = [model["python"], "-B", str(scripts / "benchmark.py"),
                               "--data", args.data, "--model", model["path"],
                               "--family", family, "--label", label, "--gpu", str(args.gpu),
                               "--out", str(target)]
                    if vad:
                        command.append("--vad")
                    entry = {"model": label, "vad_requested": bool(vad),
                             "started_at": now(), "gpu_preflight": preflight}
                    print(json.dumps({"event": "start", "index": index, "model": label, "vad": vad}), flush=True)
                    with (output / f"private-log-{index:02d}.txt").open("x") as log:
                        process = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                                 timeout=1200, check=False)
                    entry.update(finished_at=now(), exit_code=process.returncode)
                    if process.returncode == 0:
                        result = json.loads(target.read_text(encoding="utf-8"))
                        if result["sentences"] != len(pairs) or result["reference_characters"] != manifest["reference_characters"]:
                            raise ValueError("Result sample count mismatch")
                        entry["result"] = result
                    manifest["runs"].append(entry)
                    save_manifest(output, manifest)
                    print(json.dumps({"event": "finished", "index": index, "exit_code": process.returncode}), flush=True)
                    if process.returncode:
                        raise RuntimeError("Model failed; inspect its private log before retrying")
            if input_digest(args.data) != fingerprint:
                raise ValueError("Inputs changed during inference")
            manifest["status"] = "complete"
        except BaseException:
            manifest["status"] = "incomplete"
            raise
        finally:
            manifest["finished_at"] = now()
            save_manifest(output, manifest)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
