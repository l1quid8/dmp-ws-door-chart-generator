"""Tests for paths.py revision-numbered output naming.

Each generate writes {base}_rev{N}.xlsx and keeps prior revisions; the next
number is always max(existing)+1 so deleting a middle rev never causes an
overwrite of a kept one.

Run: pytest tests/test_rev_naming.py
"""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from paths import latest_rev_path, next_rev_path  # noqa: E402


def test_first_rev_in_empty_dir(tmp_path):
    assert next_rev_path(tmp_path, "school_dmp") == tmp_path / "school_dmp_rev1.xlsx"
    assert latest_rev_path(tmp_path, "school_dmp") is None


def test_increments_past_highest_with_gaps(tmp_path):
    (tmp_path / "school_dmp_rev1.xlsx").touch()
    (tmp_path / "school_dmp_rev4.xlsx").touch()   # rev2/3 deleted by the user
    assert next_rev_path(tmp_path, "school_dmp") == tmp_path / "school_dmp_rev5.xlsx"
    assert latest_rev_path(tmp_path, "school_dmp") == tmp_path / "school_dmp_rev4.xlsx"


def test_artifact_counters_are_independent(tmp_path):
    (tmp_path / "school_dmp_rev3.xlsx").touch()
    assert next_rev_path(tmp_path, "school_door_chart") == \
        tmp_path / "school_door_chart_rev1.xlsx"


def test_ignores_non_matching_files(tmp_path):
    for name in ("school_dmp_FINAL_2026-06-30.xlsx",   # legacy dated naming
                 "school_dmp_rev2.xlsx.bak",
                 "school_dmp_revX.xlsx",
                 "other_school_dmp_rev9.xlsx"):
        (tmp_path / name).touch()
    assert next_rev_path(tmp_path, "school_dmp") == tmp_path / "school_dmp_rev1.xlsx"


def test_base_with_regex_chars_is_escaped(tmp_path):
    base = "st_marys_(annex)_dmp"
    (tmp_path / f"{base}_rev2.xlsx").touch()
    assert next_rev_path(tmp_path, base) == tmp_path / f"{base}_rev3.xlsx"


def test_missing_dir_yields_rev1(tmp_path):
    assert next_rev_path(tmp_path / "nope", "school_dmp") == \
        tmp_path / "nope" / "school_dmp_rev1.xlsx"
    assert latest_rev_path(tmp_path / "nope", "school_dmp") is None
