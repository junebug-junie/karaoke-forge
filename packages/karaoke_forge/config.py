from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(os.getenv("KARAOKE_FORGE_ROOT", Path.cwd())).resolve()
LIBRARY_DIR = Path(os.getenv("KARAOKE_FORGE_LIBRARY", ROOT_DIR / "library")).resolve()
SONGS_DIR = LIBRARY_DIR / "songs"
JOBS_DIR = LIBRARY_DIR / "jobs"
RENDERS_DIR = LIBRARY_DIR / "renders"
DB_PATH = Path(os.getenv("KARAOKE_FORGE_DB", LIBRARY_DIR / "karaoke_forge.sqlite3")).resolve()

PUBLIC_BASE_PATH = os.getenv("KARAOKE_FORGE_BASE_PATH", "").strip()
if PUBLIC_BASE_PATH and not PUBLIC_BASE_PATH.startswith("/"):
    PUBLIC_BASE_PATH = "/" + PUBLIC_BASE_PATH
PUBLIC_BASE_PATH = PUBLIC_BASE_PATH.rstrip("/")

KARAOKE_GEN_BIN = os.getenv("KARAOKE_GEN_BIN", "karaoke-gen")
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "medium")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
ENABLE_LOCAL_WHISPER = os.getenv("ENABLE_LOCAL_WHISPER", "true")


def ensure_library_dirs() -> None:
    for path in (LIBRARY_DIR, SONGS_DIR, JOBS_DIR, RENDERS_DIR):
        path.mkdir(parents=True, exist_ok=True)
