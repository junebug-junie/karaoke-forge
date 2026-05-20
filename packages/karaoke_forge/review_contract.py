from __future__ import annotations

import hashlib
import json
from typing import Any

FALSE_VALUES = {"0", "false", "no", "off"}


def looks_like_segment_list(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(item, dict) for item in value)


def find_corrected_segments_summary(data: Any, *, path: str = "$") -> tuple[int, str | None]:
    if isinstance(data, dict):
        value = data.get("corrected_segments")
        if looks_like_segment_list(value):
            return len(value), f"{path}.corrected_segments"
        for key, child in data.items():
            count, found_path = find_corrected_segments_summary(child, path=f"{path}.{key}")
            if found_path is not None:
                return count, found_path
    elif isinstance(data, list):
        for idx, child in enumerate(data):
            count, found_path = find_corrected_segments_summary(child, path=f"{path}[{idx}]")
            if found_path is not None:
                return count, found_path
    return 0, None


def review_payload_summary(payload: Any) -> dict[str, Any]:
    corrected_count, corrected_path = find_corrected_segments_summary(payload)
    return {
        "payload_type": type(payload).__name__,
        "ready": isinstance(payload, dict) and corrected_count > 0,
        "corrected_segments_count": corrected_count,
        "corrected_segments_path": corrected_path,
        "payload_keys": list(payload.keys()) if isinstance(payload, dict) else [],
    }


def segment_texts(segments: Any) -> list[str]:
    if not isinstance(segments, list):
        return []
    texts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = segment.get("text") or segment.get("corrected_text") or segment.get("lyrics") or segment.get("line") or ""
        texts.append(str(text).strip())
    return texts


def segments_text_digest(segments: Any) -> str:
    payload = json.dumps(segment_texts(segments), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def segments_preview_texts(segments: Any, *, count: int = 3) -> dict[str, list[str]]:
    texts = segment_texts(segments)
    if len(texts) <= count:
        return {"first": texts, "last": []}
    return {"first": texts[:count], "last": texts[-count:]}


def review_gate_decision(*, returncode: int, review_seen: bool, require_review_payload: bool = True) -> tuple[bool, str | None]:
    if returncode != 0:
        return False, f"karaoke-gen exited with {returncode}; see log"
    if require_review_payload and not review_seen:
        return False, "karaoke-gen exited before Forge observed review completion; refusing to collect default renders"
    return True, None
