"""
Look up a school's phone number, live, at parse time.

The phone isn't on the design PDF, so the worksheet generator fills it from the
Urban Institute Education Data Portal — a free, keyless government API backed by
the federal CCD school directory. Nothing is bundled or cached to disk: the LAUSD
directory is fetched fresh once per session and matched by street number + ZIP
(both parse reliably out of the PDF title block), with a fuzzy name fallback.

Best-effort only: any failure (offline, timeout, API change) leaves the phone
blank for manual entry — it never blocks or breaks a parse.

Coverage note: the federal CCD dataset carries ~785 LAUSD schools (traditional
elementary/middle/high campuses are well covered; some non-traditional types are
absent). A miss returns None -> phone stays blank, never wrong.
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import date
from typing import Optional

# Los Angeles Unified's federal LEA id. The tool only processes LAUSD designs, so
# one filtered request returns the whole district (~785 rows) in a single page.
_LAUSD_LEAID = "0622710"
_API = "https://educationdata.urban.org/api/v1/schools/ccd/directory/{year}/?leaid=" + _LAUSD_LEAID
_UA = "DMP-WS-DoorChart/1.0 (+https://github.com/WiseUp2RiseUp)"
_TIMEOUT = 8.0

# Name-normalization: drop parentheticals ("(Julie)"), school-type suffixes, and
# punctuation so OCR'd "KORENSTEIN ES" lines up with CCD "Julie Korenstein Elementary".
_PARENS_RE = re.compile(r"\([^)]*\)")
_TYPE_WORDS = {
    "elementary", "el", "es", "middle", "ms", "high", "hs", "school", "senior",
    "primary", "center", "academy", "magnet", "span", "k8", "k12",
}


def _norm_name(name: str) -> set[str]:
    name = _PARENS_RE.sub(" ", name or "")
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return {t for t in tokens if t not in _TYPE_WORDS}


def _street_number(address: str) -> str:
    m = re.match(r"\s*(\d+)", address or "")
    return m.group(1) if m else ""


def _zip5(text: str) -> str:
    m = re.search(r"(\d{5})(?:-\d{4})?\s*$", (text or "").strip())
    return m.group(1) if m else ""


def _fmt_phone(raw: str) -> str:
    """Normalize to (XXX) XXX-XXXX; return the raw string if it isn't 10 digits."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return (raw or "").strip()
    return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"


def _get_json(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# Session cache for the directory. We cache ONLY a successful, non-empty fetch —
# never a failure. lru_cache would memoize the empty tuple a transient blip
# returns, so a single early network hiccup would leave the phone blank for
# every school for the rest of the session even after the network recovered.
_DIRECTORY_CACHE: Optional[tuple] = None


def _reset_directory_cache() -> None:
    """Clear the cached directory (test hook)."""
    global _DIRECTORY_CACHE
    _DIRECTORY_CACHE = None


def _fetch_lausd_directory() -> tuple:
    """Fetch the LAUSD school directory once per session.

    CCD data lags the calendar by a couple of years (and future years return an
    empty result set), so probe from the current year downward and use the most
    recent year that actually has rows. Returns a tuple of (street, zip, phone,
    name); empty on any failure. A successful fetch is memoized for the session;
    a failure is not, so the next lookup retries rather than staying blank.
    """
    global _DIRECTORY_CACHE
    if _DIRECTORY_CACHE is not None:
        return _DIRECTORY_CACHE
    for year in range(date.today().year, date.today().year - 7, -1):
        data = _get_json(_API.format(year=year))
        if data is None:
            # A request FAILED (offline/timeout/HTTP error). Older years would
            # fail the same way, so stop now instead of burning one timeout per
            # year — this keeps the offline path to a single ~timeout, not N.
            break
        results = data.get("results") or []
        if not results:
            # Valid response but this year isn't published yet (CCD lags the
            # calendar); these come back fast, so probe the next older year.
            continue
        rows = []
        for r in results:
            rows.append((
                str(r.get("street_location") or r.get("street_mailing") or ""),
                str(r.get("zip_location") or r.get("zip_mailing") or "")[:5],
                str(r.get("phone") or ""),
                str(r.get("school_name") or ""),
            ))
        _DIRECTORY_CACHE = tuple(rows)
        return _DIRECTORY_CACHE
    # Every path here is a failure (offline/timeout) or a genuinely empty result.
    # Return empty WITHOUT caching so a later lookup retries once the network is back.
    return tuple()


def lookup_phone(school_name: str, address_line1: str, address_line2: str) -> Optional[str]:
    """Return the school's phone as "(XXX) XXX-XXXX", or None if no confident match.

    Primary key: street number + ZIP (near-unique). Fallback: fuzzy name match
    among rows sharing the ZIP. Any lookup/network failure returns None.
    """
    rows = _fetch_lausd_directory()
    if not rows:
        return None

    street_no = _street_number(address_line1)
    zip5 = _zip5(address_line2)

    # Primary: exact street-number + ZIP.
    if street_no and zip5:
        for street, zc, phone, _name in rows:
            if zc == zip5 and _street_number(street) == street_no:
                return _fmt_phone(phone)

    # Fallback: best fuzzy name overlap within the same ZIP.
    if zip5:
        want = _norm_name(school_name)
        if want:
            best_phone, best_score = None, 0
            for _street, zc, phone, name in rows:
                if zc != zip5:
                    continue
                score = len(want & _norm_name(name))
                if score > best_score:
                    best_phone, best_score = phone, score
            if best_phone is not None and best_score > 0:
                return _fmt_phone(best_phone)

    return None
