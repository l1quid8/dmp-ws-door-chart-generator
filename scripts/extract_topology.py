"""
Spike: extract topology (splitters, RSPs, keypads, connections) from a riser-diagram PDF page.

Strategy:
  1. Read all text spans with bboxes from page 11 (the riser diagram)
  2. Identify "anchor" labels — device IDs (RSP1, 710-LX500-1, KEYPAD 3, MSP, SERVICE KP)
  3. For each anchor, find nearby labels (location strings, addresses, cable types)
  4. Read vector lines from the page; for each line, check which two device bboxes its endpoints touch
  5. Build a directed topology graph from those edges

This is classical computer vision — no ML, no API calls, no language models.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz


# -------- data structures --------

@dataclass
class TextSpan:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def distance_to(self, other: "TextSpan") -> float:
        return math.hypot(self.cx - other.cx, self.cy - other.cy)


@dataclass
class Device:
    """A device on the riser diagram (RSP, splitter, keypad, MSP)."""
    kind: str            # "RSP" | "SPLITTER" | "KEYPAD" | "MSP" | "SERVICE_KP"
    id: str              # "RSP1", "710-LX500-1", "KEYPAD 3", "MSP"
    anchor: TextSpan     # the label that identifies this device
    location: Optional[str] = None     # e.g. "ADMIN BUILDING (A/V ROOM)"
    address: Optional[str] = None      # e.g. "01" (for keypads)
    location_inherited: bool = False   # True if location was inferred from nearest device
    nearby_labels: list[str] = field(default_factory=list)


@dataclass
class Edge:
    """A connection between two devices (a line on the riser)."""
    src: Device
    dst: Device
    cable: Optional[str] = None  # nearest cable-type label along the line


# -------- text classification --------

DEVICE_PATTERNS = [
    # (regex, device kind)
    (re.compile(r"^MSP$"),                                      "MSP"),
    (re.compile(r"^RSP\s*(\d+)$"),                              "RSP"),
    (re.compile(r"^710[- ]?(LX\d+)[- ]?(\d+)$", re.IGNORECASE), "SPLITTER"),
    (re.compile(r"^710[- ]?(KP)[- ]?(\d+)$", re.IGNORECASE),    "SPLITTER"),
    (re.compile(r"^KEYPAD\s*(\d+)$", re.IGNORECASE),            "KEYPAD"),
    (re.compile(r"^SERVICE\s*KP$", re.IGNORECASE),              "SERVICE_KP"),
]

# Strings that look like locations: usually all-caps building names with optional parenthetical
LOCATION_PATTERN = re.compile(r"^[A-Z][A-Z0-9 #\-]+(?:\s*\([A-Z][A-Z0-9 \-/'#]+\))?$")

# Cable type labels: e.g. (N)(1)WP240(R), (E)(6)WP240(R), (N)(11)AQC240
CABLE_PATTERN = re.compile(r"^\([NE]\)\(\d+\)[A-Z0-9]+(?:\(\w\))?$")

# Address pattern
ADDRESS_PATTERN = re.compile(r"^ADDRESS:\s*(\d+)$", re.IGNORECASE)


def classify_label(text: str) -> str:
    """Returns 'device:KIND', 'cable', 'address', 'location', or 'noise'."""
    s = text.strip()
    for pat, kind in DEVICE_PATTERNS:
        if pat.match(s):
            return f"device:{kind}"
    if CABLE_PATTERN.match(s):
        return "cable"
    if ADDRESS_PATTERN.match(s):
        return "address"
    if LOCATION_PATTERN.match(s) and len(s) < 80 and not s.isdigit():
        # Filter obvious noise — keypad-display chrome, single-word device-type words, etc.
        noise_words = {
            "SPLITTER", "KEYPAD", "MSP", "RSP", "PANEL",  # device-type words (not locations)
            "FRI", "PM", "AM",                              # day/time on keypad displays
            "BACK", "ENTER", "RESET", "POWER", "ARMED",     # keypad display chrome
            "CHIME", "PERIM", "SLEEP", "ALL", "HOME", "CMD",
        }
        if s.upper() in noise_words:
            return "noise"
        if any(noise in s.split() for noise in ["ABC", "DEF", "GHI", "JKL", "MNO", "PQR", "STU", "VWX", "YZ"]):
            return "noise"
        return "location"
    return "noise"


def merge_multiline_locations(spans: list[TextSpan]) -> list[TextSpan]:
    """Riser-diagram location strings often span 2 lines (e.g. "ADMIN BUILDING" / "(A/V ROOM)").
    Merge these into single TextSpans by joining vertically-adjacent labels with same x-center."""
    out: list[TextSpan] = []
    used = set()
    spans_sorted = sorted(enumerate(spans), key=lambda iv: (iv[1].cx, iv[1].cy))
    for i, sp in enumerate(spans):
        if i in used: continue
        if classify_label(sp.text) != "location":
            out.append(sp)
            continue
        # Look for spans directly below (within ~25 y-units, similar x-center) that look like
        # the second line of the same location (e.g. parenthetical room name).
        merged_text = sp.text
        merged_y1 = sp.y1
        for j, other in enumerate(spans):
            if j == i or j in used: continue
            dx = abs(other.cx - sp.cx)
            dy = other.cy - sp.cy
            if dx < 30 and 0 < dy < 35 and other.text.startswith("("):
                merged_text += " " + other.text
                merged_y1 = max(merged_y1, other.y1)
                used.add(j)
                break  # only one continuation line
        if merged_text != sp.text:
            out.append(TextSpan(text=merged_text, x0=sp.x0, y0=sp.y0, x1=sp.x1, y1=merged_y1))
            used.add(i)
        else:
            out.append(sp)
    return out


def merge_horizontal_locations(spans: list[TextSpan]) -> list[TextSpan]:
    """Merge same-line location fragments (e.g. 'ADMIN' + 'BUILDING' -> 'ADMIN BUILDING').

    PyMuPDF often splits multi-word labels into per-word spans; this re-assembles them
    so downstream classification matches the full building name. Groups consecutive
    location-classified spans on the same y-line (within ±4pt) with horizontal gaps
    under 50pt, in left-to-right order.
    """
    by_y: dict[int, list[tuple[int, TextSpan]]] = {}
    for i, sp in enumerate(spans):
        if classify_label(sp.text) == "location":
            by_y.setdefault(round(sp.cy / 3) * 3, []).append((i, sp))

    used: set[int] = set()
    merged_spans: list[TextSpan] = []
    for group in by_y.values():
        group.sort(key=lambda iv: iv[1].cx)
        i = 0
        while i < len(group):
            base_idx, base = group[i]
            text = base.text
            x1, y1 = base.x1, base.y1
            j = i + 1
            while j < len(group):
                next_idx, next_sp = group[j]
                if next_sp.x0 - x1 < 50 and abs(next_sp.cy - base.cy) < 4:
                    text += " " + next_sp.text
                    used.add(next_idx)
                    x1, y1 = next_sp.x1, max(y1, next_sp.y1)
                    j += 1
                else:
                    break
            if j > i + 1:
                merged_spans.append(TextSpan(text=text, x0=base.x0, y0=base.y0, x1=x1, y1=y1))
                used.add(base_idx)
            i = j

    out: list[TextSpan] = list(merged_spans)
    for k, sp in enumerate(spans):
        if k not in used:
            out.append(sp)
    return out


def extract_spans(pdf_path: Path, page_idx: int) -> list[TextSpan]:
    """Get every text span from a specific page."""
    doc = fitz.open(str(pdf_path))
    page = doc[page_idx]
    d = page.get_text("dict")
    spans: list[TextSpan] = []
    for block in d["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for sp in line.get("spans", []):
                t = sp.get("text", "").strip()
                if not t:
                    continue
                bbox = sp["bbox"]
                spans.append(TextSpan(text=t, x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3]))
    doc.close()
    return spans


def cluster_devices(spans: list[TextSpan], close_threshold: float = 50.0, far_threshold: float = 200.0) -> list[Device]:
    """For every device-anchor span, gather nearby labels (location/address/cable).

    Two-pass strategy:
      Pass A: tight proximity (~50pt) catches direct labels — works great for RSPs/keypads/MSP
              where the building name sits right next to the device on the riser.
      Pass B: for any device still missing a location, fall back to the location of the
              nearest *other device* (within far_threshold). This handles splitters whose
              building label is at the room perimeter, far from the splitter symbol itself.
    """
    # Find anchor spans + other spans
    anchors: list[tuple[TextSpan, str]] = []
    other_spans: list[tuple[TextSpan, str]] = []
    for sp in spans:
        cls = classify_label(sp.text)
        if cls.startswith("device:"):
            anchors.append((sp, cls.split(":")[1]))
        else:
            other_spans.append((sp, cls))

    # PASS A — for each device, find the closest location/address within tight threshold
    devices: list[Device] = []
    for anchor, kind in anchors:
        dev = Device(kind=kind, id=anchor.text.strip(), anchor=anchor)
        candidates: dict[str, tuple[float, str]] = {}
        for sp, cls in other_spans:
            if cls == "noise":
                continue
            d = anchor.distance_to(sp)
            if d > close_threshold:
                continue
            if cls not in candidates or d < candidates[cls][0]:
                candidates[cls] = (d, sp.text)
        if "location" in candidates:
            dev.location = candidates["location"][1]
        if "address" in candidates:
            m = ADDRESS_PATTERN.match(candidates["address"][1])
            if m: dev.address = m.group(1)
        devices.append(dev)

    # PASS B — for devices still missing location (typically splitters), inherit from nearest other
    # device that has a NATIVE (non-inherited) location. This avoids cascade-inheriting.
    for dev in devices:
        if dev.location:
            continue
        best_distance = float("inf")
        best_loc = None
        for other in devices:
            if other is dev or not other.location or other.location_inherited:
                continue
            d = dev.anchor.distance_to(other.anchor)
            if d < best_distance and d < far_threshold:
                best_distance = d
                best_loc = other.location
        if best_loc:
            dev.location = best_loc
            dev.location_inherited = True

    # PASS C — for devices still missing location (e.g. splitter trios on a page with no
    # other device anchors), look for the closest 'location'-classified text span directly.
    # Bias toward labels above the device, since riser-diagram convention puts the room
    # label above the equipment that sits in it. Boost spans containing 'BUILDING'/'BLDG'
    # so a real room name wins over stray fragments like 'NG'.
    location_spans = [sp for sp in spans if classify_label(sp.text) == "location"]
    for dev in devices:
        if dev.location:
            continue
        best_score = float("inf")
        best_loc = None
        for sp in location_spans:
            dx = abs(sp.cx - dev.anchor.cx)
            dy = dev.anchor.cy - sp.cy  # positive => label above device (preferred)
            # Splitter trios on a riser sit side-by-side under a single room label that
            # may be horizontally offset from the rightmost splitter — keep dx generous.
            if dy < -50 or dx > 250:
                continue
            penalty = math.hypot(dx, dy if dy < 0 else dy * 0.5)
            if any(kw in sp.text.upper() for kw in ("BUILDING", "BLDG")):
                penalty *= 0.5
            if penalty < best_score:
                best_score = penalty
                best_loc = sp.text
        if best_loc and best_score < 200:
            dev.location = best_loc
            dev.location_inherited = True

    return devices


# -------- Phase 3: vector-line edge detection --------

class UnionFind:
    """Simple union-find for merging connected line segments into polylines."""
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x: int, y: int):
        px, py = self.find(x), self.find(y)
        if px == py: return
        if self.rank[px] < self.rank[py]:
            px, py = py, px
        self.parent[py] = px
        if self.rank[px] == self.rank[py]:
            self.rank[px] += 1


def extract_line_segments(pdf_path: Path, page_idx: int) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Extract all line segments from a PDF page's vector graphics.

    Returns a list of (start_point, end_point) tuples.
    """
    doc = fitz.open(str(pdf_path))
    page = doc[page_idx]
    drawings = page.get_drawings()

    segments = []
    for d in drawings:
        if d.get('type') != 's':  # Only strokes (lines/curves)
            continue
        items = d.get('items', [])
        for item in items:
            if item[0] == 'l':  # line segment
                p1, p2 = item[1], item[2]
                segments.append(((p1.x, p1.y), (p2.x, p2.y)))

    doc.close()
    return segments


def snap_point(p: tuple[float, float], grid_size: float = 2.0) -> tuple[float, float]:
    """Snap a point to a grid for merging near-duplicate endpoints."""
    return (round(p[0] / grid_size) * grid_size, round(p[1] / grid_size) * grid_size)


def build_polylines(segments: list[tuple[tuple[float, float], tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Build polylines from line segments by merging connected segments.

    Returns a list of polylines, where each polyline is a list of points.
    """
    if not segments:
        return []

    # Snap all endpoints to grid and build adjacency
    snapped_segs = []
    point_set = set()
    for p1, p2 in segments:
        sp1 = snap_point(p1)
        sp2 = snap_point(p2)
        snapped_segs.append((sp1, sp2))
        point_set.add(sp1)
        point_set.add(sp2)

    points_list = sorted(point_set)
    point_to_idx = {p: i for i, p in enumerate(points_list)}

    # Build graph: each segment is an edge between its two endpoints
    uf = UnionFind(len(points_list))
    for p1, p2 in snapped_segs:
        i1, i2 = point_to_idx[p1], point_to_idx[p2]
        uf.union(i1, i2)

    # Group points by connected component
    components = {}
    for p, idx in point_to_idx.items():
        root = uf.find(idx)
        if root not in components:
            components[root] = []
        components[root].append(p)

    # For each component, build polyline(s) using DFS along segment edges
    polylines = []
    for comp_points in components.values():
        if len(comp_points) < 2:
            continue

        # Build adjacency within component
        comp_set = set(comp_points)
        adj = {p: [] for p in comp_points}
        for p1, p2 in snapped_segs:
            if p1 in comp_set and p2 in comp_set:
                adj[p1].append(p2)
                adj[p2].append(p1)

        # Find endpoints (degree 1) or pick any start if cycle
        endpoints = [p for p in comp_points if len(adj[p]) == 1]
        start = endpoints[0] if endpoints else comp_points[0]

        # Trace polyline from start
        path = [start]
        visited = {start}
        current = start
        while True:
            neighbors = [p for p in adj[current] if p not in visited]
            if not neighbors:
                break
            current = neighbors[0]
            path.append(current)
            visited.add(current)

        if len(path) > 1:
            polylines.append(path)

    return polylines


def find_device_in_bbox(point: tuple[float, float], devices: list[Device], tolerance: float = 80.0) -> Optional[Device]:
    """Check if a point falls within any device's bounding box (anchor ± tolerance)."""
    x, y = point
    for dev in devices:
        cx, cy = dev.anchor.cx, dev.anchor.cy
        if abs(x - cx) <= tolerance and abs(y - cy) <= tolerance:
            return dev
    return None


def find_closest_device(point: tuple[float, float], devices: list[Device], max_distance: float = 150.0) -> Optional[Device]:
    """Find the closest device to a point, if within max_distance."""
    x, y = point
    best_dev = None
    best_dist = max_distance
    for dev in devices:
        cx, cy = dev.anchor.cx, dev.anchor.cy
        dist = math.hypot(x - cx, y - cy)
        if dist < best_dist:
            best_dist = dist
            best_dev = dev
    return best_dev


def reconstruct_edges(segments: list[tuple[tuple[float, float], tuple[float, float]]],
                      devices: list[Device],
                      spans: list[TextSpan],
                      max_endpoint_distance: float = 120.0) -> list[Edge]:
    """Reconstruct edges by finding which devices the polyline endpoints connect to.

    Uses closest-device matching: for each polyline endpoint, find the closest device
    within max_endpoint_distance. This is more robust than fixed-tolerance matching.

    Args:
        segments: list of (start, end) line segments
        devices: list of identified devices with anchors
        spans: all text spans (for nearest cable label lookup)
        max_endpoint_distance: max distance from endpoint to device anchor to consider

    Returns:
        list of Edge objects with source, destination, and cable label (deduplicated)
    """
    polylines = build_polylines(segments)
    edges_set = set()  # Use set to deduplicate (src_id, dst_id) pairs
    edges = []

    for poly in polylines:
        if len(poly) < 2:
            continue

        # Get endpoints of polyline
        start_pt = poly[0]
        end_pt = poly[-1]

        # Find closest device to each endpoint (if within max_endpoint_distance)
        start_dev = find_closest_device(start_pt, devices, max_endpoint_distance)
        end_dev = find_closest_device(end_pt, devices, max_endpoint_distance)

        if not start_dev or not end_dev or start_dev == end_dev:
            continue  # Skip lines that don't connect two different devices

        # Determine direction: if one is RSP/KEYPAD, the other (splitter) is source
        if start_dev.kind in ("RSP", "KEYPAD", "SERVICE_KP") and end_dev.kind == "SPLITTER":
            src, dst = end_dev, start_dev
        elif end_dev.kind in ("RSP", "KEYPAD", "SERVICE_KP") and start_dev.kind == "SPLITTER":
            src, dst = start_dev, end_dev
        elif start_dev.kind == "SPLITTER" and end_dev.kind == "SPLITTER":
            # Both splitters: typically, the one FURTHER DOWN (larger Y) is the main/upstream,
            # and outputs flow downward to other splitters. Use ID to disambiguate if Y is same.
            # (This is a heuristic; the DMP worksheet is the ground truth.)
            if start_dev.anchor.cy > end_dev.anchor.cy:
                src, dst = start_dev, end_dev
            elif end_dev.anchor.cy > start_dev.anchor.cy:
                src, dst = end_dev, start_dev
            else:  # Same Y; use ID number as tiebreaker (e.g., LX-1 before LX-2)
                # Extract splitter number (LX 710-1 -> 1, LX 710-2 -> 2)
                start_num = int(''.join(filter(str.isdigit, start_dev.id.split('-')[-1])) or '0')
                end_num = int(''.join(filter(str.isdigit, end_dev.id.split('-')[-1])) or '0')
                if start_num < end_num:
                    src, dst = start_dev, end_dev
                else:
                    src, dst = end_dev, start_dev
        elif start_dev.kind in ("RSP", "KEYPAD", "SERVICE_KP") and end_dev.kind in ("RSP", "KEYPAD", "SERVICE_KP"):
            # Both are terminals; skip (shouldn't happen on riser)
            continue
        else:
            # Fallback: use polyline direction heuristic
            if start_pt[0] < end_pt[0]:
                src, dst = start_dev, end_dev
            else:
                src, dst = end_dev, start_dev

        # Deduplicate by (src.id, dst.id) to avoid reporting the same edge multiple times
        edge_key = (src.id, dst.id)
        if edge_key in edges_set:
            continue
        edges_set.add(edge_key)

        # Find nearest cable label along the polyline
        cable = None
        midpoint_x = (start_pt[0] + end_pt[0]) / 2
        midpoint_y = (start_pt[1] + end_pt[1]) / 2
        best_dist = 50.0  # tolerance for cable label proximity
        for sp in spans:
            cls = classify_label(sp.text)
            if cls != "cable":
                continue
            dist = math.hypot(sp.cx - midpoint_x, sp.cy - midpoint_y)
            if dist < best_dist:
                best_dist = dist
                cable = sp.text

        edges.append(Edge(src=src, dst=dst, cable=cable))

    return edges


def extract_full_topology(pdf_path: Path, page_idx: int = 10) -> tuple[list[Device], list[Edge]]:
    """Full Phase 1+2+3 pipeline: extract devices and edges from a riser-diagram page.

    Args:
        pdf_path: path to PDF
        page_idx: 0-indexed page number (default 10 = page 11)

    Returns:
        (devices, edges) tuple. Gracefully degrades: if Phase 3 fails, returns Phase 2 devices with empty edges.
    """
    # Phase 1+2: extract text and cluster devices
    spans = extract_spans(pdf_path, page_idx)
    spans = merge_multiline_locations(spans)
    devices = cluster_devices(spans)

    # Phase 3: edge detection
    try:
        segments = extract_line_segments(pdf_path, page_idx)
        edges = reconstruct_edges(segments, devices, spans)
    except Exception as e:
        print(f"Warning: Phase 3 edge detection failed: {e}")
        edges = []

    return devices, edges


# -------- CLI --------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python extract_topology.py <pdf_path> [page_index=10]")
        sys.exit(1)
    pdf_path = Path(sys.argv[1])
    page_idx = int(sys.argv[2]) if len(sys.argv) > 2 else 10  # page 11 = index 10

    print(f"Reading page {page_idx + 1} of {pdf_path}...")
    spans = extract_spans(pdf_path, page_idx)
    print(f"  Found {len(spans)} text spans")
    spans = merge_multiline_locations(spans)
    print(f"  After merging multi-line locations: {len(spans)} spans")

    # Show the classified spans for debugging
    print("\n=== Classified spans (location, cable, address only — device anchors handled separately) ===")
    by_kind: dict[str, int] = {}
    for sp in spans:
        cls = classify_label(sp.text)
        by_kind[cls] = by_kind.get(cls, 0) + 1
    for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")

    devices = cluster_devices(spans)
    print(f"\n=== {len(devices)} devices identified ===")
    for d in devices:
        loc = d.location or "?"
        marker = "(inherited)" if d.location_inherited else ""
        addr = f" addr={d.address}" if d.address else ""
        print(f"  [{d.kind:<11}] {d.id:<20} @ ({d.anchor.cx:>5.0f},{d.anchor.cy:>5.0f})  → {loc} {marker}{addr}")

    # Phase 3: extract edges
    print(f"\n=== Phase 3: Edge Detection ===")
    segments = extract_line_segments(pdf_path, page_idx)
    print(f"  Extracted {len(segments)} line segments")
    polylines = build_polylines(segments)
    print(f"  Built {len(polylines)} polylines")
    edges = reconstruct_edges(segments, devices, spans)
    print(f"  Reconstructed {len(edges)} edges")

    if edges:
        print("\n=== Reconstructed Edges ===")
        for e in edges:
            cable_str = f" [{e.cable}]" if e.cable else ""
            print(f"  {e.src.id} → {e.dst.id}{cable_str}")

    # Validation against the O'Melveny ground-truth (from the DMP worksheet)
    print("\n=== Validation against O'Melveny DMP worksheet (where applicable) ===")
    ground_truth_devices = {
        "MSP":          "Admin Bldg AV Room",
        "RSP1":         "Admin Bldg AV Room",
        "RSP2":         "Assembly Bldg Storage",
        "RSP3":         "Bldg A-1009 CLRM 29",
        "RSP4":         "Classroom Bldg #2 Ceramic Room",
        "710-LX500-1":  "Admin Bldg AV Room",
        "710-LX500-2":  "Admin Bldg AV Room",
        "710-LX500-3":  "Assembly Bldg Storage",
        "710-KP-1":     "Admin Bldg AV Room",
    }
    for d in devices:
        gt = ground_truth_devices.get(d.id)
        if not gt: continue
        match = "✓" if d.location and gt.split()[0].upper() in d.location.upper() else "?"
        print(f"  {match}  {d.id:<15} got: {d.location!r:<40} expected: {gt!r}")

    # Validate reconstructed edges against ground-truth (visible connections only)
    print("\n=== Edge Validation ===")
    # Ground truth uses normalized IDs (no spaces)
    ground_truth_visible = {
        ("710-LX500-1", "RSP1"),
        ("710-LX500-1", "710-LX500-3"),
        ("710-LX500-1", "710-LX500-2"),
        ("710-LX500-2", "RSP4"),
        ("710-LX500-3", "RSP3"),
        ("710-LX500-3", "RSP2"),
        ("710-KP-1", "KEYPAD2"),
        ("710-KP-1", "KEYPAD3"),
        ("710-KP-1", "KEYPAD4"),
    }

    def normalize_id(dev_id: str) -> str:
        """Normalize device ID by removing spaces (e.g. 'KEYPAD 3' -> 'KEYPAD3')."""
        return dev_id.replace(" ", "")

    # Convert reconstructed edges to normalized form for comparison
    reconstructed_pairs = {(normalize_id(e.src.id), normalize_id(e.dst.id)) for e in edges}
    correct = reconstructed_pairs & ground_truth_visible
    missed = ground_truth_visible - correct
    extra = reconstructed_pairs - ground_truth_visible

    print(f"  Correctly reconstructed: {len(correct)}/{len(ground_truth_visible)} visible ground-truth edges")
    if correct:
        for src, dst in sorted(correct):
            print(f"    ✓ {src} → {dst}")
    if missed:
        print(f"  Missed: {len(missed)} edges (likely not drawn as visible lines)")
        for src, dst in sorted(missed):
            print(f"    ✗ {src} → {dst}")
    if extra:
        print(f"  Extra: {len(extra)} edges reconstructed (not in DMP worksheet)")
        for src, dst in sorted(extra):
            print(f"    + {src} → {dst}")

    print(f"\n  Polylines detected: {len(polylines)}")
    print(f"  Line segments extracted: {len(segments)}")
