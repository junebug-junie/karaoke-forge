"""Ensure Forge subprocesses load patched vendor/karaoke-gen, not pip site-packages."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from packages.karaoke_forge.runner import build_environment


def test_build_environment_prefers_vendor_karaoke_gen() -> None:
    env = build_environment()
    pythonpath = env.get("PYTHONPATH", "")
    root = Path(__file__).resolve().parents[1]
    vendor = str(root / "vendor" / "karaoke-gen")
    assert pythonpath.startswith(f"{vendor}:")


def test_subprocess_imports_inline_lead_in_from_vendor() -> None:
    root = Path(__file__).resolve().parents[1]
    env = build_environment()
    code = (
        "import karaoke_gen.lyrics_transcriber.output.ass.lyrics_line as m; "
        "print('inline_lead_in' if hasattr(m.LyricsLine, '_create_inline_lead_in_prefix') else 'missing')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "inline_lead_in"
