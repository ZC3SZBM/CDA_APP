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
#  LAYOUT BUILDER
#  Reproduces strip-pass placement geometry AND computes the
#  Length-wise / Width-wise / Height-wise counts for the summary table.
# ══════════════════════════════════════════════════════════════════════════════

def _build_layout_and_counts(load: dict, dims: dict, container: dict,
                             rack_color_map: dict):
    """
    Returns
    -------
    records : list of PER-UNIT cuboid records — one record for every
               individual rack unit (not merged into stack blocks). This is
               what a forklift operator needs: each rack they physically
               place corresponds to exactly one drawn cuboid with its own
               visible edges, at its own (x, y, z) position.
               Each record: {name, color, x, y, z, iW, iL, h}
    counts  : { rack_name: {"length_wise": int, "width_wise": int,
                            "height_wise": int, "net_qty": int} }
    """
    CW = float(container["W"])
    CL = float(container["L"])
    CH = float(container["H"])

    ordered = sorted(
        [(r, q) for r, q in load.items() if q > 0],
        key=lambda x: dims[x[0]]["Length (MM)"],
        reverse=True,
    )

    records = []
    counts  = {}

    # Shared "shelf" cursor. Fill the container WIDTH of a row first (racks
    # side by side across the width), THEN advance along the LENGTH. This
    # mirrors how the packer fills a container, so the drawing matches reality:
    # racks pack 2-, 3-across as they fit and the layout never runs past the
    # container walls (previously every rack took its own length-row, so many
    # single-qty racks lined up end-to-end and overflowed the box).
    cur_y = 0.0   # length position where the current row starts
    cur_x = 0.0   # width position of the next column in the current row
    row_L = 0.0   # depth (length) of the current row
    EPS   = 1e-6

    for r, total_qty in ordered:
        r_l = float(dims[r]["Length (MM)"])
        r_w = float(dims[r]["Width (MM)"])
        r_h = float(dims[r]["Height (MM)"])
        color = rack_color_map[r]
        stack = max(1, int(CH // r_h))

        best = None
        for iW, iL in ((r_w, r_l), (r_l, r_w)):
            across = int(CW // iW)
            if across == 0:
                continue
            score = (across * stack) / iL
            if best is None or score > best[3]:
                best = (iW, iL, across, score)

        if best is None:
            counts[r] = {"length_wise": 0, "width_wise": 0,
                        "height_wise": 0, "net_qty": 0}
            continue

        item_W, item_L, fit_across, _ = best
        per_row = max(1, fit_across * stack)

        remaining = total_qty
        while remaining > 0:
            # Start a new row if another column would exceed the container width.
            if cur_x + item_W > CW + EPS:
                cur_y += row_L
                cur_x  = 0.0
                row_L  = 0.0
            # One column at (cur_x, cur_y): a vertical stack of up to `stack` units.
            for layer in range(stack):
                if remaining <= 0:
                    break
                records.append({
                    "name": r, "color": color,
                    "x": cur_x, "y": cur_y, "z": layer * r_h,
                    "iW": item_W, "iL": item_L, "h": r_h,
                })
                remaining -= 1
            row_L = max(row_L, item_L)
            cur_x += item_W

        counts[r] = {
            "length_wise": -(-total_qty // per_row),   # ceil(qty / per_row)
            "width_wise":  fit_across,
            "height_wise": stack,
            "net_qty":     total_qty,
        }

    return records, counts


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


def _add_face(ax, verts, rgb, alpha):
    poly = Poly3DCollection([verts], alpha=alpha, zsort="average")
    poly.set_facecolor(rgb)
    poly.set_edgecolor("none")
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
    ax.plot(fxs, fys, fzs, color=_FRAME_COL, lw=1.2, alpha=0.55)

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
        ax.plot(xs,ys,zs, color=_FRAME_COL, lw=2.0, alpha=1.0, solid_capstyle="round")


def _draw_racks_3d(ax, records):
    """
    Draw ONE solid cuboid per individual rack unit, each with its own thin
    dark edge outline. This is intentional: an operator loading the
    container needs to see every physical rack as a distinct box, not an
    abstracted stack — that's what tells them exactly how many racks go in
    each position and how they're oriented.

    Edge lines are kept thin (0.35pt) and a consistent dark neutral colour
    so adjacent units read as clearly separate without creating visual
    clutter at a glance.
    """
    for rec in records:
        ox, oy, oz = rec["x"], rec["y"], rec["z"]
        rw, rl, rh = rec["iW"], rec["iL"], rec["h"]
        c = _hex_to_rgb01(rec["color"])
        top, front = _lighten(c,0.30), _lighten(c,0.12)
        side, back, bot = _darken(c,0.08), _darken(c,0.22), _darken(c,0.35)
        face_defs = [
            ([[ox,oy,oz],[ox+rw,oy,oz],[ox+rw,oy+rl,oz],[ox,oy+rl,oz]], bot, 0.92),
            ([[ox,oy,oz+rh],[ox+rw,oy,oz+rh],[ox+rw,oy+rl,oz+rh],[ox,oy+rl,oz+rh]], top, 0.96),
            ([[ox,oy,oz],[ox+rw,oy,oz],[ox+rw,oy,oz+rh],[ox,oy,oz+rh]], front, 0.94),
            ([[ox,oy+rl,oz],[ox+rw,oy+rl,oz],[ox+rw,oy+rl,oz+rh],[ox,oy+rl,oz+rh]], back, 0.90),
            ([[ox,oy,oz],[ox,oy+rl,oz],[ox,oy+rl,oz+rh],[ox,oy,oz+rh]], side, 0.92),
            ([[ox+rw,oy,oz],[ox+rw,oy+rl,oz],[ox+rw,oy+rl,oz+rh],[ox+rw,oy,oz+rh]], _darken(side,0.04), 0.92),
        ]
        for verts, fc, alpha in face_defs:
            poly = Poly3DCollection([verts], alpha=alpha, zsort="average")
            poly.set_facecolor(fc)
            poly.set_edgecolor("#2A2A2A")
            poly.set_linewidth(0.35)
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

    BORDER_LW = 0.7
    EDGE_COL  = "#2A2A2A"

    if view == "top":
        # x-axis = LENGTH, y-axis = WIDTH. Use 'auto' so the LENGTH axis always
        # fills the full panel width — the container length must read identically
        # in the TOP and FRONT views (same physical length from either angle).
        ax.set_aspect("auto")
        # Horizontal strip: x-axis = LENGTH, y-axis = WIDTH. Neutral background.
        ax.add_patch(mpatches.Rectangle((0,0), CL, CW,
                     facecolor=_EMPTY_COL, edgecolor=_FRAME_COL, lw=2.0))
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
        # This view represents looking at the container from beside it
        # (like watching a truck drive past) — the line of sight runs along
        # the container WIDTH. Only the FRONT-MOST rack (smallest width
        # position = closest to the viewer) is visible at each (length,
        # height) position; racks behind it along the width are hidden.
        # We keep, for every (y,z) slot, the record with the smallest x.
        ax.add_patch(mpatches.Rectangle((0,0), CL, CH,
                     facecolor=_EMPTY_COL, edgecolor=_FRAME_COL, lw=2.0))
        seen = {}
        for rec in records:
            key = (round(rec["y"]), round(rec["z"]))
            if key not in seen or rec["x"] < seen[key]["x"]:
                seen[key] = rec
        for rec in seen.values():
            c = _hex_to_rgb01(rec["color"])
            ax.add_patch(mpatches.Rectangle(
                (rec["y"], rec["z"]), rec["iL"], rec["h"],
                facecolor=_lighten(c,0.08), edgecolor=EDGE_COL,
                lw=BORDER_LW, alpha=0.97))
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
                     facecolor=_EMPTY_COL, edgecolor=_FRAME_COL, lw=2.0))

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

    records, counts = _build_layout_and_counts(load, dims, container, rack_color_map)

    cont_wt = sum(load.get(r,0)*dims[r]["Weight (Kg)"] for r in load)

    fig = plt.figure(figsize=figsize, facecolor="white")

    gs = GridSpec(
        4, 2,
        height_ratios=[0.07, 0.30, 0.34, 0.24],
        width_ratios=[0.56, 0.44],
        figure=fig,
        hspace=0.42, wspace=0.08,
        left=0.02, right=0.98, top=0.95, bottom=0.135,
    )

    # ── Title ─────────────────────────────────────────────────────────────────
    suffix = f"  (Container {container_number}/{total_containers})" if total_containers > 1 else ""
    ax_title = fig.add_subplot(gs[0, :])
    ax_title.axis("off")
    ax_title.text(0.0, 0.5, f"Density Analysis - {container_label}{suffix}",
                  fontsize=20, fontweight="bold", color=_JD_GREEN,
                  va="center", ha="left", transform=ax_title.transAxes)

    # ── Summary table (top-left) ─────────────────────────────────────────────
    ax_tbl = fig.add_subplot(gs[1, 0])
    ax_tbl.axis("off")

    col_labels = ["Rack ID", "Length\nWise", "Width\nWise", "Height\nWise",
                  "Net\nQuantity", "Package\nWeight", "Net Container\nWeight (KG)"]
    rows_data = []
    for r in sorted(load.keys()):
        c = counts.get(r, {"length_wise":0,"width_wise":0,"height_wise":0,"net_qty":0})
        unit_wt = dims[r]["Weight (Kg)"]
        rows_data.append([
            r, str(c["length_wise"]), str(c["width_wise"]), str(c["height_wise"]),
            str(c["net_qty"]), f"{unit_wt:.0f}", f"{c['net_qty']*unit_wt:,.0f}",
        ])
    total_qty = sum(counts.get(r,{}).get("net_qty",0) for r in load)
    rows_data.append(["TOTAL", "", "", "", str(total_qty), "", f"{cont_wt:,.0f}"])

    # Size the table to fit its panel no matter how many racks are in the
    # container (a bbox in axes-fraction coords overrides per-row auto-height,
    # so 14+ rows no longer overflow into the views below). Font shrinks a
    # little as the row count grows.
    n_total = len(rows_data) + 1                      # data rows + header
    row_h   = 0.095
    tbl_h   = min(1.0, n_total * row_h)
    tbl = ax_tbl.table(cellText=rows_data, colLabels=col_labels,
                       cellLoc="center", loc="upper center",
                       bbox=[0.0, 1.0 - tbl_h, 1.0, tbl_h])
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8.5 if n_total <= 11 else max(5.5, 8.5 * 11.0 / n_total))
    for j in range(len(col_labels)):
        tbl[0, j].set_facecolor("#D9D9D9")
        tbl[0, j].set_text_props(fontweight="bold", color="#1A1A1A")
    last_row = len(rows_data)
    for j in range(len(col_labels)):
        tbl[last_row, j].set_text_props(fontweight="bold")
        tbl[last_row, j].set_facecolor("#F0F0F0")

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

    ax_iso = _setup_3d_ax(fig, gs[1, 1], CW, CL, CH, elev=20, azim=-55, dist=6.0)
    _draw_container_3d(ax_iso, CW, CL, CH)
    _draw_racks_3d(ax_iso, iso_records)
    ax_iso.text2D(0.5, -0.10, "ISO VIEW", transform=ax_iso.transAxes,
                  fontsize=11, fontweight="bold", ha="center", color="#1A1A1A")

    # ── Top view (mid-left, wide) ────────────────────────────────────────────
    ax_top = fig.add_subplot(gs[2, 0])
    _draw_2d_view(ax_top, records, container, "top", "")
    ax_top.text(0.5, -0.18, "TOP VIEW", transform=ax_top.transAxes,
               fontsize=12, fontweight="bold", ha="center", color="#1A1A1A")

    # ── Side view (mid-right) ────────────────────────────────────────────────
    ax_side = fig.add_subplot(gs[2, 1])
    _draw_2d_view(ax_side, records, container, "side", "")
    ax_side.text(0.5, -0.10, "SIDE VIEW", transform=ax_side.transAxes,
                fontsize=12, fontweight="bold", ha="center", color="#1A1A1A")

    # ── Front view (bottom-left, wide) ───────────────────────────────────────
    ax_front = fig.add_subplot(gs[3, 0])
    _draw_2d_view(ax_front, records, container, "front", "")
    ax_front.text(0.5, -0.15, "FRONT VIEW", transform=ax_front.transAxes,
                 fontsize=12, fontweight="bold", ha="center", color="#1A1A1A")

    # ── Legend (bottom-right) — ONLY the racks loaded in THIS container ──────
    ax_legend = fig.add_subplot(gs[3, 1])
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
                         logo_path: str | None = None):
    """
    Add the PDF download button to a Streamlit app.

    `container_type` can be your raw dropdown value (e.g. "40HC", "20 GP");
    it is normalised automatically to e.g. "40 HC" for the title, which is
    rendered inside the report as "Density Analysis - {label}".

    No shipment/route info (origin, destination) is shown anywhere in the
    report — only container + rack loading data.

    Only the PDF is offered (the PPTX export was removed since the slides
    aren't editable). `pdf_only` is kept for backward compatibility but is
    now always effectively True.

    Parameters
    ----------
    logo_path : optional explicit path to the footer logo. If omitted, the
                report looks for assets/john_deere_footer.png automatically.

    Usage:
        from engine.container_report import add_download_buttons
        add_download_buttons(st, containers, dims, container_spec,
                             container_type=container_type)
    """
    label = container_label_from_type(container_type)

    try:
        pdf = generate_pdf(containers, dims, container_spec,
                           container_label=label, logo_path=logo_path)
        st.download_button(
            "📄 Download PDF", pdf,
            f"density_analysis_{label.replace(' ','_')}.pdf",
            "application/pdf", use_container_width=True)
    except Exception as e:
        st.error(f"PDF error: {e}")
