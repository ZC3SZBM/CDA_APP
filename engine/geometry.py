# engine/geometry.py
# ─────────────────────────────────────────────────────────────────────────────
# Rect        : an axis-aligned free rectangle on the container FLOOR
# MaxRectsBin : 2-D guillotine bin-packer, Best-Short-Side-Fits heuristic
#
# AXIS CONVENTION (strictly enforced throughout)
# ───────────────────────────────────────────────
#   Rect(x, y, rw, rl)
#     x  : position along container WIDTH  axis  (0 … container_W)
#     y  : position along container LENGTH axis  (0 … container_L)
#     rw : extent in WIDTH  direction  (≤ container_W - x)
#     rl : extent in LENGTH direction  (≤ container_L - y)
#
#   place(item_W, item_L)
#     item_W : item dimension that goes along the WIDTH  axis
#     item_L : item dimension that goes along the LENGTH axis
#     Both orientations are tried (item_W↔item_L swap = 90° rotation).
#
# All internal arithmetic uses these names consistently so no axis is
# ever swapped accidentally.
# ─────────────────────────────────────────────────────────────────────────────


class Rect:
    """
    A free rectangle on the container floor.

    Attributes
    ----------
    x, y : position (WIDTH axis, LENGTH axis)
    rw   : extent in WIDTH  direction
    rl   : extent in LENGTH direction
    """

    __slots__ = ("x", "y", "rw", "rl")

    def __init__(self, x: float, y: float, rw: float, rl: float):
        self.x  = x
        self.y  = y
        self.rw = rw   # width  extent
        self.rl = rl   # length extent

    # ── Queries ───────────────────────────────────────────────────────────────

    def fits(self, item_W: float, item_L: float) -> bool:
        """True if an item of (item_W × item_L) fits inside this free rect."""
        return item_W <= self.rw and item_L <= self.rl

    def area(self) -> float:
        return self.rw * self.rl

    def __repr__(self):
        return (f"Rect(x={self.x}, y={self.y}, "
                f"rw={self.rw}, rl={self.rl})")

    # ── Guillotine split ──────────────────────────────────────────────────────

    def split(self, item_W: float, item_L: float) -> list:
        """
        After placing an (item_W × item_L) item at this rect's corner,
        return up to 2 remaining free rectangles.

        Uses the Longer-Axis rule: split along the longer remaining dimension
        to keep offcuts as large as possible.
        """
        parts  = []
        gap_W  = self.rw - item_W   # leftover width  to the right
        gap_L  = self.rl - item_L   # leftover length above

        if self.rw >= self.rl:
            # Wide rect → right strip spans full rl
            if gap_W > 0:
                parts.append(Rect(self.x + item_W, self.y, gap_W, self.rl))
            if gap_L > 0:
                parts.append(Rect(self.x, self.y + item_L, item_W, gap_L))
        else:
            # Tall rect → top strip spans full rw
            if gap_L > 0:
                parts.append(Rect(self.x, self.y + item_L, self.rw, gap_L))
            if gap_W > 0:
                parts.append(Rect(self.x + item_W, self.y, gap_W, item_L))

        return parts

    # ── Containment ───────────────────────────────────────────────────────────

    def contains(self, other) -> bool:
        """True if `other` lies entirely within `self`."""
        return (
            self.x  <= other.x
            and self.y  <= other.y
            and self.x  + self.rw >= other.x  + other.rw
            and self.y  + self.rl >= other.y  + other.rl
        )


# ─────────────────────────────────────────────────────────────────────────────


def _orients(st):
    """
    Allowed (across_width, along_length) orientations for a stack tuple.

    Stack tuples are (L, W, weight, key) or (L, W, weight, key, rotatable).
    A 2-way-access package is stored already turned the right way and marked
    rotatable=False, so only its given orientation is offered — it can never be
    rotated by any layout. 4-way packages (the default) offer both.
    """
    L, W = st[0], st[1]
    rotatable = st[4] if len(st) > 4 else True
    return ((W, L), (L, W)) if rotatable else ((W, L),)


def lane_layout(stacks, CW, CL, MWT=float("inf"), sort_mode="maxdim",
                lane_bias="wide", balance=False, EPS=1.0):
    """
    Pack footprints into LANES that run along the container LENGTH.

    Each lane has a fixed width; footprints are dropped into a lane along the
    length (choosing, per footprint, the orientation whose cross dimension best
    fills the lane so the least length is used), and new lanes are opened across
    the WIDTH as needed.

    lane_bias controls how WIDE a new lane is opened:
      "wide"   -> a new lane takes the footprint's LARGER side across the width
                  (good when two footprints' larger sides fill the width, e.g.
                  1193.8 + 1092.2 = 2286).
      "narrow" -> a new lane takes the footprint's SMALLER side across the width
                  (good when several footprints' smaller sides tile the width,
                  e.g. 889 + 762 + 635 = 2286, racks turned lengthwise).
    Both are tried by callers and the fewest-container / best-fitting result is
    kept, so tight arrangements a rectangle packer misses are found.

    stacks    : list of (L, W, weight, key) tuples (key identifies the stack).
    returns   : (placements, leftover)
                placements = [(key, x, y, iW, iL), ...]  (x=width pos, y=length pos)
                leftover   = the (L,W,weight,key) tuples that did not fit.
    """
    if sort_mode == "maxdim":
        keyf = lambda s: max(s[0], s[1])
    elif sort_mode == "area":
        keyf = lambda s: s[0] * s[1]
    else:                       # "mindim"
        keyf = lambda s: min(s[0], s[1])
    order = sorted(stacks, key=keyf, reverse=True)

    lanes = []                  # {x, w, y}
    placements, leftover = [], []
    used_w, weight = 0.0, 0.0

    for st in order:
        L, W, wt, k = st[0], st[1], st[2], st[3]
        if weight + wt > MWT + EPS:
            leftover.append(st)
            continue
        done = False
        # 1) drop into an existing lane. With balance=True we choose the
        #    SHORTEST lane that fits, so the load spreads evenly across the
        #    width (e.g. 6 + 6) instead of filling one lane and leaving a stub
        #    (10 + 2), which would put the weight off-centre in the container.
        lane_order = sorted(lanes, key=lambda ln: ln["y"]) if balance else lanes
        for lane in lane_order:
            opts = [(a, b) for (a, b) in _orients(st)
                    if a <= lane["w"] + EPS and lane["y"] + b <= CL + EPS]
            if opts:
                a, b = max(opts, key=lambda o: o[0])
                placements.append((k, lane["x"], lane["y"], a, b))
                lane["y"] += b
                weight += wt
                done = True
                break
        if done:
            continue
        # 2) open a new lane across the remaining width, per the bias
        rem = CW - used_w
        opts = [(a, b) for (a, b) in _orients(st)
                if a <= rem + EPS and b <= CL + EPS]
        if not opts:
            leftover.append(st)
            continue
        if lane_bias == "narrow":
            a, b = min(opts, key=lambda o: o[0])   # narrowest lane
        else:
            a, b = max(opts, key=lambda o: o[0])   # widest lane
        lanes.append({"x": used_w, "w": a, "y": b})
        placements.append((k, used_w, 0.0, a, b))
        used_w += a
        weight += wt

    return placements, leftover


def shelf_layout(stacks, CW, CL, MWT=float("inf"), sort_mode="maxdim", EPS=1.0):
    """
    Shelf/row packing along the container LENGTH.

    Footprints are laid in rows across the WIDTH; when a row fills, the next row
    starts further along the length. Each footprint is turned to the orientation
    that packs the most per row for the least length (its column-efficient
    orientation). This finds "sectioned" fits a lane packer misses — e.g. four
    rows of two 1143-wide racks, then rows of one 1626-wide rack — keeping
    everything inside the walls.

    stacks  : list of (L, W, weight, key) tuples.
    returns : (placements, leftover); placements = [(key, x, y, iW, iL), ...].
    """
    if sort_mode == "area":
        keyf = lambda s: s[0] * s[1]
    elif sort_mode == "mindim":
        keyf = lambda s: min(s[0], s[1])
    else:                       # "maxdim"
        keyf = lambda s: max(s[0], s[1])
    order = sorted(stacks, key=keyf, reverse=True)

    def best_orient(L, W, st=None):
        best = None
        for across, along in (_orients(st) if st is not None else ((W, L), (L, W))):
            if across <= CW + EPS:
                per = int((CW + EPS) // across)
                if per >= 1:
                    cost = along / per          # length used per stack
                    if best is None or cost < best[0]:
                        best = (cost, across, along)
        return best

    placements, leftover = [], []
    y = 0.0                      # length position of current row
    row_x = 0.0                  # width filled in current row
    row_depth = 0.0              # deepest stack in current row
    weight = 0.0

    for st in order:
        L, W, wt, k = st[0], st[1], st[2], st[3]
        if weight + wt > MWT + EPS:
            leftover.append(st)
            continue
        # Candidate orientations, most space-efficient first, but ALWAYS try the
        # other one as a fallback — otherwise a rack whose preferred orientation
        # is too deep for the remaining length gets dropped (and later drawn
        # outside the container) even though it would fit turned the other way.
        cands = []
        for across, along in _orients(st):
            if across <= CW + EPS:
                per = max(1, int((CW + EPS) // across))
                cands.append((along / per, across, along))
        cands.sort()
        if not cands:
            leftover.append(st)
            continue

        placed = False
        # 1) fit in the current row
        for _c, across, along in cands:
            if row_x + across <= CW + EPS and y + along <= CL + EPS:
                placements.append((k, row_x, y, across, along))
                row_x += across
                row_depth = max(row_depth, along)
                weight += wt
                placed = True
                break
        if placed:
            continue
        # 2) start a new row
        ny = y + row_depth
        for _c, across, along in cands:
            if across <= CW + EPS and ny + along <= CL + EPS:
                y, row_x, row_depth = ny, 0.0, along
                placements.append((k, 0.0, y, across, along))
                row_x = across
                weight += wt
                placed = True
                break
        if not placed:
            leftover.append(st)

    return placements, leftover


def maxrects_layout_all(stacks, CW, CL, sort_key=None, EPS=1e-6):
    """
    TRUE MaxRects placement (not guillotine): when an item is placed, every free
    rectangle it overlaps is split into up to four maximal rectangles, and
    contained rectangles are pruned. This keeps far more usable space than the
    guillotine split and fits loads the simpler packer cannot.

    Returns [(key, x, y, iW, iL), ...] or None if any footprint cannot be placed.
    """
    if sort_key is None:
        sort_key = lambda s: s[0] * s[1]
    free = [(0.0, 0.0, CW, CL)]

    def prune(fs):
        out = []
        for i, a in enumerate(fs):
            contained = False
            for j, b in enumerate(fs):
                if i != j and (b[0] <= a[0] + EPS and b[1] <= a[1] + EPS
                               and b[0] + b[2] >= a[0] + a[2] - EPS
                               and b[1] + b[3] >= a[1] + a[3] - EPS):
                    # keep only one of two identical rects
                    if (b[2] * b[3] > a[2] * a[3]) or (j < i):
                        contained = True
                        break
            if not contained:
                out.append(a)
        return out

    placements = []
    for st in sorted(stacks, key=sort_key, reverse=True):
        L, W = st[0], st[1]
        k = st[3]
        best = None
        _os = _orients(st)
        for (fx, fy, fw, fl) in free:
            for (a, b) in _os:
                if a <= fw + EPS and b <= fl + EPS:
                    score = (min(fw - a, fl - b), fy, fx)   # best short side, then nose-first
                    if best is None or score < best[0]:
                        best = (score, fx, fy, a, b)
        if best is None:
            return None
        _s, x, y, a, b = best
        placements.append((k, x, y, a, b))
        nf = []
        for (fx, fy, fw, fl) in free:
            if x >= fx + fw - EPS or x + a <= fx + EPS or \
               y >= fy + fl - EPS or y + b <= fy + EPS:
                nf.append((fx, fy, fw, fl))
                continue
            if x > fx + EPS:                nf.append((fx, fy, x - fx, fl))
            if x + a < fx + fw - EPS:       nf.append((x + a, fy, fx + fw - (x + a), fl))
            if y > fy + EPS:                nf.append((fx, fy, fw, y - fy))
            if y + b < fy + fl - EPS:       nf.append((fx, y + b, fw, fy + fl - (y + b)))
        free = prune([f for f in nf if f[2] > EPS and f[3] > EPS])
    return placements


def uniform_width_lanes(stacks, CW, CL, EPS=1.0):
    """
    Exact lane splitting for the common case where every package has the SAME
    width, so the floor is simply k identical lanes running nose-to-doors.

    The general lane heuristic fills lanes one after another and can fail on a
    tight load (two lanes at 99.5% need a specific split of the lengths). Here
    the lengths are distributed with Best-Fit-Decreasing across all k lanes at
    once, which finds those splits. Returns placements or None.
    """
    ws = set()
    for s in stacks:
        rot = s[4] if len(s) > 4 else True
        ws.add(round(float(s[1]), 2))
        if rot:
            ws.add(round(float(s[1]), 2))        # natural orientation only
    if len(ws) != 1:
        return None
    w = ws.pop()
    if w <= 0:
        return None
    k = int((CW + EPS) // w)
    if k < 1:
        return None

    order = sorted(stacks, key=lambda s: -float(s[0]))     # longest first

    # EXACT split for the very common 2-lane case. Best-fit alone fails on a
    # tight load (e.g. two lanes at 99.5%) because the lengths have to be
    # divided just so; a subset-sum search finds that division when it exists.
    if k == 2 and len(order) <= 60:
        step = 10.0
        cap = int(CL // step)
        sizes = [int(-(-float(s[0]) // step)) for s in order]
        total = sum(sizes)
        if total <= 2 * cap:
            reach = {0: None}                    # sum -> (prev_sum, item_index)
            for i, sz in enumerate(sizes):
                for v in sorted(reach.keys(), reverse=True):
                    nv = v + sz
                    if nv <= cap and nv not in reach:
                        reach[nv] = (v, i)
            need = max(0, total - cap)
            hit = None
            for v in sorted(reach.keys(), reverse=True):
                if v >= need:                    # other lane then fits too
                    hit = v
                    break
            if hit is not None:
                picked, v = set(), hit
                while v and reach.get(v):
                    pv, idx = reach[v]
                    picked.add(idx)
                    v = pv
                laneA = [order[i] for i in range(len(order)) if i in picked]
                laneB = [order[i] for i in range(len(order)) if i not in picked]
                placements = []
                for li, lane in enumerate((laneA, laneB)):
                    y = 0.0
                    for s in lane:
                        placements.append((s[3], li * w, y, w, float(s[0])))
                        y += float(s[0])
                    if y > CL + EPS:
                        placements = None
                        break
                if placements:
                    return placements

    lanes = [[0.0, []] for _ in range(k)]
    for s in order:
        L = float(s[0])
        if L > CL + EPS:
            return None
        # best fit: the lane that ends up fullest while still fitting
        best_i, best_left = -1, None
        for i, ln in enumerate(lanes):
            left = CL - ln[0] - L
            if left >= -EPS and (best_left is None or left < best_left):
                best_i, best_left = i, left
        if best_i < 0:
            return None
        lanes[best_i][1].append(s)
        lanes[best_i][0] += L

    placements = []
    for i, ln in enumerate(lanes):
        y = 0.0
        for s in ln[1]:
            placements.append((s[3], i * w, y, w, float(s[0])))
            y += float(s[0])
    return placements


def corner_layout(stacks, CW, CL, sort_key=None, score="bl", EPS=1e-6):
    """
    Corner-point (bottom-left-fill) placement.

    Lane, shelf and guillotine packers all cut the floor into strips, so they
    can never produce an INTERLOCKING arrangement - e.g. a pinwheel, where one
    rack tucks into the corner made by two others. This placer keeps a list of
    candidate corner points (starting at the nose-left corner) and drops each
    rack at the lowest/leftmost corner it fits, creating two new corners. It
    finds interlocking patterns the strip methods cannot.

    Used as a LAST RESORT in find_layout: measured on 300 random loads it
    placed 2 that every other method rejected, while never losing a case the
    others could already do.
    """
    if sort_key is None:
        sort_key = lambda s: -float(s[0]) * float(s[1])
    order = sorted(stacks, key=sort_key)
    placed, pts = [], [(0.0, 0.0)]

    def _fits(x, y, w, l):
        if x + w > CW + EPS or y + l > CL + EPS:
            return False
        for (px, py, pw, pl, _k) in placed:
            if (x + w > px + EPS and px + pw > x + EPS
                    and y + l > py + EPS and py + pl > y + EPS):
                return False
        return True

    for s in order:
        L, W = float(s[0]), float(s[1])
        rot = s[4] if len(s) > 4 else True
        best = None
        for (a, b) in (((W, L), (L, W)) if rot else ((W, L),)):
            for (x, y) in pts:
                if _fits(x, y, a, b):
                    sc = (y + b, x + a) if score == "bl" else (x + a, y + b)
                    if best is None or sc < best[0]:
                        best = (sc, x, y, a, b)
        if best is None:
            return None
        _sc, x, y, a, b = best
        placed.append((x, y, a, b, s[3]))
        for p in ((x + a, y), (x, y + b)):
            if p not in pts and p[0] <= CW + EPS and p[1] <= CL + EPS:
                pts.append(p)
    return [(k, x, y, a, b) for (x, y, a, b, k) in placed]


def find_layout(stacks, CW, CL, EPS=1.0):
    """
    THE shared layout finder used by BOTH the packer (to verify a container's
    contents can actually be arranged) and the report (to draw them).

    Tries every layout strategy — lane (wide/narrow), shelf/row, and true
    MaxRects with several sort orders — and returns the first arrangement that
    places EVERY stack strictly inside the container walls, or None if no
    strategy can. Because the packer and the report call the same function,
    the report can always draw exactly what the packer says fits: no rack is
    ever drawn outside the container.

    stacks  : list of (L, W, weight, key) tuples.
    returns : [(key, x, y, iW, iL), ...] or None.
    """
    if not stacks:
        return []

    # Canonicalise the input order. The layout heuristics sort internally, but
    # Python's sort is STABLE, so equal-sized items keep their incoming order -
    # which means the same set of packages could succeed for one caller and
    # fail for another that happened to pass them in a different order. The
    # packer validates a container and the report then draws it, so the two
    # MUST agree.
    #
    # The tie-break uses only INTRINSIC properties (size, weight, rotatability)
    # and never the caller's key: keys are positional indices, which change
    # when a caller sorts its stacks first. Items that tie on all of these are
    # geometrically interchangeable, so their relative order cannot matter.
    stacks = sorted(stacks, key=lambda s: (-float(s[0]), -float(s[1]),
                                           -float(s[2]),
                                           0 if (s[4] if len(s) > 4 else True) else 1))

    def ok(pl, lo=()):
        return (pl and not lo
                and max(x + a for (k, x, y, a, b) in pl) <= CW + EPS
                and max(y + b for (k, x, y, a, b) in pl) <= CL + EPS)

    # Uniform-width loads (every package the same width) split into k identical
    # lanes; solving that exactly first lets very tight loads (two lanes at
    # 99.5%) be arranged, which the general heuristics cannot always find.
    pl = uniform_width_lanes(stacks, CW, CL, EPS)
    if ok(pl):
        return pl

    # Balanced lane fills first (spread evenly across the width), then the
    # tight fills as a fallback for loads that only fit when packed hard.
    for bal in (True, False):
        for mode in ("maxdim", "area", "mindim"):
            for bias in ("wide", "narrow"):
                pl, lo = lane_layout(stacks, CW, CL, float("inf"), mode, bias,
                                     balance=bal)
                if ok(pl, lo):
                    return pl
    for mode in ("maxdim", "area", "mindim"):
        pl, lo = shelf_layout(stacks, CW, CL, float("inf"), mode)
        if ok(pl, lo):
            return pl
    for key in (lambda s: s[0] * s[1],
                lambda s: max(s[0], s[1]),
                lambda s: s[0],
                lambda s: s[1],
                lambda s: min(s[0], s[1])):
        pl = maxrects_layout_all(stacks, CW, CL, key)
        if pl and len(pl) == len(stacks) and ok(pl):
            return pl

    # LAST RESORT: corner-point placement, which can produce interlocking
    # (non-guillotine) arrangements the strip/rectangle methods cannot.
    for key in (lambda s: -float(s[0]) * float(s[1]),
                lambda s: float(s[0]) * float(s[1]),
                lambda s: -max(float(s[0]), float(s[1])),
                lambda s: -float(s[0])):
        for sc in ("bl", "lb"):
            pl = corner_layout(stacks, CW, CL, key, sc)
            if pl and len(pl) == len(stacks) and ok(pl):
                return pl
    return None


class MaxRectsBin:
    """
    2-D floor bin-packer using guillotine splits + Best-Short-Side-Fits.

    Constructor
    -----------
    MaxRectsBin(container_W, container_L)
      container_W : container floor WIDTH  (mm)
      container_L : container floor LENGTH (mm)

    place(item_W, item_L) → bool
      item_W : item dimension along WIDTH  axis
      item_L : item dimension along LENGTH axis
      Both 90° orientations are tried; the tighter fit is chosen.
      Returns True on success, False if no space available.

    can_place(item_W, item_L) → bool
      Non-mutating check — True if item (or its rotation) fits anywhere.
    """

    def __init__(self, container_W: float, container_L: float):
        self.W = container_W
        self.L = container_L
        self.free: list[Rect] = [Rect(0.0, 0.0, container_W, container_L)]

    # ── Public API ────────────────────────────────────────────────────────────

    def can_place(self, item_W: float, item_L: float,
                  allow_rotate: bool = True) -> bool:
        """
        True if the item fits anywhere.

        allow_rotate : when True (default) the 90° rotation is also tried;
                       when False only the given (item_W × item_L) orientation
                       is considered (used by the width-strip pass, which has
                       already chosen its orientation).
        """
        for fr in self.free:
            if fr.fits(item_W, item_L):
                return True
            if allow_rotate and fr.fits(item_L, item_W):
                return True
        return False

    def place(self, item_W: float, item_L: float,
              allow_rotate: bool = True) -> bool:
        """
        Place one item of (item_W × item_L).

        item_W goes along the container WIDTH  axis.
        item_L goes along the container LENGTH axis.

        allow_rotate : when True (default) the 90° rotation is also tried and
                       the tighter fit is chosen; when False the item is placed
                       only in the given orientation.

        Returns True on success, False if no free rect can accommodate it.
        """
        best_fr  = None
        best_sc  = None
        best_iW  = item_W   # chosen width  dimension of placed item
        best_iL  = item_L   # chosen length dimension of placed item

        orientations = (((item_W, item_L), (item_L, item_W))
                        if allow_rotate else ((item_W, item_L),))

        for fr in self.free:
            for iW, iL in orientations:
                if not fr.fits(iW, iL):
                    continue
                # Best Short Side Fits: minimise the smaller leftover dimension
                score = min(fr.rw - iW, fr.rl - iL)
                if best_fr is None or score < best_sc:
                    best_fr = fr
                    best_sc = score
                    best_iW = iW
                    best_iL = iL

        if best_fr is None:
            return False

        new_parts = best_fr.split(best_iW, best_iL)
        self.free.remove(best_fr)
        self.free.extend(new_parts)
        self._prune()
        return True

    def place_located(self, item_W: float, item_L: float,
                      allow_rotate: bool = True):
        """
        Same placement rule as place(), but RETURNS where the item was put:
        (x, y, w, h) — x along width, y along length, w/h the placed extents
        (w/h swapped if it was rotated). Returns None if it did not fit.
        Used by the report so its drawing matches the packer's own layout.
        """
        best_fr = None
        best_sc = None
        best_iW = item_W
        best_iL = item_L
        orientations = (((item_W, item_L), (item_L, item_W))
                        if allow_rotate else ((item_W, item_L),))
        for fr in self.free:
            for iW, iL in orientations:
                if not fr.fits(iW, iL):
                    continue
                score = min(fr.rw - iW, fr.rl - iL)
                if best_fr is None or score < best_sc:
                    best_fr = fr
                    best_sc = score
                    best_iW = iW
                    best_iL = iL
        if best_fr is None:
            return None
        x, y = best_fr.x, best_fr.y
        new_parts = best_fr.split(best_iW, best_iL)
        self.free.remove(best_fr)
        self.free.extend(new_parts)
        self._prune()
        return (x, y, best_iW, best_iL)

    def free_area(self) -> float:
        return sum(r.area() for r in self.free)

    def used_area(self) -> float:
        return self.W * self.L - self.free_area()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _prune(self):
        """Remove free rects fully contained within another free rect."""
        to_remove: set = set()
        free = self.free
        n    = len(free)
        for i in range(n):
            if i in to_remove:
                continue
            for j in range(n):
                if i == j or j in to_remove:
                    continue
                if free[j].contains(free[i]):
                    to_remove.add(i)
                    break
        self.free = [r for i, r in enumerate(free) if i not in to_remove]
