# engine/container_report.py  — v4
# ─────────────────────────────────────────────────────────────────────────────
# Generates PDF and PPTX container loading-plan reports matching the
# John Deere "Density Analysis" PPT reference style:
#
#   ┌────────────────────────────────────────────────────────────────┐
#   │  Title: "Density Analysis - 40 HC"                              │
#   │  ┌───────────────────────┐        ┌─────────────────────────┐  │
#   │  │ Rack ID | L-wise |     │        │                         │  │
#   │  │ W-wise | H-wise | Qty  │        │       ISO VIEW          │  │
#   │  │ | Pkg Wt | Net Wt      │        │                         │  │
#   │  └───────────────────────┘        └─────────────────────────┘  │
#   │  ┌─────────────────────────────┐                                │
#   │  │                             │        ┌──────────────┐        │
#   │  │        TOP VIEW             │        │  SIDE VIEW   │        │
#   │  │                             │        │              │        │
#   │  └─────────────────────────────┘        └──────────────┘        │
#   │  ┌─────────────────────────────┐                                │
#   │  │       FRONT VIEW            │       [ Legend swatches ]      │
#   │  └─────────────────────────────┘                                │
#   │  John Deere | Logistics Engineering |          Company Use      │
#   └────────────────────────────────────────────────────────────────┘
#
# Container is rendered with CLOSED doors (solid end walls, no door gap/
# cross-bars) — looks identical from all four sides except colour shading
# from the simulated light source.  No mm dimension labels are shown
# anywhere in the rendered views.
#
# Title automatically becomes "Density Analysis - 20 HC", "- 40 HC",
# "- 40 GP" etc. based on the `container_label` you pass in.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations
import io
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import math
from matplotlib.gridspec import GridSpec
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm


# ══════════════════════════════════════════════════════════════════════════════
#  COLOUR PALETTE  (one distinct colour per rack type)
# ══════════════════════════════════════════════════════════════════════════════

_RACK_COLORS_HEX = [
    "#2980B9", "#27AE60", "#E67E22", "#8E44AD", "#C0392B",
    "#16A085", "#F1C40F", "#2C3E50", "#1ABC9C", "#D35400",
    "#7F8C8D", "#E74C3C", "#3498DB", "#9B59B6", "#2ECC71",
    "#B9770E", "#117864", "#6C3483", "#A93226", "#1F618D",
    "#0E6251", "#943126", "#7D6608", "#4A235A", "#0B5345",
    "#154360", "#78281F", "#186A3B", "#B03A2E", "#5B2C6F",
]

_JD_GREEN  = "#367C2B"   # John Deere green
_COMPANY_RED = "#C0392B"

def _hex_to_rgb01(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))

def _rack_color(idx: int) -> str:
    return _RACK_COLORS_HEX[idx % len(_RACK_COLORS_HEX)]

def _lighten(rgb, f):
    return tuple(min(1.0, c + (1 - c) * f) for c in rgb)

def _darken(rgb, f):
    return tuple(c * (1 - f) for c in rgb)


# ══════════════════════════════════════════════════════════════════════════════
#  SKYLINE FLOOR PACKER (for the loading-plan drawing)
#  Fills the container the way it is really loaded: each footprint rests at the
#  lowest (nose-most) free spot across the width, so long racks anchor one lane
#  while shorter racks fill the other lane independently. This avoids the old
#  "row = longest rack" waste that left gaps and pushed a box past the doors.
# ══════════════════════════════════════════════════════════════════════════════

_SKEPS = 1e-6

def _sky_base(sky, x, w):
    """Highest filled length under the width span [x, x+w] (where a box rests)."""
    a, b = x, x + w
    m = 0.0
    for x0, x1, y in sky:
        if x1 <= a + _SKEPS or x0 >= b - _SKEPS:
            continue
        if y > m:
            m = y
    return m

def _sky_raise(sky, a, b, newy):
    """Set the width span [a, b] to filled-length newy; keep the rest; merge."""
    out = []
    for x0, x1, y in sky:
        if x0 < a:
            out.append([x0, min(x1, a), y])
        if x1 > b:
            out.append([max(x0, b), x1, y])
        lo, hi = max(x0, a), min(x1, b)
        if hi > lo:
            out.append([lo, hi, newy])
    out = [s for s in out if s[1] - s[0] > _SKEPS]
    out.sort()
    merged = [out[0]]
    for s in out[1:]:
        if abs(s[2] - merged[-1][2]) < 1e-6 and abs(s[0] - merged[-1][1]) < _SKEPS:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    sky[:] = merged

def _sky_place(sky, w, l, CW, CL):
    """
    Place a footprint of width w, length l. Returns (x, y) of its nose corner.
    Chooses the position that rests lowest (nearest the nose), tie-broken left.
    """
    xs = sorted({seg[0] for seg in sky} | {CW - w})
    best = None
    for x in xs:
        if x < -_SKEPS or x + w > CW + _SKEPS:
            continue
        base = _sky_base(sky, x, w)
        if base + l > CL + _SKEPS:
            continue
        key = (round(base, 3), round(x, 3))
        if best is None or key < best[0]:
            best = (key, x, base)
    if best is None:                       # nothing fits within length: least-bad spot
        cand = [x for x in xs if -_SKEPS <= x and x + w <= CW + _SKEPS]
        if not cand:
            cand = [0.0]
        x = min(cand, key=lambda xx: _sky_base(sky, xx, w))
        base = _sky_base(sky, x, w)
    else:
        _, x, base = best
    _sky_raise(sky, x, x + w, base + l)
    return x, base


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT BUILDER
#  Reproduces strip-pass placement geometry AND computes the
#  Length-wise / Width-wise / Height-wise counts for the summary table.
# ══════════════════════════════════════════════════════════════════════════════

def _skyline_layout(stacks, CW, CL):
    """Lane/skyline placement (natural orientation) -> [(stk, x, y, iW, iL)]."""
    sky = [[0.0, CW, 0.0]]
    out = []
    for s in stacks:
        s_l, s_w = s[0]["L"], s[0]["W"]
        if s_w <= CW + _SKEPS:
            iW, iL = s_w, s_l
        elif s_l <= CW + _SKEPS:
            iW, iL = s_l, s_w
        else:
            iW, iL = s_w, s_l
        x, y = _sky_place(sky, iW, iL, CW, CL)
        out.append((s, x, y, iW, iL))
    return out


def _maxrects_layout(stacks, CW, CL):
    """
    Placement using the SAME MaxRects packer the container packer uses, so the
    drawing reproduces a real, tight, feasible layout. Returns
    [(stk, x, y, iW, iL)] or None if any footprint could not be placed.
    """
    from engine.geometry import MaxRectsBin
    order = sorted(stacks, key=lambda s: s[0]["L"] * s[0]["W"], reverse=True)
    bin_ = MaxRectsBin(CW, CL)
    out = []
    for s in order:
        L, W = s[0]["L"], s[0]["W"]
        r = bin_.place_located(W, L, allow_rotate=True)   # W=width, L=length
        if r is None:
            return None
        x, y, iW, iL = r
        out.append((s, x, y, iW, iL))
    return out


def _block_layout(stacks, CW, CL):
    """
    Group each rack footprint into a compact rectangular BLOCK (a grid of that
    rack, oriented to fit the most across the width in the shortest length),
    then pack the few blocks into the container with the MaxRects packer. This
    finds arrangements that fixed full-length lanes miss — e.g. a 2-wide block
    of long racks up front with a block of sideways racks filling the back.
    Returns [(stk, x, y, iW, iL)] or None if the blocks don't all fit.
    """
    from collections import defaultdict
    from engine.geometry import MaxRectsBin
    groups = defaultdict(list)
    for s in stacks:
        groups[(round(s[0]["L"], 1), round(s[0]["W"], 1))].append(s)

    blocks = []
    for (L, W), members in groups.items():
        N = len(members)
        best = None
        for across, along in ((W, L), (L, W)):
            cols = int(CW // across)
            if cols < 1:
                continue
            rows = -(-N // cols)            # ceil
            bL = rows * along
            if bL > CL + 1.0:
                continue
            if best is None or bL < best[0]:
                best = (bL, across, along, cols, cols * across)
        if best is None:
            return None
        bL, across, along, cols, bW = best
        blocks.append({"W": bW, "L": bL, "members": members,
                       "across": across, "along": along, "cols": cols})

    binp = MaxRectsBin(CW, CL)
    blocks.sort(key=lambda b: b["W"] * b["L"], reverse=True)
    placed = []
    for b in blocks:
        r = binp.place_located(b["W"], b["L"], allow_rotate=False)
        if r is None:
            return None
        bx, by, _bw, _bl = r
        for j, s in enumerate(b["members"]):
            col = j % b["cols"]
            row = j // b["cols"]
            placed.append((s, bx + col * b["across"], by + row * b["along"],
                           b["across"], b["along"]))
    return placed


def _max_extent(placed):
    return max((y + iL) for _, x, y, iW, iL in placed) if placed else float("inf")


def _build_layout_and_counts(load: dict, dims: dict, container: dict,
                             rack_color_map: dict):
    """
    Returns
    -------
    records : list of PER-UNIT cuboid records — one per individual box, at its
               own (x, y, z). Boxes that stack share a floor position and sit on
               top of one another (heavier at the bottom), following the same
               rules the packer uses (see engine/stacking.py).
               Each record: {name, color, x, y, z, iW, iL, h}
    counts  : { rack_name: {"length_wise": int, "width_wise": int,
                            "height_wise": int, "net_qty": int} }
    """
    from collections import Counter
    from engine.stacking import build_stacks, stack_height_mm, is_metal_rack, METAL_NEST_MM

    CW = float(container["W"])
    CL = float(container["L"])
    CH = float(container["H"])
    MWT = float(container.get("MAX_WT", float("inf")))

    # Build the same vertical stacks the packer built (heavier at the bottom,
    # footprint match, stackability / weight / material rules).
    stacks = build_stacks(load, dims, CH, MWT)
    # Tile same-footprint stacks together (longest footprint first).
    stacks.sort(key=lambda s: (s[0]["L"], s[0]["W"]), reverse=True)

    records = []
    positions = []
    EPS = 1e-6

    # Decide floor placements: a list of (stk, x, y, iW, iL).
    foots = {(round(s[0]["L"], 1), round(s[0]["W"], 1)) for s in stacks}
    placed = []

    # A 2-way-access rack may not be rotated, so it must NOT use the
    # mixed-orientation column layout below (that lane mix rotates half the
    # racks). Those loads go to the access-aware layout finder instead.
    all_rotatable = all(s[0].get("acc", "4way") == "4way" for s in stacks)

    if len(foots) == 1 and stacks and all_rotatable:
        # ── Single footprint: optimal mixed-orientation column layout ────────
        # Fill the width with the best mix of a length-wise lane and a
        # width-wise lane (matches the packer, e.g. 1200x800 -> 10 + 15 = 25).
        rl, rw = next(iter(foots))
        capA = int(CL // rl) if rl > 0 else 0      # normal column (width rw)
        capB = int(CL // rw) if rw > 0 else 0      # rotated column (width rl)
        best = (0, 0, 0)
        max_a = int(CW // rw) if rw > 0 else 0
        for a in range(max_a + 1):
            rem = CW - a * rw
            b = int(rem // rl) if rl > 0 else 0
            if a * capA + b * capB > best[0]:
                best = (a * capA + b * capB, a, b)
        _, na, nb = best

        cols = []           # (x, iW, iL, cap)
        x = 0.0
        for _ in range(na):
            cols.append((x, rw, rl, capA)); x += rw
        for _ in range(nb):
            cols.append((x, rl, rw, capB)); x += rl

        # LOAD DISTRIBUTION: when the container is NOT full, spread the stacks
        # evenly across the columns instead of filling column 1 to capacity and
        # leaving a stub in column 2. Twelve racks in two columns should load
        # 6 + 6 (balanced side to side), not 10 + 2 — an off-centre load is bad
        # practice on the road. A full container is unaffected: every column
        # still fills to capacity.
        total_cap = sum(c[3] for c in cols)
        n = len(stacks)
        if cols and n < total_cap:
            share = [0] * len(cols)
            i = 0
            while sum(share) < n:                 # round-robin, respecting caps
                if share[i % len(cols)] < cols[i % len(cols)][3]:
                    share[i % len(cols)] += 1
                i += 1
                if i > n * len(cols) + len(cols):
                    break
        else:
            share = [c[3] for c in cols]

        si = 0
        for ci, (cx, ciW, ciL, cap) in enumerate(cols):
            take = min(share[ci] if ci < len(share) else cap, cap)
            for k in range(take):
                if si >= len(stacks):
                    break
                placed.append((stacks[si], cx, k * ciL, ciW, ciL))
                si += 1
            if si >= len(stacks):
                break
        if si < len(stacks):                        # safety fallback (rare)
            sky = [[0.0, CW, 0.0]]
            for _, cx, cy, ciW, ciL in placed:
                _sky_raise(sky, cx, cx + ciW,
                           max(_sky_base(sky, cx, ciW), cy + ciL))
            for j in range(si, len(stacks)):
                iW, iL = (rw, rl) if rw <= CW else (rl, rw)
                xx, yy = _sky_place(sky, iW, iL, CW, CL)
                placed.append((stacks[j], xx, yy, iW, iL))
    else:
        # ── Mixed footprints: prefer the LANE layout (matches the packer, and
        #    finds tight complementary-lane fits). If it can't place every stack
        #    within the walls, fall back to skyline, then to the MaxRects packer.
        # LOADING ACCESS: 2-way packages are stored already turned the correct
        # way and are marked non-rotatable, so the drawing shows them loading
        # only in their permitted direction (same rule the packer used).
        def _oriented(s):
            acc = s[0].get("acc", "4way")
            L, W = s[0]["L"], s[0]["W"]
            if acc == "width":
                L, W = W, L
            return L, W, (acc == "4way")

        stack_tuples = []
        for i, s in enumerate(stacks):
            L, W, rot = _oriented(s)
            stack_tuples.append((L, W, sum(b["wt"] for b in s), i, rot))
        from engine.geometry import find_layout

        # ONE shared layout finder, also used by the packer to validate each
        # container — so whatever the packer says fits, we can draw inside the
        # walls. Only if that somehow fails do we fall back to the older
        # layouts (which may not fill as tightly).
        pl = find_layout(stack_tuples, CW, CL)
        if pl is not None:
            placed = [(stacks[k], x, y, a, b) for (k, x, y, a, b) in pl]
        else:
            placed = _skyline_layout(stacks, CW, CL)
            if _max_extent(placed) > CL + EPS:
                mr = _maxrects_layout(stacks, CW, CL)
                if mr is not None and _max_extent(mr) < _max_extent(placed):
                    placed = mr

        if placed is None:
            placed = _skyline_layout(stacks, CW, CL)
            if _max_extent(placed) > CL + EPS:
                mr = _maxrects_layout(stacks, CW, CL)
                if mr is not None and _max_extent(mr) < _max_extent(placed):
                    placed = mr

    # Build per-unit records and per-position entries from the placements.
    for stk, x, y, iW, iL in placed:
        z = 0.0
        prev_mat = None
        for b in stk:
            b_mat = b.get("mat", "")
            # metal racks nest 38.1 mm into the metal rack below, so this box's
            # bottom drops by that much (keeps the drawing height honest).
            if prev_mat is not None and is_metal_rack(prev_mat) and is_metal_rack(b_mat):
                z -= METAL_NEST_MM
            records.append({
                "name": b["name"], "color": rack_color_map[b["name"]],
                "x": x, "y": y, "z": z, "iW": iW, "iL": iL, "h": b["H"],
            })
            z += b["H"]
            prev_mat = b_mat
        names = [b["name"] for b in stk]
        cnt = Counter(names)
        homog = (len(cnt) == 1)
        positions.append({
            "x": x, "y": y, "iW": iW, "iL": iL,
            "members": names, "stack_n": len(names),
            "rack": names[0] if homog else "MIXED",
            "homogeneous": homog, "counts": dict(cnt),
            "height": stack_height_mm(stk),
            "weight": sum(b["wt"] for b in stk),
            # Topmost box (stacks are bottom-first/top-last) -- that's what's
            # actually visible looking straight down, not the bottom one.
            "color": rack_color_map[names[-1]],
        })

    # loading order: NOSE (small y) first, then across the width (x)
    positions.sort(key=lambda p: (round(p["y"], 1), round(p["x"], 1)))
    for i, p in enumerate(positions, 1):
        p["seq"] = i

    # ── counts for the summary table ─────────────────────────────────────────
    # height_wise = tallest run of a rack within a single stack.
    max_run = {}
    for stk in stacks:
        for nm, cnt in Counter(b["name"] for b in stk).items():
            max_run[nm] = max(max_run.get(nm, 0), cnt)

    counts = {}
    for r, q in load.items():
        q = int(q)
        if q <= 0:
            continue
        r_w = float(dims[r]["Width (MM)"])
        r_l = float(dims[r]["Length (MM)"])
        a1 = int(CW // r_w) if r_w > 0 else 0
        a2 = int(CW // r_l) if r_l > 0 else 0
        fit_across = max(1, a1, a2)
        height_wise = max(1, max_run.get(r, 1))
        per = max(1, fit_across * height_wise)
        counts[r] = {
            "length_wise": -(-q // per),        # ceil(q / per)
            "width_wise":  fit_across,
            "height_wise": height_wise,
            "net_qty":     q,
        }

    return records, counts, positions


# ══════════════════════════════════════════════════════════════════════════════
#  CONTAINER 3-D RENDERER  (closed-door look, no dimension labels)
# ══════════════════════════════════════════════════════════════════════════════

_CONT_DARK   = (0.50, 0.12, 0.01)
_CONT_MID    = (0.62, 0.18, 0.03)
_CONT_LIGHT  = (0.70, 0.22, 0.04)
_FLOOR_COL   = (0.80, 0.80, 0.80)   # neutral light gray — never collides with rack colours
_EMPTY_COL   = (0.88, 0.88, 0.88)   # very light gray for empty floor/elevation space in 2-D views
_FRAME_COL   = "#180800"


# ─────────────────────────────────────────────────────────────────────────────
#  SIDE-VIEW (DOOR-VIEW) PROJECTION SETTINGS
# ─────────────────────────────────────────────────────────────────────────────
# The SIDE VIEW looks straight in through the container DOORS. It is rendered as
# a true back-to-front projection: a rack nearer the door hides whatever sits
# directly behind it (across its full width), but where a near rack does NOT
# cover a width position, the rack behind it remains visible there.
#
# Example (matches the reference sheet): a single layer of green at the door
# only spans the left part of the width, so you see GREEN on the left and the
# ORANGE behind it on the right — exactly what you'd see standing at the doors.

# Which end the doors are on. Packing fills nose (y=0) -> door (y=CL), so the
# door-facing racks have the LARGEST y. Set False if your convention is flipped.
_DOOR_AT_MAX_Y = True


# ─────────────────────────────────────────────────────────────────────────────
#  FOOTER LOGO SETTINGS
# ─────────────────────────────────────────────────────────────────────────────
# Company logo shown bottom-centre of every PDF page / PPT slide, followed by
# "Logistics Engineering" (title-green) and "Company Use" (red, bottom-right).
# The file is looked up in a few likely locations; override by passing
# logo_path=... to add_download_buttons / generate_pdf.
_LOGO_FILENAME = "john_deere_footer.png"


import os


def _resolve_logo(logo_path: str | None = None) -> str | None:
    """Return a usable path to the footer logo, or None if not found."""
    candidates = []
    if logo_path:
        candidates.append(logo_path)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [
        os.path.join(os.getcwd(), "assets", _LOGO_FILENAME),
        os.path.join(here, "..", "assets", _LOGO_FILENAME),
        os.path.join(here, "assets", _LOGO_FILENAME),
        os.path.join("assets", _LOGO_FILENAME),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _draw_footer(fig, logo_path: str | None = None):
    """
    Bottom-centre footer drawn directly onto the figure so it appears
    identically in both the PDF page and the PPT slide:

        [ LOGO ]  Logistics Engineering            Company Use
        └──────────── centred ───────────┘        └ red, right ┘
    """
    import matplotlib.image as mpimg

    band_h = 0.050          # footer height as a fraction of the figure height
    y0     = 0.022          # distance of footer band from the bottom edge
    yc     = y0 + band_h / 2.0
    fig_w, fig_h = fig.get_size_inches()

    path = _resolve_logo(logo_path)
    text_x, ha = 0.5, "center"

    if path:
        try:
            img = mpimg.imread(path)
            ih, iw = img.shape[0], img.shape[1]
            aspect = (iw / ih) if ih else 3.0
            logo_w = band_h * aspect * (fig_h / fig_w)   # keep logo aspect ratio
            # logo just left of centre, text just right of centre -> reads centred
            logo_ax = fig.add_axes([0.5 - logo_w - 0.006, y0, logo_w, band_h])
            logo_ax.imshow(img)
            logo_ax.axis("off")
            text_x, ha = 0.5 + 0.006, "left"
        except Exception:
            text_x, ha = 0.5, "center"

    # "Logistics Engineering" — same green as the title, height ~ logo height
    fig.text(text_x, yc, "Logistics Engineering",
             color=_JD_GREEN, fontsize=16, fontweight="bold",
             va="center", ha=ha)

    # "Company Use" — red, bottom-right corner
    fig.text(0.992, yc, "Company Use",
             color=_COMPANY_RED, fontsize=13, fontweight="bold",
             va="center", ha="right")


def _add_face(ax, verts, rgb, alpha, zorder=0):
    poly = Poly3DCollection([verts], alpha=alpha)
    poly.set_facecolor(rgb)
    poly.set_edgecolor("none")
    poly.set_zorder(zorder)
    ax.add_collection3d(poly)


def _draw_container_3d(ax, CW, CL, CH, n_corr=30):
    """
    Open-corner container shell for the ISO view.

    Only TWO solid surfaces are drawn:
      • Left side wall (x = 0)
      • Back wall      (y = CL, the door end — shown closed/solid)

    The floor is NOT filled with a solid colour — only its outline is drawn.
    A filled floor face was showing through as a stray grey/tan triangle in
    any gap where racks don't fully cover the floor (e.g. partial last row),
    which read as a colour glitch. An outline-only floor still anchors the
    container's footprint visually without ever competing with rack colours.

    The right side wall (x = CW), the near/front wall (y = 0), and the
    roof are intentionally OMITTED so the loaded racks are fully visible
    from the camera angle used in render_container_page() (elev=20, azim=-55).
    """
    # Floor — OUTLINE ONLY, no fill (eliminates the stray colour patch)
    floor_edge = [
        [0,0,0],[CW,0,0],[CW,CL,0],[0,CL,0],[0,0,0]
    ]
    fxs = [p[0] for p in floor_edge]
    fys = [p[1] for p in floor_edge]
    fzs = [p[2] for p in floor_edge]
    ax.plot(fxs, fys, fzs, color=_FRAME_COL, lw=1.8, alpha=0.7, zorder=0)

    # Left side wall only (x = 0) — subtle corrugated finish, fewer strips
    sw = CL / n_corr
    for i in range(n_corr):
        y0, y1 = i*sw, (i+1)*sw
        shade = _CONT_MID if i % 2 == 0 else _CONT_LIGHT
        _add_face(ax, [[0,y0,0],[0,y1,0],[0,y1,CH],[0,y0,CH]], shade, 0.93)

    # Back wall (y = CL, door end) — solid/closed, corrugated finish
    for i in range(max(n_corr // 4, 1)):
        n = max(n_corr // 4, 1)
        x0 = i * (CW / n)
        x1 = x0 + (CW / n)
        shade = _CONT_MID if i % 2 == 0 else _CONT_LIGHT
        _add_face(ax, [[x0,CL,0],[x1,CL,0],[x1,CL,CH],[x0,CL,CH]], shade, 0.95)

    # Frame edges — only along the 3 drawn surfaces (no right-wall / front-wall posts)
    edges = [
        ([0,CW],[CL,CL],[0,0]),([0,CW],[CL,CL],[CH,CH]),  # back wall top/bottom
        ([0,0],[0,CL],[0,0]),([0,0],[0,CL],[CH,CH]),      # left wall top/bottom
        ([0,0],[0,0],[0,CH]),                        # left-front corner post
        ([0,0],[CL,CL],[0,CH]),                      # left-back corner post
        ([CW,CW],[CL,CL],[0,CH]),                    # right-back corner post
    ]
    for xs,ys,zs in edges:
        ax.plot(xs,ys,zs, color=_FRAME_COL, lw=3.0, alpha=1.0, solid_capstyle="round", zorder=0)


def _draw_racks_3d(ax, records, azim=-55, elev=20):
    """
    Draw ONE solid cuboid per individual rack unit for the ISO view.

    Only the three CAMERA-FACING faces (top, front, right) are drawn, fully
    OPAQUE. Draw order is controlled EXPLICITLY via each face's zorder
    (requires the axes to have `computed_zorder = False`, set in
    _setup_3d_ax) rather than relying on matplotlib's own automatic 3-D
    depth sort, which recomputes an approximate order from each polygon's
    own projected vertices at render time and can override a correct manual
    painter's-algorithm order.

    Depth is measured from each box's NEAREST corner to the camera (not its
    centre) -- more robust than a centroid for boxes of very different sizes.

    A single scalar zorder per box is still not enough for every case: two
    boxes in adjacent lanes (touching in x, i.e. disjoint footprints) can
    have PROJECTED images that overlap on screen at this oblique angle even
    though they never overlap in 3-D -- and if one lane runs deeper (in y)
    than its neighbour, there is no single "which box is nearer" answer that
    is correct across their whole overlap; part of the deeper box's side
    face is genuinely hidden behind the neighbour, part of it genuinely
    isn't. That mismatch is what showed up as one rack looking like it was
    "interfering with" / floating through another. Rather than guess a
    single order for the whole face, the RIGHT face is split at the exact
    depth where a same-height-or-taller neighbouring lane starts covering
    it, so only the genuinely-hidden slice is dropped and the genuinely-
    visible remainder still draws cleanly in place.
    """
    ar, er = math.radians(azim), math.radians(elev)
    # unit vector from the scene toward the camera
    vdx = math.cos(er) * math.cos(ar)
    vdy = math.cos(er) * math.sin(ar)
    vdz = math.sin(er)

    def _near_depth(rec):
        nx = rec["x"] + rec["iW"] if vdx > 0 else rec["x"]
        ny = rec["y"] + rec["iL"] if vdy > 0 else rec["y"]
        nz = rec["z"] + rec["h"]  if vdz > 0 else rec["z"]
        return nx * vdx + ny * vdy + nz * vdz

    ordered = sorted(records, key=_near_depth)        # farthest -> nearest
    BASE_Z = 10                                        # always above the shell (zorder 0)
    FEPS = 1e-6

    def _right_face_segments(rec):
        """
        y-subranges (within this box's own [y, y+iL]) where its right face
        is NOT covered by a taller-or-equal neighbouring lane touching its
        right edge. The covered slice would be hidden regardless -- the
        neighbour's own faces are what's actually visible there -- so it's
        simply skipped rather than drawn and fought over via zorder.
        """
        rx = rec["x"] + rec["iW"]
        ry0, ry1 = rec["y"], rec["y"] + rec["iL"]
        rz0, rz1 = rec["z"], rec["z"] + rec["h"]
        covered = []
        for other in records:
            if other is rec or abs(other["x"] - rx) > FEPS:
                continue
            oz0, oz1 = other["z"], other["z"] + other["h"]
            if oz0 <= rz0 + FEPS and oz1 >= rz1 - FEPS:
                oy0, oy1 = other["y"], other["y"] + other["iL"]
                lo, hi = max(ry0, oy0), min(ry1, oy1)
                if hi > lo + FEPS:
                    covered.append((lo, hi))
        if not covered:
            return [(ry0, ry1)]
        covered.sort()
        merged = [list(covered[0])]
        for lo, hi in covered[1:]:
            if lo <= merged[-1][1] + FEPS:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        segs, cur = [], ry0
        for lo, hi in merged:
            if lo > cur + FEPS:
                segs.append((cur, lo))
            cur = max(cur, hi)
        if cur < ry1 - FEPS:
            segs.append((cur, ry1))
        return segs

    for i, rec in enumerate(ordered):
        ox, oy, oz = rec["x"], rec["y"], rec["z"]
        rw, rl, rh = rec["iW"], rec["iL"], rec["h"]
        c = _hex_to_rgb01(rec["color"])
        top    = _lighten(c, 0.28)
        front  = _lighten(c, 0.04)
        rightf = _darken(c, 0.16)
        zo = BASE_Z + i

        faces = [
            # top
            ([[ox, oy, oz+rh], [ox+rw, oy, oz+rh],
              [ox+rw, oy+rl, oz+rh], [ox, oy+rl, oz+rh]], top),
            # front (-y face, toward the camera)
            ([[ox, oy, oz], [ox+rw, oy, oz],
              [ox+rw, oy, oz+rh], [ox, oy, oz+rh]], front),
        ]
        for verts, fc in faces:
            poly = Poly3DCollection([verts])
            poly.set_facecolor(fc)
            poly.set_edgecolor("#1A1A1A")
            poly.set_linewidth(0.6)
            poly.set_alpha(1.0)
            poly.set_zorder(zo)
            ax.add_collection3d(poly)

        # right face (+x, toward the camera) -- split around any shadowing
        # neighbour lane instead of drawn whole
        for y0, y1 in _right_face_segments(rec):
            verts = [[ox+rw, y0, oz], [ox+rw, y1, oz],
                     [ox+rw, y1, oz+rh], [ox+rw, y0, oz+rh]]
            poly = Poly3DCollection([verts])
            poly.set_facecolor(rightf)
            poly.set_edgecolor("#1A1A1A")
            poly.set_linewidth(0.6)
            poly.set_alpha(1.0)
            poly.set_zorder(zo)
            ax.add_collection3d(poly)


def _setup_3d_ax(fig, subplot, CW, CL, CH, elev, azim, dist=6.2):
    """
    Set up a 3-D axes with the TRUE container proportions enforced via
    set_box_aspect — without this, matplotlib auto-scales each axis to
    fill the plot box equally, which visually squashes the container's
    actual length:width:height ratio and makes a long container look
    almost cube-shaped. set_box_aspect((CW, CL, CH)) locks the rendered
    box to the real-world aspect ratio so the ISO view reads as a properly
    elongated container, matching the TOP and FRONT view proportions.
    """
    ax = fig.add_subplot(subplot, projection="3d", facecolor="white")
    ax.set_xlim(0, CW); ax.set_ylim(0, CL); ax.set_zlim(0, CH)
    ax.set_box_aspect((CW, CL, CH))
    ax.set_axis_off()
    ax.view_init(elev=elev, azim=azim)
    ax.dist = dist
    # Use our own explicit zorder for draw order (set on every face in
    # _draw_container_3d / _draw_racks_3d) instead of matplotlib's automatic
    # per-artist 3-D depth sort, which recomputes an approximate order from
    # each polygon's own projected vertices at render time and can silently
    # override a correct manual painter's-algorithm ordering.
    ax.computed_zorder = False
    return ax


# ══════════════════════════════════════════════════════════════════════════════
#  2-D ORTHOGRAPHIC VIEWS  (no dimension labels, no tick numbers)
# ══════════════════════════════════════════════════════════════════════════════

def _draw_2d_view(ax, records, container, view: str, title: str):
    """
    view values:
      'top'   : plan view, drawn HORIZONTAL — container LENGTH runs left-right,
                WIDTH runs top-bottom (matches reference manual-analysis layout).
      'front' : long elevation — container LENGTH (horizontal) × HEIGHT (vertical).
                This is the big strip view (looking at the container from its side,
                seeing the full run of racks) — matches the reference "FRONT VIEW".
      'side'  : short elevation — container WIDTH (horizontal) × HEIGHT (vertical).
                This is the small square-ish end view (looking straight at the
                door/rack ends) — matches the reference "SIDE VIEW".

    Top view shows one rectangle per floor footprint (stacking is invisible
    from directly above, which is correct). Front and side views show EVERY
    individual stacked unit as its own rectangle — an operator looking at
    the elevation needs to see each rack layer distinctly, not one merged
    block per column.
    """
    CL = float(container["L"]); CW = float(container["W"]); CH = float(container["H"])
    ax.set_facecolor("white")

    BORDER_LW = 0.9
    EDGE_COL  = "#2A2A2A"

    if view == "top":
        # x-axis = LENGTH, y-axis = WIDTH. Use 'auto' so the LENGTH axis always
        # fills the full panel width — the container length must read identically
        # in the TOP and FRONT views (same physical length from either angle).
        ax.set_aspect("auto")
        # Horizontal strip: x-axis = LENGTH, y-axis = WIDTH. Neutral background.
        ax.add_patch(mpatches.Rectangle((0,0), CL, CW,
                     facecolor=_EMPTY_COL, edgecolor=_FRAME_COL, lw=3.4))
        seen = {}
        for rec in records:
            key = (round(rec["x"]), round(rec["y"]))
            if key not in seen:
                seen[key] = rec
        for rec in seen.values():
            c = _hex_to_rgb01(rec["color"])
            ax.add_patch(mpatches.Rectangle(
                (rec["y"], rec["x"]), rec["iL"], rec["iW"],
                facecolor=_lighten(c,0.10), edgecolor=EDGE_COL,
                lw=BORDER_LW, alpha=0.95))
        ax.set_xlim(0, CL); ax.set_ylim(0, CW)

    elif view == "front":
        # Long elevation: x-axis = LENGTH, y-axis = HEIGHT. 'auto' so the LENGTH
        # axis fills the full panel width and matches the TOP view exactly.
        ax.set_aspect("auto")
        # Looking at the container from beside it (line of sight along the
        # WIDTH). Draw as a true back-to-front painter's projection: the racks
        # farthest across the width are drawn first and the nearest last, fully
        # opaque, so each spot on the elevation shows the colour of the single
        # front-most rack there — no bleed-through of a rack behind it.
        ax.add_patch(mpatches.Rectangle((0,0), CL, CH,
                     facecolor=_EMPTY_COL, edgecolor=_FRAME_COL, lw=3.4))
        for rec in sorted(records, key=lambda r: r["x"], reverse=True):
            c = _hex_to_rgb01(rec["color"])
            ax.add_patch(mpatches.Rectangle(
                (rec["y"], rec["z"]), rec["iL"], rec["h"],
                facecolor=_lighten(c,0.08), edgecolor=EDGE_COL,
                lw=BORDER_LW, alpha=1.0))
        ax.set_xlim(0, CL); ax.set_ylim(0, CH)

    elif view == "side":
        # Short elevation: x-axis = WIDTH, y-axis = HEIGHT. Kept proportional
        # (equal aspect) since it is the square-ish door/end view.
        ax.set_aspect("equal")
        # This is the view looking straight in through the container DOORS
        # (doors at the y = CL end), drawn as a true back-to-front (painter's)
        # projection: racks are drawn deepest-first, so a rack nearer the door
        # paints over whatever is directly behind it. Wherever a nearer rack
        # does NOT cover (a different width position, or a height the front
        # rack doesn't reach), the rack behind it stays visible. So a single
        # door-side layer that spans only part of the width shows the deeper
        # rack beside/above it (e.g. green on the left, orange on the right).
        ax.add_patch(mpatches.Rectangle((0,0), CW, CH,
                     facecolor=_EMPTY_COL, edgecolor=_FRAME_COL, lw=3.4))

        # Deepest (farthest from the door) first; nearest drawn last on top.
        order = sorted(records, key=lambda r: r["y"],
                       reverse=not _DOOR_AT_MAX_Y)
        for rec in order:
            c = _hex_to_rgb01(rec["color"])
            ax.add_patch(mpatches.Rectangle(
                (rec["x"], rec["z"]), rec["iW"], rec["h"],
                facecolor=_lighten(c,0.08), edgecolor=EDGE_COL,
                lw=BORDER_LW, alpha=1.0))
        ax.set_xlim(0, CW); ax.set_ylim(0, CH)

    # No tick labels, no axis labels, no dimension numbers
    ax.set_xticks([]); ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title(title, fontsize=11, fontweight="bold", pad=6,
                color="#1A1A1A", loc="center")


# ══════════════════════════════════════════════════════════════════════════════
#  LEGEND
# ══════════════════════════════════════════════════════════════════════════════

def _draw_legend(ax, rack_color_map: dict):
    """Horizontal colour-swatch legend: colour box + rack name."""
    ax.axis("off")
    names = list(rack_color_map.keys())
    n = len(names)
    if n == 0:
        return
    cols = min(n, 6)
    rows = -(-n // cols)
    for i, name in enumerate(names):
        col = i % cols
        row = i // cols
        x = col / cols
        y = 1.0 - (row + 0.5) / rows
        ax.add_patch(mpatches.Rectangle(
            (x + 0.01, y - 0.16/rows), 0.022, 0.30/rows,
            transform=ax.transAxes, facecolor=rack_color_map[name],
            edgecolor="black", linewidth=0.5, clip_on=False))
        ax.text(x + 0.045, y, name, transform=ax.transAxes,
                fontsize=7.5, va="center", ha="left", color="#212121")


# ══════════════════════════════════════════════════════════════════════════════
#  OPERATOR LOADING FLOOR PLAN  (labelled top view + loading order)
# ══════════════════════════════════════════════════════════════════════════════

def _pos_rack_label(p):
    """Short rack description for a floor position (handles mixed stacks)."""
    if p["homogeneous"]:
        return p["rack"]
    parts = [(nm if c == 1 else f"{nm} x{c}") for nm, c in p["counts"].items()]
    return " + ".join(parts)


def _draw_top_loading_plan(ax, positions, container):
    """
    Operator loading floor plan (plan view, looking straight down).
      x-axis = container LENGTH, y-axis = container WIDTH.
    Every floor position gets a loading-order badge, and — where the cell is
    big enough — the rack ID, footprint orientation and stack height. Small
    cells show just the badge; the loading table carries the full detail.
    NOSE (load first) / DOORS (load last) and the loading direction are marked.
    """
    CL = float(container["L"]); CW = float(container["W"])
    ax.set_facecolor("white")
    ax.set_aspect("auto")
    ax.add_patch(mpatches.Rectangle((0, 0), CL, CW,
                 facecolor="#F5F7F5", edgecolor=_FRAME_COL, lw=3.4))
    ax.set_xlim(0, CL); ax.set_ylim(0, CW * 1.32)
    ax.set_xticks([]); ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)

    # data-units -> points, so text can be fitted to each cell
    fig = ax.figure
    fw_in, fh_in = fig.get_size_inches()
    bb = ax.get_position()
    ppx = (bb.width * fw_in * 72.0) / CL
    ppy = (bb.height * fh_in * 72.0) / (CW * 1.32)

    def _fit(lines, cw_pt, ch_pt, minfs=4.8, maxfs=9.0):
        chosen = []
        for ln in lines:
            trial = chosen + [ln]
            fs_h = ch_pt / (len(trial) * 1.35 + 0.4)
            fs_w = min(cw_pt / (max(1, len(t)) * 0.60) for t in trial)
            if min(fs_h, fs_w) >= minfs:
                chosen = trial
            else:
                break
        if not chosen:
            return [], 0.0
        fs_h = ch_pt / (len(chosen) * 1.35 + 0.4)
        fs_w = min(cw_pt / (max(1, len(t)) * 0.60) for t in chosen)
        return chosen, max(minfs, min(maxfs, min(fs_h, fs_w)))

    for p in positions:
        x0, y0 = p["y"], p["x"]
        w, h   = p["iL"], p["iW"]
        c = _hex_to_rgb01(p["color"])
        ax.add_patch(mpatches.Rectangle((x0, y0), w, h,
                     facecolor=_lighten(c, 0.12), edgecolor="#222222",
                     lw=1.1, alpha=0.97))
        cw_pt, ch_pt = w * ppx, h * ppy

        # Length / width marked as plain numbers at the box's own edges
        # (length along the top, width rotated along the left) instead of a
        # combined "L x W" in the centre -- so an operator can tell at a
        # glance which dimension runs along the container's length vs its
        # width. No dimension lines/ticks -- just the number. Skipped when
        # the box is too small to spare the room (the loading table always
        # has the exact figures).
        dim_fs   = max(4.2, min(7.0, 0.26 * min(cw_pt, ch_pt)))
        show_len = cw_pt >= 26 and ch_pt >= 34
        show_wid = ch_pt >= 26 and cw_pt >= 34

        # Reserve the edge strips BEFORE sizing the main label, and shrink
        # the region the label/badge are allowed to use accordingly -- this
        # is what actually keeps every piece of text inside its own box
        # instead of drifting into the strip (or a neighbouring cell) at
        # small sizes.
        top_strip_pt  = dim_fs * 1.6 if show_len else 0.0
        left_strip_pt = dim_fs * 1.6 if show_wid else 0.0
        top_strip  = min(h * 0.35, top_strip_pt / ppy) if ppy else 0.0
        left_strip = min(w * 0.35, left_strip_pt / ppx) if ppx else 0.0

        ix0, iy0 = x0 + left_strip, y0
        iw, ih   = w - left_strip, h - top_strip
        icw_pt, ich_pt = iw * ppx, ih * ppy

        lines = [p["rack"] if p["homogeneous"] else "MIX"]
        if p["stack_n"] > 1:
            lines.append(f'x{p["stack_n"]} high')
        shown, fs = _fit(lines, icw_pt, ich_pt)

        badge_fs = max(5.5, min(11.0, 0.38 * min(icw_pt, ich_pt)))

        if show_len:
            ax.text(x0 + w * 0.5, y0 + h - top_strip * 0.5, f'{int(round(w))}',
                    fontsize=dim_fs, fontweight="bold", color="#111111",
                    ha="center", va="center", zorder=4)
        if show_wid:
            ax.text(x0 + left_strip * 0.5, y0 + h * 0.5, f'{int(round(h))}',
                    fontsize=dim_fs, fontweight="bold", color="#111111",
                    ha="center", va="center", rotation=90, zorder=4)

        if shown:
            ax.text(ix0 + iw * 0.5, iy0 + ih * 0.40, "\n".join(shown),
                    fontsize=fs, fontweight="bold", color="#111111",
                    ha="center", va="center", linespacing=1.2, zorder=4)
            ax.text(ix0 + iw * 0.86, iy0 + ih * 0.15, str(p["seq"]),
                    fontsize=badge_fs, fontweight="bold", color="white",
                    ha="center", va="center", zorder=5,
                    bbox=dict(boxstyle="circle,pad=0.22", fc="#1A1A1A", ec="white", lw=0.5))
        else:
            ax.text(x0 + w * 0.5, y0 + h * 0.5, str(p["seq"]),
                    fontsize=badge_fs, fontweight="bold", color="white",
                    ha="center", va="center", zorder=5,
                    bbox=dict(boxstyle="circle,pad=0.22", fc="#1A1A1A", ec="white", lw=0.5))

    # end markers + loading direction (above the container)
    ax.text(CL * 0.02, CW * 1.15, "NOSE\n(load 1st)", fontsize=8.5,
            fontweight="bold", color="#245A1C", ha="left", va="bottom")
    ax.text(CL * 0.98, CW * 1.15, "DOORS\n(load last)", fontsize=8.5,
            fontweight="bold", color="#C0392B", ha="right", va="bottom")
    ax.annotate("", xy=(CL * 0.66, CW * 1.09), xytext=(CL * 0.34, CW * 1.09),
                arrowprops=dict(arrowstyle="-|>", lw=2.0, color="#555555"))
    ax.text(CL * 0.5, CW * 1.14, "loading order", fontsize=7.5, style="italic",
            color="#555555", ha="center", va="bottom")


def _auto_col_widths(col_labels, rows_data, pad=1.6, min_frac=0.055):
    """
    Column widths proportional to the longest string actually in each column
    (header included), instead of fixed fractions that waste space on short
    columns (e.g. 'Qty') while cramping long ones (e.g. 'Rack ID' with mixed
    labels). `pad` is extra character-widths of breathing room per column;
    `min_frac` is a floor so no column collapses to nothing.
    """
    ncols = len(col_labels)
    maxlen = [len(str(col_labels[j])) for j in range(ncols)]
    for row in rows_data:
        for j, val in enumerate(row):
            maxlen[j] = max(maxlen[j], len(str(val)))
    weights = [maxlen[j] + pad for j in range(ncols)]
    total = sum(weights)
    fracs = [w / total for w in weights]
    # enforce the floor, then renormalise so everything still sums to 1.0
    deficit = sum(max(0.0, min_frac - f) for f in fracs)
    if deficit > 0:
        scale = 1.0 - deficit
        fracs = [max(min_frac, f) if f >= min_frac else min_frac for f in fracs]
        surplus_cols = [j for j, f in enumerate(fracs) if f > min_frac]
        over = sum(fracs) - 1.0
        if surplus_cols and over > 0:
            per = over / len(surplus_cols)
            for j in surplus_cols:
                fracs[j] -= per
    return fracs


# ══════════════════════════════════════════════════════════════════════════════
#  FULL PAGE RENDERER  (matches reference PPT layout exactly)
# ══════════════════════════════════════════════════════════════════════════════

def render_container_page(
    load: dict, dims: dict, container: dict, rack_color_map: dict,
    container_number: int, total_containers: int,
    container_label: str = "40 HC",
    figsize=(15.2, 9.0), dpi=150,
    logo_path: str | None = None,
) -> io.BytesIO:
    """
    Render one container's full layout page as PNG.

    Layout (matches reference):
        Title  "Density Analysis - {container_label}"
        Row 1 : [ Summary table ]                 [        ISO VIEW        ]
        Row 2 : [        TOP VIEW       ]         [   SIDE VIEW  ]
        Row 3 : [        FRONT VIEW     ]         [     Legend    ]
        Footer: "John Deere | Logistics Engineering |"   "Company Use"
    """
    CW = float(container["W"]); CL = float(container["L"]); CH = float(container["H"])

    records, counts, positions = _build_layout_and_counts(load, dims, container, rack_color_map)

    cont_wt = sum(load.get(r,0)*dims[r]["Weight (Kg)"] for r in load)

    fig = plt.figure(figsize=figsize, facecolor="white")

    gs = GridSpec(
        5, 2,
        height_ratios=[0.06, 0.33, 0.29, 0.20, 0.12],
        width_ratios=[0.52, 0.48],
        figure=fig,
        hspace=0.42, wspace=0.08,
        left=0.02, right=0.98, top=0.95, bottom=0.135,
    )

    # ── Title ─────────────────────────────────────────────────────────────────
    suffix = f"  (Container {container_number}/{total_containers})" if total_containers > 1 else ""
    ax_title = fig.add_subplot(gs[0, :])
    ax_title.axis("off")
    ax_title.text(0.0, 0.5, f"Loading Plan - {container_label}{suffix}",
                  fontsize=20, fontweight="bold", color=_JD_GREEN,
                  va="center", ha="left", transform=ax_title.transAxes)

    # ── Loading sequence table (top-left) — what the operator actually does ───
    ax_tbl = fig.add_subplot(gs[1, 0])
    ax_tbl.axis("off")

    col_labels = ["Seq", "Rack ID", "Qty", "Orient (mm)", "Stack", "Wt (kg)"]
    rows_data = []
    for p in sorted(positions, key=lambda z: z["seq"]):
        rows_data.append([
            str(p["seq"]),
            _pos_rack_label(p),
            str(p["stack_n"]),
            f'{int(round(p["iL"]))} x {int(round(p["iW"]))}',
            (f'x{p["stack_n"]}' if p["stack_n"] > 1 else "1"),
            f'{p["weight"]:,.0f}',
        ])
    rows_data.append(["", "TOTAL", str(sum(p["stack_n"] for p in positions)),
                      "", "", f"{cont_wt:,.0f}"])
    col_widths = _auto_col_widths(col_labels, rows_data)

    n_total = len(rows_data) + 1
    row_h   = 0.115
    tbl_h   = min(1.0, n_total * row_h)
    tbl_bottom = (1.0 - tbl_h) / 2.0   # centre vertically -- spare room split
                                       # above AND below instead of dumped
                                       # entirely under the table
    tbl = ax_tbl.table(cellText=rows_data, colLabels=col_labels,
                       colWidths=col_widths, cellLoc="center", loc="upper center",
                       bbox=[0.0, tbl_bottom, 1.0, tbl_h])
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9.5 if n_total <= 10 else max(5.5, 9.5 * 10.0 / n_total))
    # left-align the Rack ID column so long mixed labels read cleanly
    for i in range(len(rows_data) + 1):
        cell = tbl[i, 1]
        cell.set_text_props(ha="left")
        cell.PAD = 0.03
    for j in range(len(col_labels)):
        tbl[0, j].set_facecolor("#367C2B")
        tbl[0, j].set_text_props(fontweight="bold", color="white",
                                 ha=("left" if j == 1 else "center"))
    last_row = len(rows_data)
    for j in range(len(col_labels)):
        tbl[last_row, j].set_text_props(fontweight="bold")
        tbl[last_row, j].set_facecolor("#EAF2E7")

    # ── ISO view (top-right) ─────────────────────────────────────────────────
    # Mirror the layout along the container LENGTH for the ISO only, so the
    # DOOR end (the side loaded last, where any partial/adjustment layer sits)
    # faces the viewer as the entrance — matching how a forklift operator
    # leaves space at the doors, not the sealed rear. Other views unchanged.
    iso_records = []
    for rec in records:
        m = dict(rec)
        m["y"] = CL - (rec["y"] + rec["iL"])
        iso_records.append(m)

    ax_iso = _setup_3d_ax(fig, gs[1:3, 1], CW, CL, CH, elev=20, azim=-55, dist=5.4)
    _draw_container_3d(ax_iso, CW, CL, CH)
    _draw_racks_3d(ax_iso, iso_records)
    ax_iso.text2D(0.5, -0.06, "ISO VIEW", transform=ax_iso.transAxes,
                  fontsize=12, fontweight="bold", ha="center", color="#1A1A1A")

    # ── Loading floor plan (mid-left, wide) — the operator's main view ───────
    ax_top = fig.add_subplot(gs[2, 0])
    _draw_top_loading_plan(ax_top, positions, container)
    ax_top.text(0.5, -0.14, "LOADING FLOOR PLAN  (top view)",
               transform=ax_top.transAxes,
               fontsize=12, fontweight="bold", ha="center", color="#1A1A1A")

    # ── Side view (row 3, right) ─────────────────────────────────────────────
    ax_side = fig.add_subplot(gs[3, 1])
    _draw_2d_view(ax_side, records, container, "side", "")
    ax_side.text(0.5, -0.10, "SIDE VIEW", transform=ax_side.transAxes,
                fontsize=12, fontweight="bold", ha="center", color="#1A1A1A")

    # ── Front view (bottom-left, wide) ───────────────────────────────────────
    ax_front = fig.add_subplot(gs[3, 0])
    _draw_2d_view(ax_front, records, container, "front", "")
    ax_front.text(0.5, -0.15, "FRONT VIEW", transform=ax_front.transAxes,
                 fontsize=12, fontweight="bold", ha="center", color="#1A1A1A")

    # ── Legend (bottom strip, full width) — ONLY racks in THIS container ─────
    ax_legend = fig.add_subplot(gs[4, :])
    local_color_map = {
        r: rack_color_map[r]
        for r in sorted(load)
        if load.get(r, 0) > 0 and r in rack_color_map
    }
    _draw_legend(ax_legend, local_color_map)

    # ── Footer: logo (centre) + Logistics Engineering + Company Use ──────────
    _draw_footer(fig, logo_path)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=dpi, facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf


# ══════════════════════════════════════════════════════════════════════════════
#  PDF GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_pdf(
    containers: list, dims: dict, container_spec: dict,
    container_label: str = "40 HC",
    output_path: str | None = None,
    logo_path: str | None = None,
) -> bytes:
    """
    Generate landscape A4 PDF, one page per container.
    Title on each page: "Density Analysis - {container_label}"
    Footer on each page: company logo + "Logistics Engineering" (centre)
    and "Company Use" (red, right).
    """
    all_racks = sorted({r for c in containers for r in c})
    rack_color_map = {r: _rack_color(i) for i, r in enumerate(all_racks)}

    from reportlab.pdfgen import canvas as _rl_canvas
    from reportlab.lib.utils import ImageReader

    PAGE = landscape(A4)
    PW, PH = PAGE
    margin = 4 * mm
    img_w = PW - 2 * margin
    img_h = PH - 2 * margin
    # Render at the page's printable aspect ratio so the image fills the whole
    # page and the footer lands at the very bottom (no blank strip below).
    fig_w = 15.2
    fig_h = fig_w * (img_h / img_w)

    buf = io.BytesIO()
    c = _rl_canvas.Canvas(buf, pagesize=PAGE)

    for ci, load in enumerate(containers, 1):
        png_buf = render_container_page(
            load, dims, container_spec, rack_color_map,
            container_number=ci,
            total_containers=len(containers),
            container_label=container_label,
            figsize=(fig_w, fig_h), dpi=150,
            logo_path=logo_path,
        )
        c.drawImage(ImageReader(png_buf), margin, margin,
                    width=img_w, height=img_h,
                    preserveAspectRatio=False, mask=None)
        c.showPage()

    c.save()
    pdf_bytes = buf.getvalue()
    if output_path:
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)
    return pdf_bytes


# ══════════════════════════════════════════════════════════════════════════════
#  CONTAINER LABEL HELPER
#  Maps your app's container-type dropdown value to the PDF/PPT title suffix.
# ══════════════════════════════════════════════════════════════════════════════

def container_label_from_type(container_type: str) -> str:
    """
    Normalises common container-type strings to the title format used in
    'Density Analysis - {label}', e.g. '40HC' -> '40 HC', '20gp' -> '20 GP'.
    """
    s = container_type.strip().upper().replace("'", "").replace("FT", "")
    s = s.replace("-", " ")
    parts = s.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    # Fallback: try to split digits from letters, e.g. "40HC"
    import re
    m = re.match(r"(\d+)\s*([A-Z]+)", s)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return container_type


# ══════════════════════════════════════════════════════════════════════════════
#  STREAMLIT HELPER
# ══════════════════════════════════════════════════════════════════════════════

def add_download_buttons(st, containers, dims, container_spec,
                         container_type: str = "40 HC",
                         pdf_only: bool = True,
                         logo_path: str | None = None,
                         pdf_bytes: bytes | None = None):
    """
    Add the PDF download button to a Streamlit app.

    `container_type` can be your raw dropdown value (e.g. "40HC", "20 GP");
    it is normalised automatically to e.g. "40 HC" for the title.

    pdf_bytes : if the PDF was already generated (recommended — generate it
                once when the user clicks Calculate and cache it in
                st.session_state), pass it here so the button serves it
                instantly and does NOT rebuild the PDF on every rerun. If left
                None, the PDF is built here and a "generating" message is shown.

    Only the PDF is offered (the PPTX export was removed). `pdf_only` is kept
    for backward compatibility but is now always effectively True.
    """
    label = container_label_from_type(container_type)

    if pdf_bytes is None:
        try:
            with st.spinner("Generating PDF report… this can take a few seconds."):
                pdf_bytes = generate_pdf(containers, dims, container_spec,
                                         container_label=label, logo_path=logo_path)
        except Exception as e:
            st.error(f"PDF error: {e}")
            return

    st.download_button(
        "📄 Download PDF", pdf_bytes,
        f"density_analysis_{label.replace(' ', '_')}.pdf",
        "application/pdf", use_container_width=True)
