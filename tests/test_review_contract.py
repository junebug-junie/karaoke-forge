from packages.karaoke_forge.review_contract import (
    find_corrected_segments_summary,
    review_gate_decision,
    review_payload_summary,
)


def test_finds_top_level_corrected_segments():
    payload = {"corrected_segments": [{"text": "a"}, {"text": "b"}]}
    assert find_corrected_segments_summary(payload) == (2, "$.corrected_segments")


def test_finds_nested_corrected_segments():
    payload = {"data": {"review": {"corrected_segments": [{"text": "a"}]}}}
    assert find_corrected_segments_summary(payload) == (1, "$.data.review.corrected_segments")


def test_missing_corrected_segments_is_not_ready():
    payload = {"segments": [{"text": "whisper only"}]}
    summary = review_payload_summary(payload)
    assert summary["ready"] is False
    assert summary["corrected_segments_count"] == 0
    assert summary["corrected_segments_path"] is None


def test_review_gate_blocks_success_without_review():
    allowed, error = review_gate_decision(returncode=0, review_seen=False, require_review_payload=True)
    assert allowed is False
    assert "corrected_segments review payload" in error


def test_review_gate_allows_success_after_review():
    allowed, error = review_gate_decision(returncode=0, review_seen=True, require_review_payload=True)
    assert allowed is True
    assert error is None


def test_review_gate_preserves_nonzero_exit_failure():
    allowed, error = review_gate_decision(returncode=7, review_seen=True, require_review_payload=True)
    assert allowed is False
    assert "exited with 7" in error
