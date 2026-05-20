from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path

import pytest

from packages.karaoke_forge.vocal_timing import find_vocal_stem_path, refine_segment_word_timings


def _write_sine_burst_wav(
    path: Path,
    *,
    sample_rate: int = 22050,
    burst_start: float = 0.8,
    burst_end: float = 1.2,
    frequency: float = 440.0,
    amplitude: float = 0.8,
    total_duration: float = 2.5,
) -> None:
    total_samples = int(sample_rate * total_duration)
    burst_start_i = int(burst_start * sample_rate)
    burst_end_i = int(burst_end * sample_rate)
    samples = array("h")
    for index in range(total_samples):
        if burst_start_i <= index < burst_end_i:
            t = index / sample_rate
            value = amplitude * math.sin(2 * math.pi * frequency * t)
        else:
            value = 0.0
        samples.append(int(max(-32767, min(32767, round(value * 32767)))))

    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())


def test_refine_snaps_word_boundaries_toward_energy(tmp_path: Path) -> None:
    wav_path = tmp_path / "Artist (Vocals).wav"
    _write_sine_burst_wav(wav_path, burst_start=0.8, burst_end=1.2)

    segments = [
        {
            "text": "hello",
            "start_time": 0.5,
            "end_time": 1.5,
            "words": [
                {"text": "hello", "start_time": 0.65, "end_time": 1.35},
            ],
        },
    ]

    refine_segment_word_timings(segments, wav_path)
    word = segments[0]["words"][0]
    assert word["start_time"] > 0.65
    assert word["start_time"] >= 0.75
    assert word["end_time"] < 1.35
    assert word["end_time"] <= 1.28


def test_refine_preserves_monotonic_non_overlap(tmp_path: Path) -> None:
    wav_path = tmp_path / "track_vocals.wav"
    _write_sine_burst_wav(wav_path, burst_start=0.5, burst_end=0.9, total_duration=2.0)
    _write_sine_burst_wav(
        tmp_path / "second_burst.wav",
        burst_start=1.1,
        burst_end=1.5,
        total_duration=2.0,
    )

    segments = [
        {
            "text": "one two",
            "start_time": 0.0,
            "end_time": 2.0,
            "words": [
                {"text": "one", "start_time": 0.35, "end_time": 0.95},
                {"text": "two", "start_time": 0.95, "end_time": 1.65},
            ],
        },
    ]

    refine_segment_word_timings(segments, wav_path)
    first, second = segments[0]["words"]
    assert first["start_time"] < first["end_time"]
    assert second["start_time"] < second["end_time"]
    assert first["end_time"] <= second["start_time"] + 0.001
    assert first["end_time"] <= second["end_time"]


def test_refine_missing_file_is_noop(tmp_path: Path) -> None:
    segments = [
        {
            "text": "hello",
            "start_time": 1.0,
            "end_time": 2.0,
            "words": [{"text": "hello", "start_time": 1.1, "end_time": 1.9}],
        },
    ]
    original = (segments[0]["words"][0]["start_time"], segments[0]["words"][0]["end_time"])
    refine_segment_word_timings(segments, tmp_path / "missing.wav")
    word = segments[0]["words"][0]
    assert (word["start_time"], word["end_time"]) == original


def test_find_vocal_stem_path_prefers_vocals_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = tmp_path / "run"
    stems = run_dir / "stems"
    stems.mkdir(parents=True)
    backing = stems / "song_backing_vocals.wav"
    vocals = stems / "song_mixed_vocals.wav"
    backing.write_bytes(b"x")
    vocals.write_bytes(b"x")

    class _Job:
        metadata = {"run_dir": str(run_dir)}

    found = find_vocal_stem_path(_Job())
    assert found == vocals
