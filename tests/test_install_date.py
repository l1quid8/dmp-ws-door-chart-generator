"""Tests for the install-date default (editor_frame._format_install_date).

Install date is not remembered across projects (a carried-forward date is always
stale); it defaults to today, written the way techs write it on the worksheet.

Run: pytest tests/test_install_date.py
"""
from datetime import date
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from editor_frame import _format_install_date  # noqa: E402


def test_ordinal_suffixes():
    assert _format_install_date(date(2026, 7, 21)) == "JULY 21st 2026"
    assert _format_install_date(date(2026, 7, 1)) == "JULY 1st 2026"
    assert _format_install_date(date(2026, 7, 2)) == "JULY 2nd 2026"
    assert _format_install_date(date(2026, 7, 3)) == "JULY 3rd 2026"
    assert _format_install_date(date(2026, 7, 4)) == "JULY 4th 2026"


def test_teens_are_all_th():
    # 11/12/13 are the ordinal exceptions, not st/nd/rd.
    assert _format_install_date(date(2026, 3, 11)) == "MARCH 11th 2026"
    assert _format_install_date(date(2026, 3, 12)) == "MARCH 12th 2026"
    assert _format_install_date(date(2026, 3, 13)) == "MARCH 13th 2026"
    assert _format_install_date(date(2026, 12, 23)) == "DECEMBER 23rd 2026"
