from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def _initial_root_dir() -> Path:
    return Path(os.getenv("KARAOKE_FORGE_ROOT", Path.cwd())).resolve()


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


ROOT_DIR = _initial_root_dir()
ENV_LOCAL_PATH = ROOT_DIR / ".env.local"
ENV_EXAMPLE_PATH = ROOT_DIR / ".env.example"

# Load local machine configuration before reading any derived settings.
# Existing real environment variables still win over values in .env.local.
load_dotenv(ENV_LOCAL_PATH, override=False)

# KARAOKE_FORGE_ROOT may itself be defined in .env.local, so resolve it once more
# after loading the file.
ROOT_DIR = _initial_root_dir()
ENV_LOCAL_PATH = ROOT_DIR / ".env.local"
ENV_EXAMPLE_PATH = ROOT_DIR / ".env.example"

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

# whisper-timestamped uses the OpenAI Whisper loader, so the supported name is
# large-v3, not the Hugging Face identifier openai/whisper-large-v3.
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "large-v3")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
ENABLE_LOCAL_WHISPER = os.getenv("ENABLE_LOCAL_WHISPER", "true")
SPACY_MODEL = os.getenv("SPACY_MODEL", "en_core_web_md")

# Indie-karaoke-safe defaults. Clean instrumental avoids unstable lead-vocal
# leakage from backing-vocal stems, and +500ms compensates for highlights that
# tend to fire slightly early after Whisper alignment.
DEFAULT_INSTRUMENTAL_SELECTION = os.getenv("KARAOKE_DEFAULT_INSTRUMENTAL_SELECTION", "clean").strip() or "clean"
DEFAULT_SUBTITLE_OFFSET_MS = _int_env("KARAOKE_DEFAULT_SUBTITLE_OFFSET_MS", 500)

# Optional vocal-stem envelope refinement for per-word timings at review complete.
ENABLE_VOCAL_TIMING_REFINE = _bool_env("KARAOKE_FORGE_VOCAL_TIMING", False)
VOCAL_TIMING_FRAME_MS = _int_env("KARAOKE_FORGE_VOCAL_TIMING_FRAME_MS", 50)
VOCAL_TIMING_SNAP_MS = _int_env("KARAOKE_FORGE_VOCAL_TIMING_SNAP_MS", 150)
VOCAL_TIMING_MAX_DRIFT_MS = _int_env("KARAOKE_FORGE_VOCAL_TIMING_MAX_DRIFT_MS", 200)
VOCAL_TIMING_SNAP_SEC = VOCAL_TIMING_SNAP_MS / 1000.0
VOCAL_TIMING_MAX_DRIFT_SEC = VOCAL_TIMING_MAX_DRIFT_MS / 1000.0

# Optional semi-transparent vocal activity bar on final rendered video (ffmpeg).
ENABLE_VOCAL_VISUALIZER = _bool_env("KARAOKE_FORGE_VOCAL_VISUALIZER", False)


def ensure_library_dirs() -> None:
    for path in (LIBRARY_DIR, SONGS_DIR, JOBS_DIR, RENDERS_DIR):
        path.mkdir(parents=True, exist_ok=True)
