"""Encode a DMP design as a RemoteLink encrypted `.xml` account export.

Format (reverse-engineered from real exports; see the injector repo's
tools/XML_FORMAT.md):

    file = HEX_UPPER( AES-128-ECB( xml_document, key = MD5(passphrase_ascii) ) )

The plaintext is a <Panels><Panel>...</Panel></Panels> document. This module
produces one the way the SQL path does — clone-and-rebadge from a golden
DECRYPTED template account (the operator supplies it once, the XML analogue of
templates/academy_full.sql), swapping in the design's account identity, zones,
and keypads while keeping the template's valid comm/options config.

Field values carry a DataType attribute: "3"/"14" = integer text, "1" =
Base64(text + trailing NUL), "11" = datetime. Zones live in <ZoneInfoList> as
<ZoneInfo> blocks (NUMBER int, NAME base64 "Z501 ROOM", TYPE base64 NT/SV/--);
keypads in <DeviceInfoList> as <DeviceInfo> blocks.
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Optional

from Crypto.Cipher import AES

from .errors import InjectorError
from .schema import build_staging_account

BLOCK = 16
SENTINEL_BASE = 90000   # internal <ID> = SENTINEL_BASE + account number (matches clone.py)


# ------------------------------------------------------------------ crypto ---

def _key(passphrase: str) -> bytes:
    return hashlib.md5(passphrase.encode("ascii")).digest()


def decode_account(data: bytes | str, passphrase: str) -> str:
    """Decrypt a RemoteLink `.xml` export (hex text or raw bytes) to its XML."""
    if isinstance(data, str):
        ct = bytes.fromhex(data.strip())
    else:
        stripped = bytes(b for b in data if b not in b" \t\r\n")
        ct = bytes.fromhex(stripped.decode("ascii")) if stripped and \
            all(c in b"0123456789abcdefABCDEF" for c in stripped) else bytes(data)
    if len(ct) % BLOCK:
        raise InjectorError(f"ciphertext length {len(ct)} is not a multiple of {BLOCK}")
    text = AES.new(_key(passphrase), AES.MODE_ECB).decrypt(ct).decode("latin-1")
    end = text.rfind("</Panels>")
    if end == -1:
        raise InjectorError("decrypted payload has no </Panels> — wrong passphrase?")
    return text[:end + len("</Panels>")]


def encode_account(xml_text: str, passphrase: str) -> str:
    """Encrypt an XML account document to the on-disk UPPERCASE hex form."""
    raw = xml_text.encode("latin-1")
    if len(raw) % BLOCK:
        raw += b"\x00" * (BLOCK - len(raw) % BLOCK)
    return AES.new(_key(passphrase), AES.MODE_ECB).encrypt(raw).hex().upper()


# --------------------------------------------------------------- xml render ---

def _b64(text: str) -> str:
    """Encode a DataType=1 string value: Base64(text + trailing NUL)."""
    return base64.b64encode(text.encode("latin-1") + b"\x00").decode("ascii")


def _unb64(value: str) -> str:
    return base64.b64decode(value).decode("latin-1").rstrip("\x00")


def _set(xml: str, tag: str, inner: str, *, count: int = 1) -> str:
    """Replace the inner text of the first `count` <tag ...>...</tag> (count=0 = all)."""
    pat = re.compile(rf"(<{tag}\b[^>]*>).*?(</{tag}>)", re.S)
    return pat.sub(lambda m: m.group(1) + inner + m.group(2), xml, count=count)


def _field(block: str, tag: str) -> Optional[str]:
    m = re.search(rf'<{tag}\b[^>]*DataType="(\d+)"[^>]*>(.*?)</{tag}>', block, re.S)
    if not m:
        return None
    return _unb64(m.group(2)) if m.group(1) == "1" else m.group(2)


def _replace_list(xml: str, list_tag: str, inner: str) -> str:
    return re.sub(rf"(<{list_tag}>).*?(</{list_tag}>)",
                  lambda m: m.group(1) + inner + m.group(2), xml, count=1, flags=re.S)


def _rebadge_identity(xml: str, *, account_num: str, name: str, sentinel: int) -> str:
    """Re-point the internal ID (referenced by every child <ACCOUNT_ID>) and the
    two account-number fields, and set the account NAME (first NAME only)."""
    m = re.search(r"<ID\b[^>]*>(\d+)</ID>", xml)
    old_id = m.group(1) if m else None
    xml = _set(xml, "ACCOUNT_NUM", account_num, count=0)
    xml = _set(xml, "ACCT_NUM", account_num, count=0)
    if old_id is not None and old_id != str(sentinel):
        xml = xml.replace(f">{old_id}<", f">{sentinel}<")
    xml = _set(xml, "NAME", _b64(name), count=1)
    return xml


def build_account_xml(acct, template_xml: str, *, sentinel: Optional[int] = None) -> str:
    """Render a StagingAccount into a full RemoteLink account XML by cloning the
    template's per-type zone blocks and keypad block."""
    if sentinel is None:
        sentinel = SENTINEL_BASE + int(acct.account_num)
    xml = _rebadge_identity(template_xml, account_num=acct.account_num,
                            name=acct.name, sentinel=sentinel)

    # Account address block from the design (kept as base64 DataType=1 fields).
    for tag, val in (("ADDRESS", acct.address), ("CITY", acct.city),
                     ("STATE", acct.state), ("ZIP", acct.zip_code),
                     ("PHONE", acct.phone)):
        if val:
            xml = _set(xml, tag, _b64(val), count=1)

    # Zones: clone a prototype block per DMP zone TYPE so type-specific
    # programming fields (AREA_LIST, action messages, SWGR_BYPS) come along.
    protos: dict[str, str] = {}
    for zb in re.findall(r"<ZoneInfo>.*?</ZoneInfo>", xml, re.S):
        protos.setdefault(_field(zb, "TYPE"), zb)
    if not protos:
        raise InjectorError("template has no <ZoneInfo> block to clone from")
    default_proto = protos.get("NT") or next(iter(protos.values()))
    blocks = []
    for z in sorted(acct.zones, key=lambda x: x.number):
        proto = protos.get(z.zone_type, default_proto)
        b = _set(proto, "NUMBER", str(z.number), count=1)
        b = _set(b, "NAME", _b64(f"Z{z.number} {z.name}"), count=1)
        blocks.append(b)
    xml = _replace_list(xml, "ZoneInfoList", "".join(blocks))

    # Keypads: clone the DeviceInfo prototype, one per keypad bus number.
    dproto = re.search(r"<DeviceInfo>.*?</DeviceInfo>", xml, re.S)
    if dproto and acct.keypads:
        dblocks = []
        for n in acct.keypads:
            d = _set(dproto.group(0), "NUMBER", str(n), count=1)
            d = _set(d, "NAME", _b64(f"KEYPAD {n}"), count=1)
            dblocks.append(d)
        xml = _replace_list(xml, "DeviceInfoList", "".join(dblocks))

    return xml


# ------------------------------------------------------------------- public ---

def generate_account_xml(design, account_num, receiver_num: str = "", *,
                         template_path, passphrase: str,
                         out_dir, sentinel: Optional[int] = None) -> Path:
    """Build an encrypted RemoteLink `.xml` for a design and write it.

    `template_path` is a DECRYPTED golden account XML (create it once with the
    injector's tools/rl_xml.py: `decode <export> <passphrase> -o template.xml`).
    `passphrase` is the export pin the operator will type at import time.
    Returns the written `.xml` path. Raises InjectorError on bad input.
    """
    account_num = str(account_num).strip()
    if not account_num.isdigit():
        raise InjectorError(
            f"Account number '{account_num}' must be numeric (the school LOC "
            "CODE, e.g. 2250) — it is assigned as the panel user code.")
    if not passphrase:
        raise InjectorError("An export passphrase is required for the .xml.")
    tmpl = Path(template_path)
    if not tmpl.is_file():
        raise InjectorError(
            f"RemoteLink XML template not found: {tmpl}\n"
            "Decode one real export once (tools/rl_xml.py decode) and point the "
            "app at that decrypted .xml.")
    template_xml = tmpl.read_text(encoding="latin-1")
    if "<Panels>" not in template_xml:
        raise InjectorError(
            f"{tmpl} is not a decrypted RemoteLink account XML (no <Panels>). "
            "It must be the DECRYPTED template, not an encrypted export.")

    acct = build_staging_account(design, account_num, receiver_num=(receiver_num or "").strip())
    if not acct.zones:
        raise InjectorError("No zones to stage — the design has no zones on an installed RSP.")
    xml = build_account_xml(acct, template_xml, sentinel=sentinel)
    hexed = encode_account(xml, passphrase)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{account_num}_remotelink.xml"
    out_path.write_text(hexed, encoding="ascii")
    return out_path
