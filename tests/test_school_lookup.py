"""Tests for school_lookup — the live LAUSD phone lookup.

The load-bearing property: a transient network failure must NOT be cached. The
directory fetch is best-effort (blank on failure), but if a momentary blip at
the first lookup of a session got cached, the phone would stay blank for every
school for the rest of that session even after the network recovered.

Run: pytest tests/test_school_lookup.py
"""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import school_lookup  # noqa: E402


_DIRECTORY = {
    "results": [
        {"street_location": "19452 Hart St.", "zip_location": "91335",
         "phone": "(818) 342-6183", "school_name": "Shirley Avenue Elementary"},
    ]
}


def _reset_cache():
    school_lookup._reset_directory_cache()


def test_transient_failure_is_not_cached(monkeypatch):
    """A failed fetch must retry on the next call, not poison the session."""
    _reset_cache()
    calls = {"n": 0}

    def fake_get_json(url):
        calls["n"] += 1
        return None if calls["n"] == 1 else _DIRECTORY  # first fails, then recovers

    monkeypatch.setattr(school_lookup, "_get_json", fake_get_json)

    # First lookup hits the network blip -> no match, blank phone.
    assert school_lookup.lookup_phone(
        "Shirley Avenue Elementary", "19452 HART ST", "RESEDA, CA 91335") is None
    # Network recovered: the SAME session must now succeed (failure wasn't cached).
    assert school_lookup.lookup_phone(
        "Shirley Avenue Elementary", "19452 HART ST", "RESEDA, CA 91335") == "(818) 342-6183"


def test_successful_directory_is_cached(monkeypatch):
    """A successful fetch is cached — no repeat network call per lookup."""
    _reset_cache()
    calls = {"n": 0}

    def fake_get_json(url):
        calls["n"] += 1
        return _DIRECTORY

    monkeypatch.setattr(school_lookup, "_get_json", fake_get_json)

    assert school_lookup.lookup_phone(
        "X", "19452 HART ST", "RESEDA, CA 91335") == "(818) 342-6183"
    assert school_lookup.lookup_phone(
        "X", "19452 HART ST", "RESEDA, CA 91335") == "(818) 342-6183"
    assert calls["n"] == 1  # directory fetched once, reused for the second lookup
