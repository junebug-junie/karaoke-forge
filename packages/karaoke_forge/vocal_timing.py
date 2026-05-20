"""Refine per-word timings using RMS energy on an isolated vocal stem."""

from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path
from typing import Any

from .config import (
    VOCAL_TIMING_FRAME_MS,
    VOCAL_TIMING_MAX_DRIFT_SEC,
    VOCAL_TIMING_SNAP_SEC,
)

_MIN_WORD_DURATION_SEC = 0.02


def find_vocal_stem_path(job: Any) -> Path | None:
    """Locate a vocals stem WAV under the job run directory (best-effort)."""
    if job is None:
        return None
    metadata = getattr(job, "metadata", None) or {}
    run_dir = metadata.get("run_dir")
    if not run_dir:
        return None
    root = Path(str(run_dir))
    if not root.is_dir():
        return None

    def _score(path: Path) -> tuple[int, int, str]:
        name = path.name.lower()
        if "backing" in name or "no_vocal" in name or "no vocal" in name:
            return (99, 99, name)
        prefer = 0
        if "lead_vocal" in name or "lead vocal" in name:
            prefer = 0
        elif "mixed_vocal" in name or "mixed vocal" in name:
            prefer = 1
        elif "vocals" in name or "vocal" in name:
            prefer = 2
        else:
            prefer = 5
        depth = len(path.relative_to(root).parts)
        return (prefer, depth, name)

    candidates: list[Path] = []
    for pattern in ("*.wav", "*.WAV"):
        for path in root.rglob(pattern):
            if "vocal" in path.name.lower():
                candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=_score)
    return candidates[0]


def _load_mono_samples(wav_path: Path) -> tuple[list[float], int]:
    with wave.open(str(wav_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
        raw = wav_file.readframes(frame_count)

    if sample_width == 2:
        samples = array("h")
        samples.frombytes(raw)
        scale = 1.0 / 32768.0
    elif sample_width == 1:
        samples = array("b")
        samples.frombytes(raw)
        scale = 1.0 / 128.0
    elif sample_width == 4:
        samples = array("i")
        samples.frombytes(raw)
        scale = 1.0 / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width: {sample_width}")

    if channels < 1:
        return [], sample_rate

    mono: list[float] = []
    for index in range(0, len(samples), channels):
        chunk = samples[index : index + channels]
        mono.append(sum(chunk) / len(chunk) * scale)
    return mono, sample_rate


def _rms_envelope(
    samples: list[float],
    sample_rate: int,
    *,
    frame_ms: int,
) -> tuple[list[float], list[float]]:
    if not samples or sample_rate <= 0:
        return [], []
    frame_len = max(1, int(sample_rate * frame_ms / 1000))
    times: list[float] = []
    rms_values: list[float] = []
    for start in range(0, len(samples), frame_len):
        chunk = samples[start : start + frame_len]
        if not chunk:
            break
        energy = sum(value * value for value in chunk) / len(chunk)
        center = (start + len(chunk) / 2.0) / sample_rate
        times.append(center)
        rms_values.append(math.sqrt(energy))
    return times, rms_values


def _window_indices(
    times: list[float],
    center: float,
    half_window: float,
    *,
    pad_sec: float = 0.0,
) -> range:
    if not times:
        return range(0)
    start = center - half_window
    end = center + half_window + pad_sec
    first = 0
    while first < len(times) and times[first] < start:
        first += 1
    last = first
    while last < len(times) and times[last] <= end:
        last += 1
    return range(first, last)


def _snap_to_rise(
    times: list[float],
    rms_values: list[float],
    target: float,
    half_window: float,
    *,
    frame_pad_sec: float = 0.0,
) -> float:
    indices = list(_window_indices(times, target, half_window, pad_sec=frame_pad_sec))
    if len(indices) < 2:
        return target
    best_time = target
    best_score = float("-inf")
    for index in indices[1:]:
        prev = index - 1
        delta = rms_values[index] - rms_values[prev]
        if delta > best_score:
            best_score = delta
            best_time = times[index]
    return best_time if best_score > 0 else target


def _snap_to_fall(
    times: list[float],
    rms_values: list[float],
    target: float,
    half_window: float,
    *,
    frame_pad_sec: float = 0.0,
) -> float:
    indices = list(_window_indices(times, target, half_window, pad_sec=frame_pad_sec))
    if len(indices) < 2:
        return target
    best_time = target
    best_score = float("-inf")
    for index in indices[1:]:
        prev = index - 1
        delta = rms_values[prev] - rms_values[index]
        if delta > best_score:
            best_score = delta
            best_time = times[index]
    return best_time if best_score > 0 else target


def _clamp_word_time(
    proposed: float,
    *,
    original: float,
    seg_start: float,
    seg_end: float,
    max_drift: float,
) -> float:
    low = max(seg_start - max_drift, original - max_drift)
    high = min(seg_end + max_drift, original + max_drift)
    if low > high:
        return max(seg_start, min(seg_end, original))
    return max(low, min(high, proposed))


def _segment_bounds(segment: dict[str, Any]) -> tuple[float, float]:
    start = segment.get("start_time", segment.get("start"))
    end = segment.get("end_time", segment.get("end"))
    try:
        seg_start = float(start)
        seg_end = float(end)
    except (TypeError, ValueError):
        seg_start, seg_end = 0.0, 0.0
    if seg_end < seg_start:
        seg_end = seg_start
    return seg_start, seg_end


def _word_bounds(word: dict[str, Any], seg_start: float, seg_end: float) -> tuple[float, float]:
    try:
        start = float(word.get("start_time", seg_start))
        end = float(word.get("end_time", seg_end))
    except (TypeError, ValueError):
        start, end = seg_start, seg_end
    if end < start:
        end = start + _MIN_WORD_DURATION_SEC
    return start, end


def refine_segment_word_timings(
    segments: list[dict[str, Any]],
    vocal_wav_path: str | Path,
    *,
    frame_ms: int | None = None,
    snap_sec: float | None = None,
    max_drift_sec: float | None = None,
) -> list[dict[str, Any]]:
    """Snap word boundaries toward vocal RMS rises/falls; preserves monotonic order."""
    path = Path(vocal_wav_path)
    if not path.is_file():
        return segments

    frame_ms = frame_ms if frame_ms is not None else VOCAL_TIMING_FRAME_MS
    snap_sec = snap_sec if snap_sec is not None else VOCAL_TIMING_SNAP_SEC
    max_drift_sec = max_drift_sec if max_drift_sec is not None else VOCAL_TIMING_MAX_DRIFT_SEC

    try:
        samples, sample_rate = _load_mono_samples(path)
    except (OSError, wave.Error, ValueError):
        return segments

    times, rms_values = _rms_envelope(samples, sample_rate, frame_ms=frame_ms)
    if not times:
        return segments

    frame_pad_sec = frame_ms / 1000.0 / 2.0

    for segment in segments:
        words = segment.get("words")
        if not isinstance(words, list):
            continue
        word_dicts = [word for word in words if isinstance(word, dict)]
        if not word_dicts:
            continue

        seg_start, seg_end = _segment_bounds(segment)
        refined: list[tuple[float, float]] = []
        for word in word_dicts:
            orig_start, orig_end = _word_bounds(word, seg_start, seg_end)
            snap_start = _snap_to_rise(
                times, rms_values, orig_start, snap_sec, frame_pad_sec=frame_pad_sec
            )
            snap_end = _snap_to_fall(
                times, rms_values, orig_end, snap_sec, frame_pad_sec=frame_pad_sec
            )
            new_start = _clamp_word_time(
                snap_start,
                original=orig_start,
                seg_start=seg_start,
                seg_end=seg_end,
                max_drift=max_drift_sec,
            )
            new_end = _clamp_word_time(
                snap_end,
                original=orig_end,
                seg_start=seg_start,
                seg_end=seg_end,
                max_drift=max_drift_sec,
            )
            new_start = max(seg_start, min(seg_end, new_start))
            new_end = max(seg_start, min(seg_end, new_end))
            if new_end - new_start < _MIN_WORD_DURATION_SEC:
                new_end = min(seg_end, new_start + _MIN_WORD_DURATION_SEC)
            refined.append((new_start, new_end))

        cursor = seg_start
        for index, word in enumerate(word_dicts):
            start, end = refined[index]
            if start < cursor:
                start = cursor
            if end <= start:
                end = min(seg_end, start + _MIN_WORD_DURATION_SEC)
            if index + 1 < len(word_dicts):
                next_start, _ = refined[index + 1]
                if end > next_start:
                    end = max(start + _MIN_WORD_DURATION_SEC, next_start)
            end = max(start + _MIN_WORD_DURATION_SEC, min(seg_end, end))
            word["start_time"] = round(start, 3)
            word["end_time"] = round(end, 3)
            cursor = end

    return segments
