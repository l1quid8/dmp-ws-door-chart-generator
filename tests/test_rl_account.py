"""Tests for the RemoteLink account generation path (scripts/rl_injector).

The staging logic (DMPDesign -> StagingAccount) and the .xml encoder both run on
every OS with no external template — a small synthetic template is built inline.

Run: pytest tests/test_rl_account.py
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from parse_dmp_worksheet import DMPDesign, SiteInfo, RSP, Keypad, Zone  # noqa: E402
from rl_injector.schema import (  # noqa: E402
    ZONE_TYPE_NIGHT,
    ZONE_TYPE_SPARE,
    ZONE_TYPE_SUPERVISORY,
    build_staging_account,
)
from rl_injector.errors import InjectorError  # noqa: E402
from rl_injector.xml_export import (  # noqa: E402
    _b64,
    build_account_xml,
    decode_account,
    encode_account,
    generate_account_xml,
)


def _rl_design(school_code="2250") -> DMPDesign:
    """A small design: two 16-point RSPs, and master zones covering four motion
    zones, one spare, and two supervisory (A/C-loss + battery) points."""
    d = DMPDesign(site_info=SiteInfo(
        school_name="TEST ELEMENTARY SCHOOL", school_code=school_code,
        address_line1="1 MAIN ST", address_line2="ENCINO, CA 91316",
        phone="(818) 555-1212"))
    d.rsps = [RSP(number=1, zones=list(range(501, 517))),
              RSP(number=2, zones=list(range(517, 533)))]
    d.keypads = [Keypad(number=1), Keypad(number=2), Keypad(number=1)]  # dup collapses
    d.master_zones = [
        Zone(number=501, description="ROOM 101"),
        Zone(number=502, description="ROOM 102"),
        Zone(number=503, description="ROOM 103"),
        Zone(number=504, description="ROOM 104"),
        Zone(number=505, description="SPARE", is_spare=True),
        Zone(number=515, description="PS-1 A/C", is_ps_ac=True),
        Zone(number=516, description="PS-1 BATT", is_ps_batt=True),
    ]
    return d


def test_build_staging_account_counts():
    acct = build_staging_account(_rl_design(), "2250", receiver_num="")
    assert acct.account_num == "2250"
    assert acct.name == "TEST ELEMENTARY SCHOOL"
    assert len(acct.zones) == 7
    assert acct.real_zone_count == 6      # spare excluded
    assert acct.spare_zone_count == 1
    assert acct.keypads == [1, 2]         # de-duplicated + sorted bus numbers


def test_build_staging_account_zone_types():
    acct = build_staging_account(_rl_design(), "2250", receiver_num="")
    types = {z.number: z.zone_type for z in acct.zones}
    assert types[501] == ZONE_TYPE_NIGHT
    assert types[505] == ZONE_TYPE_SPARE
    assert types[515] == ZONE_TYPE_SUPERVISORY
    assert types[516] == ZONE_TYPE_SUPERVISORY


def test_build_staging_account_filters_uninstalled_zones():
    """master_zones outside any installed RSP's point range are not staged."""
    d = _rl_design()
    d.master_zones.append(Zone(number=999, description="GHOST"))  # no RSP owns 999
    acct = build_staging_account(d, "2250", receiver_num="")
    assert 999 not in {z.number for z in acct.zones}


# --- .xml encoder ----------------------------------------------------------

def _zone_xml(num, typ, name):
    return (f'<ZoneInfo><ACCOUNT_ID DataType="3">90000</ACCOUNT_ID>'
            f'<NUMBER DataType="3">{num}</NUMBER>'
            f'<NAME DataType="1">{_b64(name)}</NAME>'
            f'<TYPE DataType="1">{_b64(typ)}</TYPE></ZoneInfo>')


def _mini_template() -> str:
    """A minimal DECRYPTED account XML with the fields the encoder touches:
    Account identity, one prototype zone per TYPE, and a keypad prototype."""
    account = (
        '<Account>'
        '<ID DataType="14">90000</ID>'
        '<ACCOUNT_NUM DataType="3">1</ACCOUNT_NUM>'
        f'<NAME DataType="1">{_b64("TEMPLATE")}</NAME>'
        f'<ADDRESS DataType="1">{_b64("OLD ADDR")}</ADDRESS>'
        f'<CITY DataType="1">{_b64("OLD CITY")}</CITY>'
        f'<STATE DataType="1">{_b64("XX")}</STATE>'
        f'<ZIP DataType="1">{_b64("00000")}</ZIP>'
        f'<PHONE DataType="1">{_b64("OLD PHONE")}</PHONE>'
        '<ACCT_NUM DataType="3">1</ACCT_NUM>'
        '</Account>')
    zones = ('<ZoneInfoList>'
             + _zone_xml(501, "NT", "Z501 A")
             + _zone_xml(515, "SV", "Z515 B")
             + _zone_xml(509, "--", "Z509 SPARE")
             + '</ZoneInfoList>')
    devices = ('<DeviceInfoList><DeviceInfo>'
               '<ACCOUNT_ID DataType="3">90000</ACCOUNT_ID>'
               '<NUMBER DataType="3">1</NUMBER>'
               f'<NAME DataType="1">{_b64("KEYPAD 1")}</NAME>'
               '</DeviceInfo></DeviceInfoList>')
    return '<Panels><Panel>' + account + zones + devices + '</Panel></Panels>'


def test_xml_encode_decode_round_trip():
    doc = "<Panels><Panel><Account><ID DataType=\"14\">90000</ID></Account></Panel></Panels>"
    assert decode_account(encode_account(doc, "6712"), "6712") == doc
    # wrong passphrase must not silently yield the document
    with pytest.raises(InjectorError):
        decode_account(encode_account(doc, "6712"), "0000")


def test_build_account_xml_from_design():
    xml = build_account_xml(
        build_staging_account(_rl_design(), "2250", receiver_num=""),
        _mini_template())
    s = summary_via_decode(xml)
    assert s["account_num"] == "2250" and s["id"] == "92250"
    assert s["name"] == "TEST ELEMENTARY SCHOOL"
    assert s["zone_count"] == 7                      # all 7 staged zones rendered
    assert s["keypads"] == 2                         # two keypad DeviceInfo blocks
    # old internal id fully re-pointed
    assert ">90000<" not in xml and xml.count(">92250<") >= 1


def test_generate_account_xml_writes_file(tmp_path):
    tmpl = tmp_path / "template.xml"
    tmpl.write_text(_mini_template(), encoding="latin-1")
    out = generate_account_xml(_rl_design(), "2250", template_path=tmpl,
                               passphrase="secret", out_dir=tmp_path)
    assert out.is_file() and out.name == "2250_remotelink.xml"
    # the file is uppercase hex that decodes back under the passphrase
    text = out.read_text()
    assert text == text.upper() and all(c in "0123456789ABCDEF" for c in text)
    s = summary_via_decode(decode_account(text, "secret"))
    assert s["account_num"] == "2250" and s["zone_count"] == 7


def test_generate_account_xml_requires_passphrase(tmp_path):
    tmpl = tmp_path / "template.xml"
    tmpl.write_text(_mini_template(), encoding="latin-1")
    with pytest.raises(InjectorError):
        generate_account_xml(_rl_design(), "2250", template_path=tmpl,
                             passphrase="", out_dir=tmp_path)


def test_generate_account_xml_rejects_non_numeric_account(tmp_path):
    tmpl = tmp_path / "template.xml"
    tmpl.write_text(_mini_template(), encoding="latin-1")
    with pytest.raises(InjectorError):
        generate_account_xml(_rl_design(), "TEST", template_path=tmpl,
                             passphrase="p", out_dir=tmp_path)


def test_generate_account_xml_missing_template(tmp_path):
    with pytest.raises(InjectorError):
        generate_account_xml(_rl_design(), "2250", template_path=tmp_path / "nope.xml",
                             passphrase="p", out_dir=tmp_path)


def summary_via_decode(xml: str) -> dict:
    """Minimal structure read-back for assertions (avoids depending on rl_xml)."""
    import re
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    def _int(tag):
        el = root.find(f".//{tag}")
        return el.text if el is not None else None
    return {
        "id": _int("ID"),
        "account_num": _int("ACCOUNT_NUM"),
        "name": _decode_b64(root.find(".//Account/NAME").text),
        "zone_count": len(root.findall(".//ZoneInfo")),
        "keypads": len(root.findall(".//DeviceInfo")),
    }


def _decode_b64(v: str) -> str:
    import base64
    return base64.b64decode(v).decode("latin-1").rstrip("\x00")


# --- bundled demo template -------------------------------------------------

BUNDLED_TEMPLATE = REPO_ROOT / "remotelink_account_template.xml"


def test_bundled_template_present_and_scrubbed():
    assert BUNDLED_TEMPLATE.is_file(), "bundled RemoteLink template is missing"
    xml = BUNDLED_TEMPLATE.read_text(encoding="latin-1")
    assert "<Panels>" in xml and xml.count("<ZoneInfo>") >= 3
    # No real customer data may be baked into the public repo. Fields are
    # base64, so decode each and assert none of the source account's identifiers
    # survive.
    import base64
    import re
    # LAUSD (the district) is an intentional dealer-standard Customer value, not
    # per-account identity — it's allowed. These are the source account's private
    # identifiers, which must not survive the scrub into the public repo.
    real = ("DARBY", "NORTHRIDGE", "10818", "360-1824", "0020D16D",
            "000B94289066", "D8D4X0VG", "FACP", "PRINCIPAL", "TEXTBOOK")
    # No private (10.x) IP may survive either.
    for m in re.finditer(r'DataType="1"[^>]*>([A-Za-z0-9+/=]*)<', xml):
        try:
            dec = base64.b64decode(m.group(1)).decode("latin-1").rstrip("\x00")
        except Exception:
            continue
        assert not re.fullmatch(r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}", dec), \
            f"private IP leaked in template: {dec}"
    for m in re.finditer(r'DataType="1"[^>]*>([A-Za-z0-9+/=]*)<', xml):
        try:
            dec = base64.b64decode(m.group(1)).decode("latin-1")
        except Exception:
            continue
        for tok in real:
            assert tok not in dec, f"real data leaked in template: {tok!r}"


def test_generate_account_xml_with_bundled_template(tmp_path):
    out = generate_account_xml(_rl_design(), "2250", template_path=BUNDLED_TEMPLATE,
                               passphrase="secret", out_dir=tmp_path)
    s = summary_via_decode(decode_account(out.read_text(), "secret"))
    assert s["account_num"] == "2250" and s["id"] == "92250"
    assert s["zone_count"] == 7 and s["keypads"] == 2


def _padded_dt1_fields(xml: str) -> list:
    import re
    return [v for v in re.findall(r'DataType="1"[^>]*>([A-Za-z0-9+/=]*)<', xml)
            if "=" in v]


def test_user_codes_default_from_account(tmp_path):
    import base64
    import re
    out = generate_account_xml(_rl_design(), "2250", template_path=BUNDLED_TEMPLATE,
                               passphrase="p", out_dir=tmp_path)
    xml = decode_account(out.read_text(), "p")
    codes = {}
    for m in re.finditer(r"<Users>.*?</Users>", xml, re.S):
        num = re.search(r'<USER_NUM DataType="3">(\d+)</USER_NUM>', m.group(0)).group(1)
        code = re.search(r'<CODE DataType="1">([^<]*)</CODE>', m.group(0)).group(1)
        codes[num] = base64.b64decode(code).decode("latin-1").rstrip("\x00")
    assert codes.get("1") == "2250"       # USER = site code
    assert codes.get("9999") == "12250"   # TECHNICIAN = 1 + site code


def test_area_schedule_default(tmp_path):
    import re
    out = generate_account_xml(_rl_design(), "2250", template_path=BUNDLED_TEMPLATE,
                               passphrase="p", out_dir=tmp_path)
    xml = decode_account(out.read_text(), "p")
    a1 = re.search(r"<AreaTimeScheds>.*?<NUMBER DataType=\"3\">1</NUMBER>.*?</AreaTimeScheds>",
                   xml, re.S).group(0)
    assert '<SCHED_1 DataType="3">1</SCHED_1>' in a1   # area 1 uses schedule 1


def test_no_base64_padding_in_bundled_template():
    # Real DMP exports use '=' padding in zero string fields; RemoteLink
    # mis-decodes any '='-padded value into junk characters.
    xml = BUNDLED_TEMPLATE.read_text(encoding="latin-1")
    assert _padded_dt1_fields(xml) == []


def test_no_base64_padding_in_generated_account():
    # Regression: names whose length was ≡1 mod 3 previously produced '=='-padded
    # base64 and rendered as garbage (e.g. "TEXTBOOK ROOM #2▯@") on import.
    d = _rl_design()
    d.master_zones.append(Zone(number=506, description="TEXTBOOK ROOM #2"))  # len ≡1 mod 3 case
    d.rsps[0].zones.append(506)
    xml = build_account_xml(build_staging_account(d, "2250", receiver_num=""),
                            BUNDLED_TEMPLATE.read_text(encoding="latin-1"))
    assert _padded_dt1_fields(xml) == []
