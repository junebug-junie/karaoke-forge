# Karaoke Lead-in + Vocal-Dynamic Timing + Waveform Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inline per-line lead-in coupled to first-word `\kf` highlights; preserve Whisper word timing through Forge review; optional vocal-envelope refinement and waveform video overlay.

**Architecture:** Vendor patch for ASS lead-in in `lyrics_line.py`; Forge-owned modules for word preservation, vocal timing, and visualizer. Config via `config.py`, `.env.example`, and theme JSON keys.

**Tech Stack:** Python, pytest, ASS karaoke tags, whisper-timestamped, ffmpeg, numpy/pydub (existing deps).

**Branch:** `feature/karaoke-leadin-vocal-sync`  
**Worktree:** `.worktrees/karaoke-leadin-vocal-sync`

---

## Task 1: Inline per-line lead-in coupled to first `\kf`

**Files:**
- Modify: `vendor/karaoke-gen/karaoke_gen/lyrics_transcriber/output/ass/lyrics_line.py`
- Modify: `vendor/karaoke-gen/karaoke_gen/lyrics_transcriber/output/ass/config.py`
- Modify: `vendor/karaoke-gen/karaoke_gen/lyrics_transcriber/output/subtitles.py`
- Create: `vendor/karaoke-gen/tests/unit/lyrics_transcriber/output/test_lead_in_inline.py`

**Spec:**
- Remove or bypass `_create_lead_in_event()` for default path (inline only)
- Wire `_create_lead_in_text()` logic into `_create_ass_text()`:
  - `sing_start = segment.words[0].start_time` (fallback `segment.start_time`)
  - `preview_start = max(fade_in_time, sing_start - lead_in_preview_seconds)`
  - Emit `{\k<gap>}{\kf<duration>}<LEAD_CHAR> ` before first word
  - Lead char: `█` or `▶` with lead-in color via inline `{\c...}` if feasible
- Show lead-in on every line where `sing_start > previous_segment_end + lead_in_min_gap` OR first line
- `_create_lead_in_event()` must NOT duplicate when inline is active

**Verify:** `pytest vendor/karaoke-gen/tests/unit/lyrics_transcriber/output/test_lead_in_inline.py -v`

---

## Task 2: Preserve Whisper word arrays through Forge review

**Files:**
- Modify: `packages/karaoke_forge/review_proxy.py`
- Modify: `tests/test_review_contract.py`

**Spec:**
- When `force=False` and word text matches segment text and `len(words) > 1`: return False (preserve)
- Text change: redistribute timings proportionally (document choice in comment)
- Timing-only edit: scale word times proportionally within new bounds
- `_resync_all_segment_words` on complete: `force=False` by default

**Verify:** `pytest tests/test_review_contract.py -v`

---

## Task 3: Vocal envelope word boundary refinement

**Files:**
- Create: `packages/karaoke_forge/vocal_timing.py`
- Create: `tests/test_vocal_timing.py`
- Modify: `packages/karaoke_forge/config.py`
- Hook before ASS generation when `KARAOKE_FORGE_VOCAL_TIMING=1`

**Verify:** `pytest tests/test_vocal_timing.py -v`

---

## Task 4: Vocal waveform overlay in rendered video

**Files:**
- Create: `packages/karaoke_forge/vocal_visualizer.py`
- Create: `tests/test_vocal_visualizer.py`
- Modify: `packages/karaoke_forge/config.py` + render hook

**Verify:** `pytest tests/test_vocal_visualizer.py -v`

---

## Task 5: Integration + config documentation

**Files:**
- Modify: `.env.example`, `packages/karaoke_forge/config.py` comments
- Align with `.env.local` patterns

**Verify:** Full test suite commands in user spec.
