"""Tests for riser-page detection in generate_dmp_ws.py.

The riser (sheet INT-5.0) is the single page all device topology is read from, so
picking the wrong page silently corrupts the worksheet. Detection used to guess by
splitter-anchor count, but a dense siteplan (INT-1.0) can carry the same anchors and
tie the riser — see the Toluca Lake set. Detection now keys off the page's own sheet
number; these tests pin that the title-block discriminator is correct.

Run: pytest tests/test_riser_detection.py
"""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from generate_dmp_ws import _page_sheet_number, RISER_SHEET  # noqa: E402


def test_riser_titleblock_is_recognized():
    """A page whose only sheet ref is INT-5.0 resolves to the riser sheet."""
    text = "MSP RSP1 ... 710-LX500-1 SPLITTER ... INT-5.0 INTRUSION"
    assert _page_sheet_number(text) == RISER_SHEET


def test_cover_index_page_matches_nothing():
    """The cover sheet lists every sheet number, so it must NOT be taken as the
    riser even though INT-5.0 appears in its index."""
    text = " ".join(
        f"INT-{n}" for n in (
            "0.0", "1.0", "2.1", "2.2", "2.3", "3.0", "3.1", "3.2", "4.0",
            "5.0", "6.0", "6.1", "6.2", "6.3", "6.4", "6.5", "6.6",
        )
    )
    assert _page_sheet_number(text) is None


def test_anchor_dense_siteplan_is_not_the_riser():
    """The siteplan carries the same nine splitter anchors that tied the riser, but
    its title block is INT-1.0 — so it resolves to 1.0, never the riser."""
    anchors = (
        "710-LX500-1 710-KP-1 710-LX500-2 710-KP-3 710-KP-2 "
        "710-LX500-5 710-LX500-6 710-LX500-3 710-LX500-4"
    )
    text = f"SITEPLAN {anchors} RSP1 MSP INT-1.0"
    sheet = _page_sheet_number(text)
    assert sheet == "1.0"
    assert sheet != RISER_SHEET


def test_whitespace_and_hyphen_variants():
    """Title blocks render the sheet number with a hyphen or a space; both parse."""
    assert _page_sheet_number("foo INT-5.0 bar") == "5.0"
    assert _page_sheet_number("foo INT 5.0 bar") == "5.0"


def test_unlabeled_page_matches_nothing():
    text = "MSP RSP1 some floor plan with no sheet number"
    assert _page_sheet_number(text) is None
