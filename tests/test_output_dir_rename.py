"""Tests for paths.py default output dir across the app rename.

The app was renamed "DMP WS & Door Chart Generator" -> "C1 DMP Toolkit". The
default output folder is derived from that name, so a machine that never set an
explicit "Save output to" folder has real deliverables filed under the old name.
Switching outright would strand them in a folder the app no longer looks at, with
nothing on screen explaining where they went — so the legacy folder wins, but only
when it exists and the new one does not.

Run: pytest tests/test_output_dir_rename.py
"""
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import paths  # noqa: E402


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() at a scratch dir with an empty ~/Documents."""
    (tmp_path / "Documents").mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def _docs(home):
    return home / "Documents"


def test_prefers_legacy_folder_when_only_it_exists(fake_home):
    """Prior deliverables live under the old name — keep writing there."""
    legacy = _docs(fake_home) / paths._LEGACY_APP_NAME
    legacy.mkdir()
    assert paths._default_output_dir() == legacy


def test_uses_new_name_on_a_clean_machine(fake_home):
    """Nothing to preserve, so a fresh install gets the current name."""
    assert paths._default_output_dir() == _docs(fake_home) / paths.APP_NAME


def test_new_name_wins_when_both_exist(fake_home):
    """Once the new folder exists it is authoritative; a leftover old folder
    must not pull output back to the pre-rename location."""
    (_docs(fake_home) / paths._LEGACY_APP_NAME).mkdir()
    new = _docs(fake_home) / paths.APP_NAME
    new.mkdir()
    assert paths._default_output_dir() == new


def test_legacy_file_is_not_mistaken_for_the_folder(fake_home):
    """A stray file named like the old app is not a usable output dir."""
    (_docs(fake_home) / paths._LEGACY_APP_NAME).touch()
    assert paths._default_output_dir() == _docs(fake_home) / paths.APP_NAME


def test_explicit_pref_overrides_both(fake_home, monkeypatch, tmp_path):
    """The in-app "Save output to" picker still wins over either default."""
    (_docs(fake_home) / paths._LEGACY_APP_NAME).mkdir()
    chosen = tmp_path / "chosen"
    prefs = tmp_path / "prefs.json"
    prefs.write_text('{"output_dir": "%s"}' % chosen)
    monkeypatch.setattr(paths, "_PREFS_PATH", prefs)
    assert paths.output_dir() == chosen
