"""Optional vocal activity overlay on final karaoke renders via ffmpeg."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .config import ENABLE_VOCAL_VISUALIZER
from .vocal_timing import find_vocal_stem_path

_VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi"}


def overlay_bar_height(video_height: int) -> int:
    """Bar height in pixels (60–120) derived from video height."""
    if video_height <= 0:
        return 80
    scaled = max(60, video_height // 10)
    return min(120, scaled)


def build_vocal_overlay_filter(vocal_wav_path: Path, video_height: int) -> str:
    """Build ffmpeg -filter_complex graph; input 0=video, 1=vocal stem at vocal_wav_path."""
    _ = vocal_wav_path  # second ffmpeg input; kept for call-site clarity and tests
    bar_h = overlay_bar_height(video_height)
    return (
        f"[1:a]aformat=sample_rates=44100:channel_layouts=mono,"
        f"showfreqs=mode=bar:ascale=log:win_size=512:overlap=0.75"
        f":colors=0xffffffaa:rate=30:s=640x{bar_h}[vf];"
        f"[vf][0:v]scale2ref=w=iw:h={bar_h}:force_original_aspect_ratio=disable[bar][base];"
        f"[base][bar]overlay=x=(W-w)/2:y=H-h:format=auto,format=yuv420p[vout]"
    )


def _probe_video_height(video_path: Path) -> int | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=height",
                "-of",
                "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip().splitlines()
    if not value or not value[0].isdigit():
        return None
    return int(value[0])


def apply_vocal_visualizer_to_video(
    input_mp4: Path,
    vocal_wav_path: Path,
    output_mp4: Path,
    *,
    video_height: int | None = None,
) -> bool:
    """Composite a semi-transparent vocal activity bar onto input_mp4; return True on success."""
    input_mp4 = input_mp4.resolve()
    vocal_wav_path = vocal_wav_path.resolve()
    output_mp4 = output_mp4.resolve()
    if not input_mp4.is_file() or not vocal_wav_path.is_file():
        return False

    height = video_height if video_height is not None else _probe_video_height(input_mp4)
    filter_graph = build_vocal_overlay_filter(vocal_wav_path, height or 1080)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_mp4),
        "-i",
        str(vocal_wav_path),
        "-filter_complex",
        filter_graph,
        "-map",
        "[vout]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-c:a",
        "copy",
        str(output_mp4),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def apply_vocal_visualizer_to_render_paths(
    job: Any,
    render_paths: list[str],
) -> tuple[list[str], dict[str, object]]:
    """Post-process copied render MP4s when vocal visualizer is enabled."""
    if not ENABLE_VOCAL_VISUALIZER:
        return render_paths, {"vocal_visualizer_enabled": False}

    vocal_path = find_vocal_stem_path(job)
    if vocal_path is None:
        return render_paths, {
            "vocal_visualizer_enabled": True,
            "vocal_visualizer_applied": False,
            "vocal_visualizer_skip": "no_vocal_stem",
        }

    updated = list(render_paths)
    applied_paths: list[str] = []
    failures: list[str] = []

    for index, render_path in enumerate(render_paths):
        path = Path(render_path)
        if path.suffix.lower() not in _VIDEO_SUFFIXES or not path.is_file():
            continue

        temp_out = path.with_name(f"{path.stem}.vocalviz{path.suffix}")
        if apply_vocal_visualizer_to_video(path, vocal_path, temp_out):
            temp_out.replace(path)
            updated[index] = str(path)
            applied_paths.append(str(path))
        else:
            failures.append(str(path))
            if temp_out.exists():
                temp_out.unlink()

    debug: dict[str, object] = {
        "vocal_visualizer_enabled": True,
        "vocal_visualizer_applied": bool(applied_paths),
        "vocal_visualizer_vocal_stem": str(vocal_path),
        "vocal_visualizer_paths": applied_paths,
    }
    if failures:
        debug["vocal_visualizer_failures"] = failures
    if not applied_paths and not failures:
        debug["vocal_visualizer_skip"] = "no_video_renders"
    return updated, debug
