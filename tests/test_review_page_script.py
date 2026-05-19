from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from packages.karaoke_forge.web import app

REVIEW_PATH = "/karaoke-forge/review"
VALIDATOR = Path(__file__).resolve().parent / "scripts" / "validate_review_page_script.mjs"

# Stale identifiers from pre-alignment refactors — must never reappear in inline JS.
BANNED_JS_IDENTIFIERS = (
    "canonicalLines",
    "applyCanonicalLyricsByLineOrder",
    "removeRowsAfterFinalCanonicalLyric",
    "setRowTextFromCanonical",
    "tailExtra",
    "applyCanonicalButton",
    "deleteAfterCanonicalButton",
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _extract_inline_script(html: str) -> str:
    return html.split("<script>", 1)[1].split("</script>", 1)[0]


def _served_review_html(client: TestClient) -> str:
    response = client.get(REVIEW_PATH)
    assert response.status_code == 200
    return response.text


def test_review_page_has_no_unreplaced_placeholders(client: TestClient) -> None:
    html = _served_review_html(client)
    leftovers = re.findall(r"__[A-Z_]+__", html)
    assert leftovers == [], f"unreplaced placeholders: {sorted(set(leftovers))}"


def test_review_page_dom_ids_match_script(client: TestClient) -> None:
    html = _served_review_html(client)
    script = _extract_inline_script(html)
    html_ids = set(re.findall(r'\bid="([^"]+)"', html.split("<script>", 1)[0]))
    js_ids = set(re.findall(r'getElementById\("([^"]+)"\)', script))
    missing = js_ids - html_ids
    assert not missing, f"getElementById targets missing from HTML: {sorted(missing)}"


def test_review_page_script_has_no_banned_identifiers(client: TestClient) -> None:
    script = _extract_inline_script(_served_review_html(client))
    hits = [name for name in BANNED_JS_IDENTIFIERS if name in script]
    assert hits == [], f"banned stale identifiers in script: {hits}"


def test_review_page_split_regex_not_broken_by_python_escapes(client: TestClient) -> None:
    script = _extract_inline_script(_served_review_html(client))
    split_match = re.search(r"canonicalLyricsEl\.value\.split\(([^)]+)\)", script)
    assert split_match is not None
    assert "\n" not in split_match.group(0)
    assert split_match.group(1) == "/\\r?\\n/"


def test_review_page_script_passes_node_syntax_check(client: TestClient) -> None:
    script = _extract_inline_script(_served_review_html(client))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as tmp:
        tmp.write(script)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            ["node", "--check", tmp_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_review_page_render_native_smoke(client: TestClient) -> None:
    if not VALIDATOR.is_file():
        pytest.skip("validator script missing")
    script = _extract_inline_script(_served_review_html(client))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as tmp:
        tmp.write(script)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            ["node", str(VALIDATOR), tmp_path],
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    assert result.returncode == 0, (result.stderr or result.stdout).strip()
