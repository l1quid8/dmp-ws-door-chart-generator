"""Tests for the MOTION DETECTOR ZONE SCHEDULE parser (parse_zone_schedule.extract_zones).

PyMuPDF emits each table cell on its own line. The parser must find a zone-id even when
OCR prepends a floating callout annotation onto the cell line (the real-world failure where
zones 557/573 were silently dropped from the TOLUCA design).

Run: pytest tests/test_zone_schedule.py
"""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from parse_zone_schedule import extract_zones  # noqa: E402


def _by_num(records):
    return {int(r.zone[1:]): r for r in records}


def test_mid_line_zone_id_is_recovered():
    """A zone-id merged behind an OCR callout annotation must still be parsed, and the
    preceding clean zone must keep its own room (its body just gets shortened)."""
    text = "\n".join([
        "Z556/RSP5",
        "BUILDING C",
        "1ST FLR",
        "CLASSROOM 27",
        "NEW",
        "(N)AQC240",
        "(MAIN OFFICE, BUILDING | 7557/RSP5",   # callout annotation merged onto the id cell
        "BUILDING C",
        "1ST FLR",
        "CLASSROOM 24",
        "NEW",
        "(N)AQC240",
        "Z558/RSP5",
        "BUILDING C",
        "1ST FLR",
        "CLASSROOM 25",
        "NEW",
        "(N)AQC240",
    ])
    zones = _by_num(extract_zones(text))
    assert set(zones) == {556, 557, 558}
    assert zones[557].rsp == 5
    assert zones[557].room == "CLASSROOM 24"
    assert zones[556].room == "CLASSROOM 27"   # unchanged despite shortened body
    assert zones[558].room == "CLASSROOM 25"


def test_leading_z_misread_still_parses():
    """Leading 'Z' misread as 7/2 (or present) all resolve to the 3-digit zone number."""
    text = "\n".join([
        "7501/RSP1", "BUILDING A", "1ST FLR", "ROOM A", "NEW", "(N)WP240",
        "2502/RSP1", "BUILDING A", "1ST FLR", "ROOM B", "NEW", "(N)WP240",
        "Z503/RSP1", "BUILDING A", "1ST FLR", "ROOM C", "NEW", "(N)WP240",
    ])
    zones = _by_num(extract_zones(text))
    assert set(zones) == {501, 502, 503}
    assert zones[501].rsp == 1


def test_no_false_zone_from_noise_lines():
    """Cable types and combus labels must not be mistaken for zone-ids."""
    text = "\n".join([
        "RSP 5",
        "(N)WP240",
        "BUILDING C",
        "Z510/RSP1",
        "BUILDING A",
        "1ST FLR",
        "ROOM",
    ])
    zones = _by_num(extract_zones(text))
    assert set(zones) == {510}
