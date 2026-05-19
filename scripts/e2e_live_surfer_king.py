#!/usr/bin/env python3
"""Playwright live E2E: Surfer King upload → pipeline → review → finish → log audit."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8790/karaoke-forge"
MP3 = ROOT / "library/jobs/A.A. Bondy - Surfer King/runs/20260519T092605Z/A.A. Bondy - Surfer King/A.A. Bondy - Surfer King (Original).mp3"
LYRICS = """Behind the red door in american skin
There is a murder of roses
In the midnight hiss come cover me there
For i am electric nothing
Out on the tide strangers all are drowning by
Under eclipse I wait for your kiss
With the beating of all these idiot hearts
No more evil now, no horror sound, no maniac song from a tyrant
And the surfer king will show me everything in the great green flash of the evening
Out on the tide strangers we ride
Smoke in our eyes
Under eclipse, I wait for your kiss
With the beating of all these idiot hearts"""


def log(msg: str) -> None:
    print(msg, flush=True)


def wait_for_status(page, pattern: str, timeout_s: int = 1800) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        text = page.locator(".status").first.inner_text(timeout=5000)
        if re.search(pattern, text, re.I):
            return
        time.sleep(5)
        page.reload(wait_until="domcontentloaded")
    raise TimeoutError(f"status did not match {pattern!r} within {timeout_s}s")


def wait_for_review_api(timeout_s: int = 1800) -> None:
    import urllib.request

    url = f"{BASE}/review/native/data"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                payload = json.loads(resp.read().decode())
            if payload.get("ready") and payload.get("segments"):
                return
        except Exception:
            pass
        time.sleep(5)
    raise TimeoutError("review native data never became ready")


def analyze_review_rows(page) -> dict:
    rows = page.locator("tr[data-segment-index]")
    count = rows.count()
    issues: list[str] = []
    samples: list[dict] = []
    seen_corrected: dict[str, int] = {}
    for i in range(count):
        row = rows.nth(i)
        idx = int(row.get_attribute("data-segment-index") or i)
        whisper = row.locator(".original-text").inner_text().strip()
        corrected = row.locator("textarea.segment-edit").input_value().strip()
        resolved = row.locator(".canonical-preview-cell .pasted-line").inner_text().strip() if row.locator(".canonical-preview-cell .pasted-line").count() else ""
        samples.append({"row": idx + 1, "whisper": whisper[:80], "corrected": corrected[:80], "resolved": resolved[:80]})
        norm = re.sub(r"\s+", " ", corrected.lower())
        if norm in seen_corrected and idx - seen_corrected[norm] > 2:
            issues.append(f"row {idx+1} corrected text repeats row {seen_corrected[norm]+1}: {corrected[:60]!r}")
        seen_corrected[norm] = idx
        if resolved and corrected and resolved.strip() != corrected.strip():
            issues.append(f"row {idx+1} corrected != resolved: {corrected[:40]!r} vs {resolved[:40]!r}")
    return {"segment_count": count, "issues": issues, "samples": samples[:20], "tail_samples": samples[-8:]}


def scan_log_for_issues(log_text: str) -> list[str]:
    issues: list[str] = []
    if "auto_advance=True" in log_text:
        issues.append("stdin auto-advance was True — review may have been skipped")
    if "ERROR" in log_text or "Traceback" in log_text:
        issues.append("log contains ERROR or Traceback")
    if "refusing to collect default renders" in log_text:
        issues.append("render collection blocked (review gate)")
    return issues


def main() -> int:
    if not MP3.exists():
        log(f"MP3 missing: {MP3}")
        return 1

    report: dict = {"base": BASE, "mp3": str(MP3), "phases": {}}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(120_000)

        log("== Phase 1: Submit job ==")
        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.fill('input[name="artist"]', "A.A. Bondy")
        page.fill('input[name="title"]', "Surfer King E2E")
        page.locator('textarea[name="lyrics"]').fill(LYRICS.strip())
        page.locator('input[name="audio"]').set_input_files(str(MP3))
        page.click('button[type="submit"]')
        page.wait_for_url(re.compile(r"/jobs/[a-f0-9-]+"), timeout=60_000)
        job_url = page.url
        job_id = job_url.rstrip("/").split("/")[-1]
        report["job_id"] = job_id
        log(f"job created: {job_id}")

        log("== Phase 2: Wait for review API (up to 30 min) ==")
        wait_for_review_api(timeout_s=1800)
        report["phases"]["review_api_ready"] = True
        page.reload(wait_until="domcontentloaded")

        log_text = page.locator("#live-log").inner_text()
        report["phases"]["pre_review_log_issues"] = scan_log_for_issues(log_text)
        if "lyrics.txt" in log_text or "lyrics_file" in log_text:
            report["phases"]["lyrics_file_logged"] = True

        log("== Phase 3: Review tab ==")
        page.goto(f"{BASE}/review", wait_until="domcontentloaded")
        page.wait_for_selector("#native-review .review-table tbody tr[data-segment-index]", timeout=120_000)
        time.sleep(2)
        review_analysis = analyze_review_rows(page)
        report["phases"]["review_analysis"] = review_analysis
        log(f"review segments: {review_analysis['segment_count']}, issues: {len(review_analysis['issues'])}")
        for issue in review_analysis["issues"][:15]:
            log(f"  REVIEW ISSUE: {issue}")

        log("== Phase 4: Finish review ==")
        page.click("#native-complete")
        page.wait_for_function(
            "() => document.getElementById('review-status')?.textContent?.toLowerCase().includes('complete') || document.getElementById('review-status')?.textContent?.toLowerCase().includes('done')",
            timeout=300_000,
        )

        log("== Phase 5: Post-finish job log ==")
        page.goto(job_url, wait_until="domcontentloaded")
        wait_for_status(page, r"done|failed", timeout_s=1800)
        post_log = page.locator("#live-log").inner_text()
        report["phases"]["post_finish_log_issues"] = scan_log_for_issues(post_log)
        report["phases"]["final_status"] = page.locator(".status").first.inner_text()

        browser.close()

    out_path = ROOT / "library" / f"e2e_surfer_king_{report['job_id'][:8]}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log(f"\n== Report written: {out_path} ==")
    failures = (
        report["phases"].get("pre_review_log_issues", [])
        + report["phases"].get("post_finish_log_issues", [])
        + report["phases"].get("review_analysis", {}).get("issues", [])
    )
    return 2 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PlaywrightTimeout as exc:
        log(f"TIMEOUT: {exc}")
        raise SystemExit(3) from exc
