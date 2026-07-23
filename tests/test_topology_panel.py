"""Tests for the SPLITTERS topology tree (editor_tabs.build_topology).

The tree is derived, not stored: it is rebuilt from design.splitters' inputs /
outputs plus the RSP and keypad lists every time the wiring changes. That makes
it the one place in the editor where hand-typed field data becomes a *graph*,
so the failure modes worth pinning down are structural rather than cosmetic:

- a tech can wire A -> B and B -> A, and an unguarded descent would hang the app
- removing hardware leaves dangling references that no longer name anything
- a splitter must appear exactly once, or the panel silently under-reports the
  wiring it exists to let the tech verify

build_topology is pure (no Tk), so all of that is testable headlessly.

Run: pytest tests/test_topology_panel.py
"""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from parse_dmp_worksheet import DMPDesign, Keypad, RSP, SiteInfo, Splitter  # noqa: E402
from editor_tabs import build_topology  # noqa: E402


# -------- helpers --------

def sp(sid, stype, inp, outs):
    """A splitter wired the way the editor's own dropdowns write it."""
    key = "LX-Bus In" if stype == "LX" else "KP-Bus In"
    return Splitter(id=sid, splitter_type=stype,
                    inputs={key: inp} if inp else {}, outputs=list(outs))


def design(splitters=(), rsps=(), keypads=(), xr="MDF ROOM 101"):
    d = DMPDesign(site_info=SiteInfo(xr550_location=xr))
    d.splitters = list(splitters)
    d.rsps = list(rsps)
    d.keypads = list(keypads)
    return d


def walk(topology):
    """Every node in the tree, flattened."""
    out = []

    def rec(node):
        out.append(node)
        for child in node.children:
            rec(child)

    for group in topology.groups:
        for node in group.nodes:
            rec(node)
    return out


def splitter_ids(topology):
    return [n.label for n in walk(topology) if n.kind == "splitter"]


def group_labels(topology):
    return [g.label for g in topology.groups]


# -------- the ordinary case --------

def test_normal_chain():
    """500 bus -> LX1 -> LX2, keypad bus -> KP1: nesting, metadata, spares."""
    d = design(
        splitters=[
            sp("710-LX500-1", "LX", "500 BUS IN FROM XR/550",
               ["RSP-1", "To 710-LX500-2", "Spare"]),
            sp("710-LX500-2", "LX", "From 710-LX500-1",
               ["RSP-2", "RSP-3", "Spare"]),
            sp("710-KP-1", "KP", "KEYPAD BUS IN FROM XR/550",
               ["KEYPAD #2", "Spare", "Spare"]),
        ],
        rsps=[RSP(number=1, zones=[501, 516]), RSP(number=2, zones=[517, 532]),
              RSP(number=3, zones=[533, 548])],
        keypads=[Keypad(number=1), Keypad(number=2, location="FRONT OFFICE")],
    )
    t = build_topology(d)

    assert t.root_meta == "MDF ROOM 101"
    assert group_labels(t) == ["500 BUS (LX)", "KEYPAD BUS"]
    assert sorted(splitter_ids(t)) == ["710-KP-1", "710-LX500-1", "710-LX500-2"]

    lx1_children = [c for n in walk(t) if n.label == "710-LX500-1"
                    for c in n.children]
    assert any(c.label == "710-LX500-2" for c in lx1_children)

    rsp1 = next(n for n in walk(t) if n.label == "RSP-1")
    assert rsp1.meta == "Z501–Z516"
    kp2 = next(n for n in walk(t) if n.label == "KEYPAD #2")
    assert kp2.meta == "FRONT OFFICE"
    assert sum(1 for n in walk(t) if n.kind == "spare") == 4


def test_bus_groups_are_ordered_by_bus_number():
    d = design(splitters=[
        sp("710-LX500-1", "LX", "500 BUS IN FROM XR/550", ["Spare"] * 3),
        sp("710-LX500-2", "LX", "600 BUS IN FROM XR/550", ["RSP-1", "Spare", "Spare"]),
    ])
    assert group_labels(build_topology(d)) == ["500 BUS (LX)", "600 BUS (LX)"]


def test_child_declared_only_by_its_own_input():
    """The parent's outputs never mention the child; the child's input does.

    Both halves of a connection are hand-editable, so they can disagree. The
    child must still be adopted rather than stranded under UNLINKED.
    """
    d = design(splitters=[
        sp("710-LX500-1", "LX", "500 BUS IN FROM XR/550", ["Spare"] * 3),
        sp("710-LX500-2", "LX", "From 710-LX500-1", ["RSP-1", "Spare", "Spare"]),
    ])
    t = build_topology(d)
    parent = next(n for n in walk(t) if n.label == "710-LX500-1")
    assert any(c.label == "710-LX500-2" for c in parent.children)
    assert group_labels(t) == ["500 BUS (LX)"]


# -------- cycles: these would hang the app without a visited guard --------

def test_two_splitter_cycle_terminates():
    d = design(splitters=[
        sp("710-LX500-1", "LX", "From 710-LX500-2", ["To 710-LX500-2", "Spare", "Spare"]),
        sp("710-LX500-2", "LX", "From 710-LX500-1", ["To 710-LX500-1", "Spare", "Spare"]),
    ])
    t = build_topology(d)  # would recurse forever without the guard

    assert group_labels(t) == ["UNLINKED"]
    assert sorted(splitter_ids(t)) == ["710-LX500-1", "710-LX500-2"]
    # The back-edge renders as a non-expanding link so the loop stays visible.
    back = [n for n in walk(t) if n.kind == "link"]
    assert len(back) == 1 and back[0].ref == "710-LX500-1"


def test_self_cycle_terminates():
    d = design(splitters=[
        sp("710-LX500-1", "LX", "From 710-LX500-1",
           ["To 710-LX500-1", "Spare", "Spare"]),
    ])
    assert splitter_ids(build_topology(d)) == ["710-LX500-1"]


def test_long_chain_is_fully_expanded():
    chain = []
    for i in range(1, 41):
        inp = "500 BUS IN FROM XR/550" if i == 1 else f"From 710-LX500-{i - 1}"
        outs = [f"To 710-LX500-{i + 1}"] if i < 40 else ["Spare"]
        chain.append(sp(f"710-LX500-{i}", "LX", inp, outs))
    ids = splitter_ids(build_topology(design(splitters=chain)))
    assert len(ids) == 40 and len(set(ids)) == 40


# -------- dangling references left behind by hardware removal --------

def test_input_names_a_splitter_that_no_longer_exists():
    d = design(splitters=[
        sp("710-LX500-1", "LX", "500 BUS IN FROM XR/550", ["RSP-1", "Spare", "Spare"]),
        sp("710-LX500-4", "LX", "From 710-LX500-9", ["RSP-2", "Spare", "Spare"]),
    ])
    t = build_topology(d)
    assert group_labels(t) == ["500 BUS (LX)", "UNLINKED"]
    assert sorted(splitter_ids(t)) == ["710-LX500-1", "710-LX500-4"]


def test_blank_input_still_renders():
    d = design(splitters=[sp("710-KP-1", "KP", "", ["KEYPAD #2", "Spare", "Spare"])])
    t = build_topology(d)
    assert group_labels(t) == ["UNLINKED"]
    assert splitter_ids(t) == ["710-KP-1"]


def test_output_target_claimed_by_a_bus_is_not_expanded_twice():
    """LX1 points at LX2, but LX2 is itself fed by a bus.

    LX2 belongs to its bus group; LX1's port shows a link to it rather than a
    second copy of the subtree.
    """
    d = design(splitters=[
        sp("710-LX500-1", "LX", "500 BUS IN FROM XR/550",
           ["To 710-LX500-2", "Spare", "Spare"]),
        sp("710-LX500-2", "LX", "600 BUS IN FROM XR/550", ["RSP-1", "Spare", "Spare"]),
    ])
    t = build_topology(d)
    assert splitter_ids(t).count("710-LX500-2") == 1
    assert any(n.kind == "link" and n.ref == "710-LX500-2" for n in walk(t))


def test_legacy_and_unrecognised_output_tokens():
    """'RSP 1' is the pre-normalisation spelling; 'To <gone>' and free text
    must degrade to visible markers, never vanish."""
    d = design(
        splitters=[sp("710-LX500-1", "LX", "500 BUS IN FROM XR/550",
                      ["RSP 1", "To 710-LX500-77", "SOMETHING ELSE"])],
        rsps=[RSP(number=1, zones=[501, 516])],
    )
    kinds = {n.kind for n in walk(build_topology(d))}
    nodes = walk(build_topology(d))
    assert any(n.kind == "rsp" and n.label == "RSP-1" for n in nodes)
    assert "missing" in kinds
    assert "other" in kinds


# -------- degenerate inputs --------

def test_empty_design():
    t = build_topology(design())
    assert t.groups == []
    assert t.root_label == "XR550 PANEL"


def test_bare_design_without_site_info():
    assert build_topology(DMPDesign()).root_meta == ""
