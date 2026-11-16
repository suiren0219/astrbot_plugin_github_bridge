from __future__ import annotations

import argparse
import hashlib
import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT.parent / "astrbot_plugin_github_bridge.zip"
EXCLUDE_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".github",
    ".git",
    ".local",
    ".secrets",
    ".venv",
    "dist",
    "tests",
    "tools",
    "web",
}
EXCLUDE_NAMES = {"settings.json", "panel.auth.json", "requirements.md", ".gitkeep"}
EXCLUDE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".zip",
    ".log",
    ".db",
    ".db-shm",
    ".db-wal",
}


def files() -> list[Path]:
    result = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if (
            set(relative.parts) & EXCLUDE_PARTS
            or path.name in EXCLUDE_NAMES
            or path.name.startswith("settings.before-preset-cleanup.")
            or path.name.startswith(".env")
            or path.is_symlink()
            or path.suffix in EXCLUDE_SUFFIXES
        ):
            continue
        result.append(path)
    return sorted(result)


def build(output: Path) -> tuple[int, str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files():
                archive.writestr(path.relative_to(ROOT).as_posix(), path.read_bytes())
        payload = buffer.getvalue()
    output.write_bytes(payload)
    return len(payload), hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    size, digest = build(args.output)
    print(f"created {args.output} ({size} bytes) sha256={digest}")
