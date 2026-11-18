#!/usr/bin/env python3
"""Bounded checks on staged/tracked files; NOT a complete PHI detector."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".webm", ".flac", ".ogg",
                      ".nemo", ".safetensors", ".bin", ".pt", ".pth", ".pem", ".key", ".jsonl",
                      ".sqlite3", ".sqlite3-wal", ".sqlite3-shm"}
PATTERNS = {
    "private workspace path": r"/(?:Users|home)/[A-Za-z0-9]",
    "private Drive/operations link": r"https://(?:drive\.google\.com|docs\.google\.com|ops\.kinelo\.co\.kr)/",
    "signed URL parameter": r"[?&](?:X-Amz-Signature|sig|token|signature)=",
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "GitHub credential": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
}


def main():
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    errors = []
    for name in filter(None, names):
        path = ROOT / name
        if path.is_symlink():
            errors.append(f"{name}: symlink")
            continue
        if path.suffix.lower() in BLOCKED_EXTENSIONS or path.name.startswith(".env"):
            errors.append(f"{name}: blocked file type")
        if set(path.relative_to(ROOT).parts) & {"private-data", "private-runs", "raw", "recordings", "signed-forms", "models"}:
            errors.append(f"{name}: private artifact directory")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            errors.append(f"{name}: unexpected non-text artifact")
            continue
        for label, pattern in PATTERNS.items():
            if re.search(pattern, content, flags=re.I):
                errors.append(f"{name}: {label}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Checked {sum(bool(x) for x in names)} tracked text files; no listed publication blockers.")
    print("Manual source/content review is still required; these patterns do not prove absence of PHI.")


if __name__ == "__main__":
    main()
