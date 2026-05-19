from __future__ import annotations

from packages.karaoke_forge.review_alignment import (
    align_canonical_lines_to_segments,
    tail_junk_segment_indexes,
)


def test_align_matches_by_text_not_row_index():
    segments = [
        {"text": "Behind the red door"},
        {"text": "In American skin"},
        {"text": "There is a murder of roses"},
    ]
    canonical = [
        "Behind the red door in american skin",
        "There is a murder of roses in the midnight hiss come cover me there",
        "For i am electric nothing",
    ]
    aligned = align_canonical_lines_to_segments(segments, canonical)
    assert aligned[0] == canonical[0]
    assert aligned[1] == canonical[0]
    assert aligned[2] == canonical[1]


def test_tail_junk_indexes_only_after_last_aligned():
    aligned = ["line a", "line a", "line b", None, None]
    assert tail_junk_segment_indexes(aligned) == [3, 4]


def test_tail_junk_skips_unaligned_segment_that_matches_canonical():
    aligned = ["line a", "line a", None]
    segments = [
        {"text": "line a"},
        {"text": "line a"},
        {"text": "line b real"},
    ]
    canonical = ["line a", "line b real"]
    assert tail_junk_segment_indexes(aligned, segments, canonical) == []
