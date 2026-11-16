#!/usr/bin/env python3
"""Offline aggregate-only ASR benchmark for locally authorized paired WAV/TXT files."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import wave

from metrics import normalize, summarize


def collect_pairs(directory):
    files = sorted(Path(directory).glob("*.wav"))
    if not files:
        raise ValueError("No WAV inputs")
    result = []
    for wav in files:
        txt = wav.with_suffix(".txt")
        if not txt.is_file():
            raise ValueError("Unpaired WAV input")
        reference = txt.read_text(encoding="utf-8").strip()
        if not normalize(reference):
            raise ValueError("Reference is empty after normalization")
        with wave.open(str(wav)) as audio:
            seconds = audio.getnframes() / audio.getframerate()
        prefix = wav.stem.split("_", 1)[0]
        group = prefix if prefix in ("staff", "patient") else "other"
        result.append((wav, group, reference, seconds))
    return result


class GpuSampler:
    """One-second samples of whole physical GPU memory; not per-process memory."""
    def __init__(self, gpu):
        self.gpu = gpu
        self.peak = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self):
        while not self.stop.is_set():
            try:
                output = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits",
                     "-i", self.gpu], text=True, timeout=3, stderr=subprocess.DEVNULL)
                value = int(output.strip())
                self.peak = max(self.peak or 0, value)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            self.stop.wait(1)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=4)


def evaluate(pairs, transcribe):
    start = time.monotonic()
    scored = []
    for wav, group, reference, seconds in pairs:
        hypothesis = transcribe(wav)
        if not isinstance(hypothesis, str):
            raise TypeError("Backend returned a non-text result")
        scored.append((group, reference, hypothesis))
    elapsed = time.monotonic() - start
    summary = summarize(scored)
    seconds = sum(pair[3] for pair in pairs)
    summary.update(audio_seconds=seconds, transcribe_seconds=elapsed,
                   audio_seconds_per_processing_second=seconds / max(elapsed, .001))
    return summary


def write_private_json(path, data):
    serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(serialized)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, help="Authorized private WAV/TXT directory")
    parser.add_argument("--model", required=True, help="Existing local model/checkpoint path")
    parser.add_argument("--family", required=True, choices=["whisper", "qwen", "nemotron"])
    parser.add_argument("--label", required=True, help="Public-safe model label, no paths or IDs")
    parser.add_argument("--out", required=True, help="New private aggregate JSON, never overwritten")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    parser.add_argument("--gpu", default="0", help="Physical device after a separate ownership/capacity check")
    parser.add_argument("--vad", action="store_true", help="Whisper only")
    args = parser.parse_args()
    if args.vad and args.family != "whisper":
        parser.error("--vad is implemented only for Whisper")
    if not args.gpu.isdecimal():
        parser.error("--gpu must be a physical device index")
    if Path(args.out).exists():
        parser.error("Output already exists")
    os.umask(0o077)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
    if args.device == "cuda":
        os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    pairs = collect_pairs(args.data)
    from model_backends import load_backend
    with GpuSampler(args.gpu) as sampler:
        start = time.monotonic()
        transcribe = load_backend(args.family, args.model, args.device, args.vad)
        load_seconds = time.monotonic() - start
        result = evaluate(pairs, transcribe)
    result.update(model=args.label, family=args.family, load_seconds=load_seconds,
                  vad=("on" if args.vad else "off") if args.family == "whisper" else "not_controlled",
                  sampled_gpu_memory_peak_mib=sampler.peak,
                  output_metadata_removed="terminal <ko-KR>" if args.family == "nemotron" else None)
    write_private_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
