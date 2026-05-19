#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi"}


def load_env_local(root: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    path = root / ".env.local"
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def looks_like_segment_list(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(item, dict) for item in value)


def find_named_segment_list(data: Any, keys: tuple[str, ...], path: str = "$") -> tuple[str, list[Any], str] | None:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if looks_like_segment_list(value):
                return key, value, f"{path}.{key}"
        for key, value in data.items():
            found = find_named_segment_list(value, keys, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(data, list):
        for idx, value in enumerate(data):
            found = find_named_segment_list(value, keys, f"{path}[{idx}]")
            if found is not None:
                return found
    return None


def get_latest_job(db_path: Path) -> dict[str, Any] | None:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite DB not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    data = dict(row)
    try:
        data["metadata"] = json.loads(data.get("metadata") or "{}")
    except json.JSONDecodeError:
        data["metadata"] = {"_metadata_parse_error": data.get("metadata")}
    return data


def fetch_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=2.0) as response:
            body = response.read(2_000_000)
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read(2_000_000)
        return {
            "ok": False,
            "status_code": exc.code,
            "phase": "http_error",
            "body_preview": body.decode("utf-8", errors="replace")[:1000],
        }
    except Exception as exc:
        return {"ok": False, "status_code": None, "phase": "unavailable", "error": str(exc)}

    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "status_code": status,
            "phase": "invalid_json",
            "error": str(exc),
            "body_preview": body.decode("utf-8", errors="replace")[:1000],
        }
    return {"ok": True, "status_code": status, "phase": "json", "payload": payload}


def summarize_review_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "payload_type": type(payload).__name__,
            "corrected_segments_count": 0,
            "corrected_segments_path": None,
            "display_segments_count": 0,
            "display_segments_source_key": None,
            "display_segments_path": None,
        }
    corrected = find_named_segment_list(payload, ("corrected_segments",))
    display = corrected or find_named_segment_list(payload, ("segments", "lyrics_segments"))
    return {
        "payload_type": "dict",
        "payload_keys": list(payload.keys()),
        "corrected_segments_count": len(corrected[1]) if corrected else 0,
        "corrected_segments_path": corrected[2] if corrected else None,
        "display_segments_count": len(display[1]) if display else 0,
        "display_segments_source_key": display[0] if display else None,
        "display_segments_path": display[2] if display else None,
    }


def path_state(path_text: str | None) -> dict[str, Any]:
    if not path_text:
        return {"path": path_text, "exists": False}
    path = Path(path_text)
    exists = path.exists()
    out: dict[str, Any] = {"path": str(path), "exists": exists}
    if exists:
        stat = path.stat()
        out.update({"size": stat.st_size, "mtime": stat.st_mtime, "suffix": path.suffix})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E diagnostics for Karaoke Forge review/finalize flow")
    parser.add_argument("--root", default=".", help="Repo root. Default: current directory")
    parser.add_argument("--port", type=int, default=None, help="karaoke-gen review server port. Default: env or 8000")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    env = load_env_local(root)
    library = Path(env.get("KARAOKE_FORGE_LIBRARY", root / "library")).resolve()
    db_path = Path(env.get("KARAOKE_FORGE_DB", library / "karaoke_forge.sqlite3")).resolve()
    port = args.port or int(env.get("KARAOKE_REVIEW_SERVER_PORT", "8000"))

    failures: list[str] = []
    warnings: list[str] = []

    print("== Karaoke Forge E2E Review Diagnostics ==")
    print(f"root: {root}")
    print(f"library: {library}")
    print(f"db: {db_path}")
    print(f"review_port: {port}")

    job = get_latest_job(db_path)
    if job is None:
        print("\nNo jobs found.")
        return 1

    metadata = job.get("metadata") or {}
    print("\n== Latest job ==")
    print(f"id: {job.get('id')}")
    print(f"title: {job.get('artist')} — {job.get('title')}")
    print(f"status: {job.get('status')}")
    print(f"error: {job.get('error')}")
    print(f"run_dir: {metadata.get('run_dir')}")
    print(f"log_path: {job.get('log_path')}")
    print(f"auto_advance_stdin: {metadata.get('auto_advance_stdin')}")
    print(f"review_completed_seen: {metadata.get('review_completed_seen')}")
    print(f"review_ready_at: {metadata.get('review_ready_at')}")

    log_state = path_state(job.get("log_path"))
    print("\n== Files ==")
    print(f"log exists: {log_state['exists']} :: {log_state['path']}")
    if not log_state["exists"]:
        failures.append("latest job has no run log")
    else:
        log_text = Path(str(log_state["path"])).read_text(encoding="utf-8", errors="replace")
        interesting = [
            line for line in log_text.splitlines()
            if any(token in line for token in ("[stdin]", "[review", "Opening review UI", "correction", "Final Videos", "[renders]", "[exit_code]"))
        ]
        print("\n== Log review/render trace ==")
        for line in interesting[-80:]:
            print(line)
        if "[stdin] auto_advance=True" in log_text:
            failures.append("runner auto-advanced stdin; review may have been skipped")
        if "[stdin] auto_advance=False" not in log_text:
            warnings.append("run log does not prove stdin auto-advance was disabled")

    print("\n== Live review API ==")
    correction_url = f"http://127.0.0.1:{port}/api/correction-data"
    correction = fetch_json(correction_url)
    print(json.dumps({k: v for k, v in correction.items() if k != "payload"}, indent=2, sort_keys=True))
    if correction.get("ok"):
        summary = summarize_review_payload(correction.get("payload"))
        print(json.dumps(summary, indent=2, sort_keys=True))
        if summary["corrected_segments_count"] <= 0:
            failures.append("live /api/correction-data has no corrected_segments to resolve")
    else:
        failures.append(f"live /api/correction-data unavailable: {correction.get('phase')} {correction.get('status_code')}")

    render_outputs = metadata.get("render_outputs") or []
    render_discovery = metadata.get("render_discovery") or {}
    print("\n== Render outputs ==")
    print(f"render_outputs_count: {len(render_outputs)}")
    print(json.dumps(render_discovery, indent=2, sort_keys=True))
    for idx, path_text in enumerate(render_outputs):
        state = path_state(path_text)
        print(f"render[{idx}]: exists={state['exists']} path={state['path']} size={state.get('size')}")
        if not state["exists"]:
            failures.append(f"render output missing on disk: {path_text}")

    if render_outputs and metadata.get("review_completed_seen") is False:
        failures.append("job has render_outputs even though metadata says review_completed_seen=False")
    if render_outputs and metadata.get("review_completed_seen") is None:
        warnings.append("job has render_outputs but no review_completed_seen metadata; this may be an older unsafe run")

    print("\n== Verdict ==")
    for warning in warnings:
        print(f"WARN: {warning}")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 2
    print("PASS: latest job has a coherent review/render state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
