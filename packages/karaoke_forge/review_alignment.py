from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


def normalize_lyric_text(value: str) -> str:
    text = str(value or "").lower()
    text = text.replace("'", "'").replace("'", "'")
    text = re.sub(r"[^a-z0-9']+", " ", text)
    return " ".join(text.split())


def _segment_text_value(segment: dict[str, Any]) -> str:
    for key in ("text", "corrected_text", "lyrics", "line"):
        value = segment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    words = segment.get("words")
    if isinstance(words, list):
        parts = []
        for word in words:
            if isinstance(word, dict):
                token = str(word.get("text") or word.get("word") or "").strip()
                if token:
                    parts.append(token)
        joined = " ".join(parts).strip()
        if joined:
            return joined
    return ""


def _match_score(segment_norm: str, line_norm: str) -> float:
    if not segment_norm or not line_norm:
        return 0.0
    if segment_norm == line_norm:
        return 1.0
    if segment_norm in line_norm or line_norm in segment_norm:
        shorter = min(len(segment_norm), len(line_norm))
        longer = max(len(segment_norm), len(line_norm))
        return shorter / longer if longer else 0.0
    seg_tokens = set(segment_norm.split())
    line_tokens = set(line_norm.split())
    if not seg_tokens or not line_tokens:
        return 0.0
    overlap = len(seg_tokens & line_tokens) / len(seg_tokens | line_tokens)
    fuzzy = SequenceMatcher(None, segment_norm, line_norm).ratio()
    return max(overlap, fuzzy)


def align_canonical_lines_to_segments(
    segments: list[dict[str, Any]],
    canonical_lines: list[str],
    *,
    min_score: float = 0.38,
    look_ahead: int = 3,
) -> list[str | None]:
    """Map each segment to the best matching canonical line (not by row index)."""
    if not canonical_lines:
        return [None] * len(segments)

    line_norms = [normalize_lyric_text(line) for line in canonical_lines]
    aligned: list[str | None] = [None] * len(segments)
    line_cursor = 0
    last_line_idx: int | None = None
    same_line_run = 0
    switch_margin = 0.12

    for seg_idx, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        segment_norm = normalize_lyric_text(_segment_text_value(segment))
        if not segment_norm:
            continue

        best_line = -1
        best_score = 0.0
        search_start = line_cursor if same_line_run >= 2 else max(0, line_cursor - 1)
        search_end = min(len(canonical_lines), line_cursor + look_ahead)

        sticky_floor = 0.0
        if last_line_idx is not None and same_line_run < 2:
            sticky_score = _match_score(segment_norm, line_norms[last_line_idx])
            if sticky_score >= min_score:
                best_line = last_line_idx
                best_score = sticky_score
                sticky_floor = sticky_score

        for line_idx in range(search_start, search_end):
            score = _match_score(segment_norm, line_norms[line_idx])
            if score > best_score and score >= sticky_floor + switch_margin:
                best_score = score
                best_line = line_idx

        if best_line < 0 or best_score < min_score:
            last_line_idx = None
            same_line_run = 0
            continue

        aligned[seg_idx] = canonical_lines[best_line]
        if last_line_idx == best_line:
            same_line_run += 1
        else:
            same_line_run = 1
        last_line_idx = best_line
        if same_line_run >= 2:
            line_cursor = best_line + 1
        elif best_score >= 0.72 and best_line >= line_cursor:
            line_cursor = best_line + 1
        else:
            line_cursor = max(line_cursor, best_line)

    return aligned


def tail_junk_segment_indexes(aligned: list[str | None]) -> list[int]:
    """Segments after the last aligned row are usually Whisper outro/noise."""
    last_aligned = max((idx for idx, line in enumerate(aligned) if line), default=-1)
    if last_aligned < 0:
        return []
    return [idx for idx in range(last_aligned + 1, len(aligned))]


def alignment_summary(
    segments: list[dict[str, Any]],
    canonical_lines: list[str],
    aligned: list[str | None],
) -> dict[str, Any]:
    matched = sum(1 for line in aligned if line)
    junk = tail_junk_segment_indexes(aligned)
    return {
        "canonical_line_count": len(canonical_lines),
        "segment_count": len(segments),
        "aligned_segment_count": matched,
        "tail_junk_count": len(junk),
        "tail_junk_indexes": junk,
    }
