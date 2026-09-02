# engine/packing.py
# ─────────────────────────────────────────────────────────────────────────────
# pack_containers_exact(df, container)
#
# Finds the MINIMUM number of containers to ship all requested racks.
#
# AXIS CONVENTION (matches geometry.py)
# ──────────────────────────────────────
#   WIDTH  (W) : cross dimension of container floor  (container["W"])
#   LENGTH (L) : depth  dimension of container floor  (container["L"])
#   HEIGHT (H) : vertical dimension                   (container["H"])
#
#   All bin_.place() calls use (item_W, item_L) — WIDTH first, LENGTH second.
#
# ALGORITHM
# ─────────────────────────────────────────────────────────────────────────────
# Per container (one full greedy run):
#   PASS 1 – Width-strip scan
#     For each rack type (in a given order):
#       • Evaluate BOTH orientations; pick the one with more units/mm of LENGTH.
#       • fit_across = floor(container_W / rack_W_in_chosen_orientation)
#       • actual_used_W = fit_across × rack_W
#       • units_per_row = fit_across × stack_count
#       • Each row registered as (actual_used_W, row_L) in the bin.
#         Using actual width (not full CW) keeps the side gap available.
#
#   PASS 2 – MaxRects 2-D heterogeneous packing
#     Rotation allowed. Sorted by rack LENGTH desc to avoid short items
#     fragmenting long free rectangles.  After each placement, retries
#     previously-skipped rack types.
#
#   PASS 3 – Backfill (smallest footprint first)
#
# The full run is repeated for every ordering in _strategies() and the
# result with fewest containers is returned.  A merge pass collapses
# adjacent containers where possible after every run.
#
# Weight limit enforced at every add step.
# ─────────────────────────────────────────────────────────────────────────────

from engine.geometry import MaxRectsBin
from engine.stacking import build_stacks
import itertools
import time


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _stacks(r_dims: dict, CH: float) -> int:
    """Vertical stack count for a rack inside container height CH (min 1)."""
    h = r_dims.get("Height (MM)", 0)
    if h <= 0:
        return 1
    return max(1, int(CH // h))


def _max_add(used_wt: float, candidate: int, qty: int,
             unit_wt: float, max_wt: float) -> int:
    """Largest integer ≤ min(candidate, qty) keeping total weight ≤ max_wt."""
    add = min(candidate, qty)
    if unit_wt <= 0:
        return add
    while add > 0 and used_wt + add * unit_wt > max_wt:
        add -= 1
    return add


# ══════════════════════════════════════════════════════════════════════════════
#  Single-container packer
# ══════════════════════════════════════════════════════════════════════════════

def _pack_one(remaining: dict, dims: dict,
              CL: float, CW: float, CH: float, MWT: float,
              rack_order: list,
              mutate: bool = True):
    """
    Fill one container from `remaining` stock.

    Parameters
    ----------
    remaining  : { rack_name: qty }  — mutated when mutate=True
    dims       : { rack_name: { "Length (MM)":…, "Width (MM)":…, … } }
    rack_order : sequence controlling Pass 1 strip priority
    mutate     : subtract loaded quantities from `remaining` when True

    Returns
    -------
    (load_dict, all_done_bool)
    """

    work    = {r: v for r, v in remaining.items() if v > 0}
    load    : dict = {}
    used_wt = 0.0
    bin_    = MaxRectsBin(CW, CL)   # bin WIDTH=CW, bin LENGTH=CL

    # ── PASS 1 : Width-strip packing ─────────────────────────────────────────
    for r in rack_order:
        qty = work.get(r, 0)
        if qty <= 0:
            continue

        # Rack physical dimensions
        rack_L  = float(dims[r]["Length (MM)"])
        rack_W  = float(dims[r]["Width (MM)"])
        r_wt    = float(dims[r]["Weight (Kg)"])
        stack   = _stacks(dims[r], CH)

        if rack_L <= 0 or rack_W <= 0:
            continue

        # Evaluate both orientations:
        #   normal  : rack_W across container WIDTH,  rack_L into container LENGTH
        #   rotated : rack_L across container WIDTH,  rack_W into container LENGTH
        best = None  # (strip_W, strip_L, across, upr)
        for iW, iL in ((rack_W, rack_L), (rack_L, rack_W)):
            across = int(CW // iW)
            if across == 0:
                continue
            upr = across * stack
            # units per mm of LENGTH consumed — higher is better
            score = upr / iL
            if best is None or score > best[4]:
                best = (iW, iL, across, upr, score)

        if best is None:
            continue   # rack doesn't fit in either orientation

        iW, iL, across, upr, _ = best

        while qty > 0:
            # How many units this row can take (full row = across*stack),
            # capped by remaining qty and the weight limit.
            add = _max_add(used_wt, upr, qty, r_wt, MWT)
            if add <= 0:
                break

            # Reserve ONLY the width the placed units actually occupy, not the
            # full across-width. A row is `across` columns wide and `stack`
            # high, so the number of columns used is ceil(add / stack). This
            # leaves the unused width free, so other racks of the same
            # footprint can sit SIDE BY SIDE in the same row (fixes distinct
            # single-qty racks being spread one-per-row across containers).
            cols_used = -(-add // stack)          # ceil division
            strip_W   = cols_used * iW

            # Placed in the orientation already chosen above — no rotation here,
            # otherwise a narrow strip could be rotated and break the side-by-side
            # tiling. (Passes 2 & 3 still allow rotation for leftovers.)
            if not bin_.can_place(strip_W, iL, allow_rotate=False):
                break
            if not bin_.place(strip_W, iL, allow_rotate=False):
                break

            load[r]   = load.get(r, 0) + add
            work[r]   = work.get(r, 0) - add
            used_wt  += add * r_wt
            qty      -= add

    # ── PASS 2 : MaxRects 2-D heterogeneous packing ──────────────────────────
    # Sorted by rack LENGTH desc (long racks first prevents short items from
    # cutting the free rect and leaving insufficient length for long racks).
    p2 = sorted(
        [r for r in work if work.get(r, 0) > 0],
        key=lambda r: dims[r]["Length (MM)"],
        reverse=True,
    )
    skipped = []

    for r in p2:
        qty = work.get(r, 0)
        if qty <= 0:
            continue

        rack_L  = float(dims[r]["Length (MM)"])
        rack_W  = float(dims[r]["Width (MM)"])
        r_wt    = float(dims[r]["Weight (Kg)"])
        stack   = _stacks(dims[r], CH)

        while qty > 0:
            # bin_.can_place / place use (item_W, item_L); both rotations tried inside
            if not bin_.can_place(rack_W, rack_L):
                skipped.append(r); break
            add = _max_add(used_wt, stack, qty, r_wt, MWT)
            if add <= 0:
                skipped.append(r); break
            if not bin_.place(rack_W, rack_L):
                skipped.append(r); break
            load[r]  = load.get(r, 0) + add
            work[r]  = work.get(r, 0) - add
            used_wt += add * r_wt
            qty     -= add

        # Retry previously skipped racks after each successful placement
        still = []
        for sr in skipped:
            sq = work.get(sr, 0)
            if sq <= 0:
                continue
            sW   = float(dims[sr]["Width (MM)"])
            sL   = float(dims[sr]["Length (MM)"])
            swt  = float(dims[sr]["Weight (Kg)"])
            ss   = _stacks(dims[sr], CH)
            if bin_.can_place(sW, sL):
                sadd = _max_add(used_wt, ss, sq, swt, MWT)
                if sadd > 0 and bin_.place(sW, sL):
                    load[sr]  = load.get(sr, 0) + sadd
                    work[sr]  = work.get(sr, 0) - sadd
                    used_wt  += sadd * swt
                    continue
            still.append(sr)
        skipped = still

    # ── PASS 3 : Backfill (smallest footprint first) ──────────────────────────
    for r in sorted(
        [r for r in work if work.get(r, 0) > 0],
        key=lambda r: dims[r]["Length (MM)"] * dims[r]["Width (MM)"],
    ):
        qty = work.get(r, 0)
        if qty <= 0:
            continue
        rack_L  = float(dims[r]["Length (MM)"])
        rack_W  = float(dims[r]["Width (MM)"])
        r_wt    = float(dims[r]["Weight (Kg)"])
        stack   = _stacks(dims[r], CH)

        while qty > 0:
            if not bin_.can_place(rack_W, rack_L):
                break
            add = _max_add(used_wt, stack, qty, r_wt, MWT)
            if add <= 0:
                break
            if not bin_.place(rack_W, rack_L):
                break
            load[r]  = load.get(r, 0) + add
            work[r]  = work.get(r, 0) - add
            used_wt += add * r_wt
            qty     -= add

    if mutate:
        for r, q in load.items():
            remaining[r] = remaining.get(r, 0) - q

    all_done = all(v <= 0 for v in work.values())
    return load, all_done


# ══════════════════════════════════════════════════════════════════════════════
#  Full greedy run for one ordering
# ══════════════════════════════════════════════════════════════════════════════

def _greedy_run(initial: dict, dims: dict,
                CL: float, CW: float, CH: float, MWT: float,
                rack_order: list, deadline: float = None) -> list:
    remaining = dict(initial)
    containers = []
    _n = 0
    while any(v > 0 for v in remaining.values()):
        # Give up (return None) if we blow the time budget on a very large job,
        # so the caller can fall back to the fast approximate result.
        _n += 1
        if deadline is not None and (_n & 15) == 0 and time.time() > deadline:
            return None
        load, _ = _pack_one(remaining, dims, CL, CW, CH, MWT,
                            rack_order, mutate=True)
        if not load:
            break
        containers.append(load)
    return containers


# ══════════════════════════════════════════════════════════════════════════════
#  Merge pass
# ══════════════════════════════════════════════════════════════════════════════

def _merge(containers: list, dims: dict,
           CL: float, CW: float, CH: float, MWT: float,
           rack_order: list) -> list:
    """Repeatedly try to merge adjacent containers into one."""
    changed = True
    while changed and len(containers) >= 2:
        changed = False
        for idx in range(len(containers) - 1, 0, -1):
            a, b = containers[idx - 1], containers[idx]
            total_wt = sum(
                q * dims[r]["Weight (Kg)"]
                for load in (a, b) for r, q in load.items()
            )
            if total_wt > MWT:
                continue
            merged = dict(a)
            for r, q in b.items():
                merged[r] = merged.get(r, 0) + q
            trial = dict(merged)
            tlod, ok = _pack_one(trial, dims, CL, CW, CH, MWT,
                                 rack_order, mutate=True)
            if ok:
                containers[idx - 1] = tlod
                containers.pop(idx)
                changed = True
                break
    return containers


# ══════════════════════════════════════════════════════════════════════════════
#  Strategy generator
# ══════════════════════════════════════════════════════════════════════════════

def _single_footprint_pack(stack_initial: dict, stack_dims: dict,
                           CL: float, CW: float, MWT: float):
    """
    Optimal container count when every stack shares ONE footprint.

    Fills the container WIDTH with the best mix of two column orientations:
      - "normal"  column: rack width (rw) across, holds floor(CL/rl) along length
      - "rotated" column: rack length (rl) across, holds floor(CL/rw) along length
    A small width-knapsack picks how many of each column maximises the stacks
    per container (e.g. 1200x800 in a 2350-wide 40 HC -> one 800-wide lane of
    10 + one 1200-wide lane of 15 = 25 per container, instead of 20).

    Returns a list of containers ({sid: 1}), or None if the stacks are NOT all
    one footprint (so the normal strategy search is used instead).
    """
    sids = list(stack_initial.keys())
    if not sids:
        return None

    foot = None
    for sid in sids:
        rl = round(float(stack_dims[sid]["Length (MM)"]), 1)
        rw = round(float(stack_dims[sid]["Width (MM)"]),  1)
        if foot is None:
            foot = (rl, rw)
        elif (rl, rw) != foot:
            return None                      # more than one footprint -> skip
    rl, rw = foot
    if rl <= 0 or rw <= 0:
        return None

    capA = int(CL // rl)                      # normal column: rw wide
    capB = int(CL // rw)                      # rotated column: rl wide

    # width-knapsack: maximise a*capA + b*capB with a*rw + b*rl <= CW
    best_cap, _ba, _bb = 0, 0, 0
    max_a = int(CW // rw) if rw > 0 else 0
    for a in range(max_a + 1):
        rem = CW - a * rw
        b = int(rem // rl) if rl > 0 else 0
        tot = a * capA + b * capB
        if tot > best_cap:
            best_cap, _ba, _bb = tot, a, b
    if best_cap <= 0:
        return None

    if best_cap <= 0:
        return None

    # Distribute the (possibly many) identical stacks across containers:
    # best_cap footprints per container, heaviest first, respecting the weight
    # limit. Each sid carries a COUNT (grouped identical stacks).
    remaining = {s: int(stack_initial[s]) for s in sids}
    order = sorted(sids, key=lambda s: float(stack_dims[s]["Weight (Kg)"]),
                   reverse=True)
    total = sum(remaining.values())
    containers, guard = [], 0
    while total > 0:
        guard += 1
        if guard > total + len(sids) + 5:
            return None
        cont, slots, wt_left, placed_any = {}, best_cap, MWT, False
        for s in order:
            if slots <= 0:
                break
            w = float(stack_dims[s]["Weight (Kg)"])
            cap_w = int(wt_left // w) if w > 0 else remaining[s]
            take = min(slots, remaining[s], cap_w)
            if take > 0:
                cont[s] = cont.get(s, 0) + take
                remaining[s] -= take
                slots -= take
                wt_left -= take * w
                total -= take
                placed_any = True
        if not placed_any:
            return None                      # even one stack won't fit by weight
        containers.append(cont)
    return containers


def _strategies(racks: list, dims: dict) -> list:
    """
    All orderings to try.  ≤7 types → all permutations.  >7 → curated set.
    Also includes segregation orderings (each rack type forced to strip-pass
    front) to handle cases where one awkward type packs best alone.
    """
    n = len(racks)

    if n <= 7:
        base = [list(p) for p in itertools.permutations(racks)]
    else:
        def srt(key_fn, rev):
            return sorted(racks, key=key_fn, reverse=rev)
        base = [
            srt(lambda r: dims[r]["Length (MM)"],                         True),
            srt(lambda r: dims[r]["Length (MM)"],                         False),
            srt(lambda r: dims[r]["Width (MM)"],                          True),
            srt(lambda r: dims[r]["Width (MM)"],                          False),
            srt(lambda r: dims[r]["Length (MM)"] * dims[r]["Width (MM)"], True),
            srt(lambda r: dims[r]["Length (MM)"] * dims[r]["Width (MM)"], False),
            srt(lambda r: dims[r]["Height (MM)"],                         True),
            srt(lambda r: dims[r]["Weight (Kg)"],                         True),
            racks[:],
            racks[::-1],
        ]

    # Segregation: each rack type first, rest sorted by length desc.
    # Skipped when there are many item types (would be too many full runs).
    segregations = []
    if n <= 12:
        for r in racks:
            others = sorted([x for x in racks if x != r],
                            key=lambda x: dims[x]["Length (MM)"], reverse=True)
            segregations.append([r] + others)
            segregations.append(others + [r])

    return base + segregations


# ══════════════════════════════════════════════════════════════════════════════
#  Public entry point
# ══════════════════════════════════════════════════════════════════════════════

def _lane_pack(stack_initial: dict, stack_dims: dict,
               CL: float, CW: float, MWT: float):
    """
    Lane bin-packing for the common case where every stack shares one WIDTH
    (e.g. all racks 1219.2 wide). The container is k = floor(CW/width) lanes;
    each lane is filled along the LENGTH with a MIX of different-length racks
    (First-Fit-Decreasing), so short and long racks share a lane with no gap.
    Lanes are then packed k-per-container under the weight limit.

    Returns a list of containers ({sid: 1}) or None if widths differ / infeasible.
    """
    sids = list(stack_initial.keys())
    if not sids:
        return None
    widths = {round(float(stack_dims[s]["Width (MM)"]), 1) for s in sids}
    if len(widths) != 1:
        return None
    w = widths.pop()
    if w <= 0 or w > CW:
        return None
    k = int(CW // w)
    if k < 1:
        return None

    EPS = 1e-6
    total = sum(int(stack_initial[s]) for s in sids)
    if total > 4000:            # too many to lane-pack cheaply; let greedy handle it
        return None
    items = []                  # expand grouped counts to individual stacks
    for s in sids:
        items.extend([s] * int(stack_initial[s]))
    items.sort(key=lambda s: float(stack_dims[s]["Length (MM)"]), reverse=True)  # FFD
    lanes = []                                                     # {used, wt, sids}
    for s in items:
        L  = float(stack_dims[s]["Length (MM)"])
        sw = float(stack_dims[s]["Weight (Kg)"])
        if L > CL + EPS or sw > MWT + EPS:
            return None                                           # can't fit at all
        placed = False
        for lane in lanes:
            if lane["used"] + L <= CL + EPS and lane["wt"] + sw <= MWT + EPS:
                lane["used"] += L; lane["wt"] += sw; lane["sids"].append(s)
                placed = True; break
        if not placed:
            lanes.append({"used": L, "wt": sw, "sids": [s]})

    lanes.sort(key=lambda ln: ln["wt"], reverse=True)             # heavy lanes first
    containers = []                                               # {lanes, wt, load}
    for lane in lanes:
        placed = False
        for c in containers:
            if c["lanes"] < k and c["wt"] + lane["wt"] <= MWT + EPS:
                c["lanes"] += 1; c["wt"] += lane["wt"]
                for s in lane["sids"]:
                    c["load"][s] = c["load"].get(s, 0) + 1
                placed = True; break
        if not placed:
            ld = {}
            for s in lane["sids"]:
                ld[s] = ld.get(s, 0) + 1
            containers.append({"lanes": 1, "wt": lane["wt"], "load": ld})
    return [c["load"] for c in containers]


def _column_pack(stack_initial: dict, stack_dims: dict,
                 CL: float, CW: float, MWT: float):
    """
    General greedy column packing: fill each container's width with columns
    (each column = one footprint in one orientation, stacked along the length),
    choosing at every step the column that loads the most stacks and mixing
    footprints/orientations in the leftover width. Handles mixed footprints.

    Returns a list of containers ({sid: 1}) or None if something cannot be placed.
    """
    from collections import defaultdict
    EPS = 1e-6
    groups = defaultdict(list)                 # (rl,rw) -> list of sids (expanded)
    for sid in stack_initial:
        rl = round(float(stack_dims[sid]["Length (MM)"]), 1)
        rw = round(float(stack_dims[sid]["Width (MM)"]),  1)
        rot = bool(stack_dims[sid].get("Rotatable", True))
        groups[(rl, rw, rot)].extend([sid] * int(stack_initial[sid]))
    remaining = {f: list(s) for f, s in groups.items()}

    def cols(f):
        rl, rw, rot = f
        out = []
        if 0 < rw <= CW:
            out.append((rw, int(CL // rl) if rl > 0 else 0))       # normal column
        # a 2-way package must not be turned, so only offer the rotated
        # column when this footprint is allowed to rotate
        if rot and 0 < rl <= CW and abs(rl - rw) > EPS:
            out.append((rl, int(CL // rw) if rw > 0 else 0))       # rotated column
        return [(cw, cap) for cw, cap in out if cap > 0]

    containers, guard = [], 0
    while any(remaining[f] for f in remaining):
        guard += 1
        if guard > 1000000:
            return None
        load, width_left, weight_left = {}, CW, MWT
        progress = True
        while progress and width_left > EPS:
            progress = False
            best = None
            for f in remaining:
                avail = remaining[f]
                if not avail:
                    continue
                for cw, cap in cols(f):
                    if cw > width_left + EPS:
                        continue
                    # take up to `cap` stacks from the front, capped by weight
                    ww, nn = 0.0, 0
                    limit = min(cap, len(avail))
                    for kk in range(limit):
                        w = float(stack_dims[avail[kk]]["Weight (Kg)"])
                        if ww + w > weight_left + EPS:
                            break
                        ww += w; nn += 1
                    if nn <= 0:
                        continue
                    key = (nn, nn / cw)
                    if best is None or key > best[0]:
                        best = (key, f, cw, nn, ww)
            if best is not None:
                _, f, cw, nn, ww = best
                take = remaining[f][:nn]
                remaining[f] = remaining[f][nn:]
                for sid in take:
                    load[sid] = load.get(sid, 0) + 1
                width_left -= cw
                weight_left -= ww
                progress = True
        if not load:
            return None                                            # stuck: infeasible
        containers.append(load)
    return containers


def _lane_mixed_pack(stack_initial: dict, stack_dims: dict,
                     CL: float, CW: float, MWT: float):
    """
    Mixed-footprint LANE packer. Fills the container width with lanes (full-
    length strips); each lane is filled along the length with a MIX of any
    footprints/orientations that fit its width (tightest-along orientation).
    This finds "complementary" arrangements that grid/column packers miss —
    e.g. a 1193.8-wide lane of one rack beside a 1092.2-wide lane of another,
    the two summing to the container width.

    Tries a few lane-orientation biases and sort orders and keeps the best.
    Returns a list of containers ({sid: count}) or None. Skipped for very large
    jobs (the greedy is O(n^2)); those use the time-budgeted search instead.
    """
    total = sum(int(v) for v in stack_initial.values())
    if total == 0 or total > 1200:
        return None
    EPS = 1e-6
    items = []
    for sid, c in stack_initial.items():
        d = stack_dims[sid]
        items.append((sid, float(d["Length (MM)"]), float(d["Width (MM)"]),
                      float(d["Weight (Kg)"]), int(c)))
    # expand to individual stacks
    flat = []
    for sid, L, W, wt, c in items:
        flat.extend([(sid, L, W, wt)] * c)

    def run(order, bias):
        remaining = [list(x) for x in order]
        containers, guard = [], 0
        while remaining:
            guard += 1
            if guard > len(flat) + 10:
                return None
            load, used_w, cwt, lanes = {}, 0.0, 0.0, []
            progress = True
            while progress:
                progress = False
                still = []
                for it in remaining:
                    sid, L, W, wt = it
                    done = False
                    if cwt + wt <= MWT + EPS:
                        best_lane = None
                        for lane in lanes:
                            opts = [(cr, al) for cr, al in ((W, L), (L, W))
                                    if cr <= lane["w"] + EPS and lane["f"] + al <= CL + EPS]
                            if opts:
                                cr, al = min(opts, key=lambda o: o[1])
                                if best_lane is None or al < best_lane[2]:
                                    best_lane = (lane, cr, al)
                        if best_lane:
                            lane, cr, al = best_lane
                            lane["f"] += al
                            load[sid] = load.get(sid, 0) + 1
                            cwt += wt; progress = True; done = True
                        else:
                            cands = [(cr, al) for cr, al in ((W, L), (L, W))
                                     if used_w + cr <= CW + EPS and al <= CL + EPS]
                            if cands:
                                cr, al = (max if bias == "wide" else min)(
                                    cands, key=lambda o: o[0])
                                lanes.append({"w": cr, "f": al})
                                used_w += cr
                                load[sid] = load.get(sid, 0) + 1
                                cwt += wt; progress = True; done = True
                    if not done:
                        still.append(it)
                remaining = still
            if not load:
                return None
            containers.append(load)
        return containers

    best = None
    sort_keys = (lambda f: max(f[1], f[2]),
                 lambda f: f[1] * f[2],
                 lambda f: min(f[1], f[2]))
    for sk in sort_keys:
        order = sorted(flat, key=sk, reverse=True)
        for bias in ("wide", "narrow"):
            r = run(order, bias)
            if r and (best is None or len(r) < len(best)):
                best = r
    return best


def _lane_mixed_pack(stack_initial: dict, stack_dims: dict,
                     CL: float, CW: float, MWT: float):
    """
    Mixed-footprint LANE packing: fills each container with lanes running along
    the length, letting different footprints/orientations share the width and
    the length. Finds tight "complementary" fits (e.g. a 1193.8-wide lane beside
    a 1092.2-wide lane filling a 2286-wide container) that the rectangle packer
    misses. Tries a few sort orders and keeps the fewest-container result.
    """
    from engine.geometry import lane_layout
    items = []
    for sid, cnt in stack_initial.items():
        L  = float(stack_dims[sid]["Length (MM)"])
        W  = float(stack_dims[sid]["Width (MM)"])
        wt = float(stack_dims[sid]["Weight (Kg)"])
        for _ in range(int(cnt)):
            items.append((L, W, wt, sid,
                          bool(stack_dims[sid].get("Rotatable", True))))
    if not items or len(items) > 1200:      # O(n^2) — skip for huge jobs
        return None

    best = None
    for mode in ("maxdim", "area", "mindim"):
        for bias in ("wide", "narrow"):
            remaining = list(items)
            containers, guard, ok = [], 0, True
            while remaining:
                guard += 1
                if guard > len(items) + 5:
                    ok = False
                    break
                placements, leftover = lane_layout(remaining, CW, CL, MWT,
                                                   mode, bias)
                if not placements:
                    ok = False
                    break
                load = {}
                for (k, x, y, a, b) in placements:
                    load[k] = load.get(k, 0) + 1
                containers.append(load)
                remaining = leftover
            if ok and (best is None or len(containers) < len(best)):
                best = containers
    return best


def _shelf_pack(stack_initial: dict, stack_dims: dict,
                CL: float, CW: float, MWT: float):
    """
    Shelf/row packing: fills each container with rows across the width (each
    footprint turned to its column-efficient orientation), starting a new row
    further along the length as rows fill. Catches "sectioned" fits the lane
    packer misses. Tries a few sort orders and keeps the fewest-container plan.
    """
    from engine.geometry import shelf_layout
    items = []
    for sid, cnt in stack_initial.items():
        L  = float(stack_dims[sid]["Length (MM)"])
        W  = float(stack_dims[sid]["Width (MM)"])
        wt = float(stack_dims[sid]["Weight (Kg)"])
        for _ in range(int(cnt)):
            items.append((L, W, wt, sid,
                          bool(stack_dims[sid].get("Rotatable", True))))
    if not items or len(items) > 1200:
        return None
    best = None
    for mode in ("maxdim", "area", "mindim"):
        remaining = list(items)
        containers, guard, ok = [], 0, True
        while remaining:
            guard += 1
            if guard > len(items) + 5:
                ok = False
                break
            placements, leftover = shelf_layout(remaining, CW, CL, MWT, mode)
            if not placements:
                ok = False
                break
            load = {}
            for (k, x, y, a, b) in placements:
                load[k] = load.get(k, 0) + 1
            containers.append(load)
            remaining = leftover
        if ok and (best is None or len(containers) < len(best)):
            best = containers
    return best


def max_units_in_one_container(rack_row, container):
    """
    CAPACITY ANALYSIS: how many of ONE package fit in a single container?

    Rather than new packing maths (which could disagree with the planner), this
    simply asks the real packer "does quantity N still fit in 1 container?" and
    binary-searches the largest N that does. So the answer automatically obeys
    every existing rule: stacking limits, metal nesting, the 540 kg bearing
    capacity, the container weight limit, orientation choice, and the guarantee
    that the layout is actually drawable inside the walls.

    rack_row  : dict-like with "Length (MM)", "Width (MM)", "Height (MM)",
                "Weight (Kg)", and optionally "Packaging Material"/"Stackability".
    container : { "L", "W", "H", "MAX_WT" }

    Returns
    -------
    (max_units, detail) : detail has per-stack height/footprint info, or
                          (0, {"error": ...}) if a single unit cannot fit.
    """
    import pandas as _pd

    name = str(rack_row.get("Rack / Finished Good", "PKG") or "PKG")
    base = {
        "Rack / Finished Good": name,
        "Length (MM)": float(rack_row.get("Length (MM)", 0) or 0),
        "Width (MM)":  float(rack_row.get("Width (MM)", 0) or 0),
        "Height (MM)": float(rack_row.get("Height (MM)", 0) or 0),
        "Weight (Kg)": float(rack_row.get("Weight (Kg)", 0) or 0),
        "Packaging Material": rack_row.get("Packaging Material", ""),
        "Stackability": rack_row.get("Stackability", "Auto"),
    }

    def fits(n):
        """True if n units still pack into a single container."""
        row = dict(base); row["Quantity"] = int(n)
        try:
            return len(pack_containers_exact(_pd.DataFrame([row]), container)) <= 1
        except Exception:
            return False

    # A single unit must be shippable at all (validates size/weight).
    probs = validate_inputs(_pd.DataFrame([dict(base, Quantity=1)]), container)
    if probs:
        return 0, {"error": probs[0]}
    if not fits(1):
        return 0, {"error": "A single unit does not fit in this container."}

    # Upper bound from the physical limits (volume and weight), then exponential
    # growth + binary search for the exact largest quantity that still fits.
    CL, CW, CH = float(container["L"]), float(container["W"]), float(container["H"])
    MWT = float(container["MAX_WT"])
    vol_cap = (CL * CW * CH) / max(base["Length (MM)"] * base["Width (MM)"]
                                   * base["Height (MM)"], 1.0)
    wt_cap = (MWT / base["Weight (Kg)"]) if base["Weight (Kg)"] > 0 else vol_cap
    hi_limit = max(1, int(min(vol_cap, wt_cap)) + 2)

    lo = 1
    hi = min(2, hi_limit)
    while hi < hi_limit and fits(hi):          # grow until it no longer fits
        lo = hi
        hi = min(hi * 2, hi_limit)
    if fits(hi):
        best = hi
    else:
        while lo + 1 < hi:                      # binary search the boundary
            mid = (lo + hi) // 2
            if fits(mid):
                lo = mid
            else:
                hi = mid
        best = lo

    # Describe the resulting single-container load for the user.
    row = dict(base); row["Quantity"] = int(best)
    df = _pd.DataFrame([row])
    stacks = build_stacks({name: int(best)},
                          {name: base}, CH, MWT)
    per_stack = max((len(s) for s in stacks), default=1)
    detail = {
        "units": best,
        "stacks": len(stacks),
        "per_stack": per_stack,
        "total_weight": best * base["Weight (Kg)"],
        "weight_pct": (100.0 * best * base["Weight (Kg)"] / MWT) if MWT else 0.0,
        "volume_pct": (100.0 * best * base["Length (MM)"] * base["Width (MM)"]
                       * base["Height (MM)"]) / (CL * CW * CH),
        # It is the WEIGHT limit that stops us only if one more unit would
        # actually breach it; otherwise the floor/height space ran out first.
        "limited_by": ("weight"
                       if (base["Weight (Kg)"] > 0
                           and (best + 1) * base["Weight (Kg)"] > MWT)
                       else "space"),
    }
    return best, detail


def _enforce_drawable(plan, stack_dims, CL, CW, MWT):
    """
    GUARANTEE that every container in the plan can actually be ARRANGED inside
    the container walls — using the very same layout finder the report draws
    with. A greedy packer can accept a set of footprints whose total area fits
    while no real arrangement exists; that used to surface as racks drawn
    outside the container in the PDF, which is meaningless on a loading dock.

    Any stacks that cannot be arranged are lifted out and re-packed into extra
    containers (each of which is validated the same way). The container count
    may rise slightly, but every container in the result is genuinely loadable.
    """
    from engine.geometry import find_layout

    def tuples(load):
        out = []
        for sid, cnt in load.items():
            d = stack_dims[sid]
            for i in range(int(cnt)):
                out.append((float(d["Length (MM)"]), float(d["Width (MM)"]),
                            float(d["Weight (Kg)"]), (sid, i),
                            bool(d.get("Rotatable", True))))
        return out

    fixed, spill = [], []
    for load in plan:
        load = {s: int(c) for s, c in load.items() if int(c) > 0}
        if not load:
            continue
        items = tuples(load)
        if find_layout(items, CW, CL) is not None:
            fixed.append(load)
            continue
        # Drop the largest stacks until what remains can be arranged.
        items.sort(key=lambda t: t[0] * t[1], reverse=True)
        removed = []
        while items and find_layout(items, CW, CL) is None:
            removed.append(items.pop(0))
        keep = {}
        for it in items:
            sid = it[3][0]
            keep[sid] = keep.get(sid, 0) + 1
        if keep:
            fixed.append(keep)
        spill.extend(removed)

    # Re-pack whatever spilled into additional validated containers.
    guard = 0
    while spill:
        guard += 1
        if guard > len(spill) + 5:
            break
        cur, rest, wt = [], [], 0.0
        for it in sorted(spill, key=lambda t: t[0] * t[1], reverse=True):
            if wt + it[2] > MWT + 1e-6:
                rest.append(it)
                continue
            trial = cur + [it]
            if find_layout(trial, CW, CL) is not None:
                cur = trial
                wt += it[2]
            else:
                rest.append(it)
        if not cur:                     # a single stack that fits nothing: ship alone
            cur = [spill[0]]
            rest = spill[1:]
        load = {}
        for it in cur:
            load[it[3][0]] = load.get(it[3][0], 0) + 1
        fixed.append(load)
        spill = rest
    return fixed


def _plan_fill_key(plan, stack_dims):
    """
    A comparable 'fill profile' for a plan: the floor area used in each
    container, sorted fullest-first. Used to choose, among plans that use the
    SAME (fewest) number of containers, the one that packs the earliest
    containers as full as possible (so we don't ship a half-empty container 1
    and a half-empty container 2 when one full + one part-full is possible).
    """
    fills = []
    for cont in plan:
        area = 0.0
        for sid in cont:
            d = stack_dims[sid]
            area += float(d["Length (MM)"]) * float(d["Width (MM)"])
        fills.append(area)
    fills.sort(reverse=True)
    return tuple(fills)


class PackingInputError(ValueError):
    """Raised when input data cannot be shipped in the selected container."""


def validate_inputs(df, container):
    """
    Check every rack CAN physically ship in the selected container BEFORE
    packing. Without this, a rack that is too long / too wide / too tall / too
    heavy is silently dropped: the app would report a container count that
    quietly excludes those racks — dangerous when the count drives real
    container orders. Returns a list of human-readable problems (empty if OK).
    """
    CL = float(container["L"]); CW = float(container["W"])
    CH = float(container["H"]); MWT = float(container["MAX_WT"])
    problems = []
    for _i, r in df.iterrows():
        name = str(r.get("Rack / Finished Good", "")).strip()
        if not name:
            continue

        # Packaging Material is MANDATORY and enforced HERE (not only in the
        # UI), because it decides the bearing capacity and the metal-on-
        # corrugate rule. A blank material would silently fall back to generic
        # assumptions and give wrong stacking — so every path into the engine
        # (manual entry, Excel upload, or a future API) fails loudly instead.
        mat = ""
        for _k in ("Packaging Material", "Material", "Packaging", "Pkg Material"):
            if _k in r and r.get(_k) is not None:
                _v = str(r.get(_k)).strip()
                if _v and _v.lower() not in ("nan", "none"):
                    mat = _v
                    break
        if not mat:
            problems.append(
                f"{name}: Packaging Material is required (it sets the stacking "
                f"and weight-bearing rules).")
        try:
            L = float(r.get("Length (MM)", 0) or 0)
            W = float(r.get("Width (MM)", 0) or 0)
            H = float(r.get("Height (MM)", 0) or 0)
            wt = float(r.get("Weight (Kg)", 0) or 0)
            q = float(r.get("Quantity", 0) or 0)
        except (TypeError, ValueError):
            problems.append(f"{name}: dimensions/weight are not valid numbers.")
            continue
        if q < 0:
            problems.append(f"{name}: quantity is negative.")
        if min(L, W, H) <= 0:
            problems.append(f"{name}: length/width/height must all be greater than 0.")
            continue
        if wt < 0:
            problems.append(f"{name}: weight is negative.")
        # footprint must fit the floor in at least one orientation
        fits_flat = ((L <= CL and W <= CW) or (W <= CL and L <= CW))
        if not fits_flat:
            problems.append(
                f"{name}: footprint {L:.0f} x {W:.0f} mm does not fit the container "
                f"floor ({CL:.0f} x {CW:.0f} mm) in either orientation.")
        if H > CH:
            problems.append(
                f"{name}: height {H:.0f} mm exceeds the container height "
                f"({CH:.0f} mm).")
        if wt > MWT:
            problems.append(
                f"{name}: one unit weighs {wt:,.0f} kg, more than the container "
                f"limit ({MWT:,.0f} kg).")
    return problems


def pack_containers_exact(df, container):
    """
    Pack all racks in `df` into the minimum number of containers.

    Racks are first combined into vertical STACKS (see engine/stacking.py:
    same footprint only, heavier at the bottom, capped by the Stackability
    input or a 540 kg bearing capacity for non-metal / height for metal, and
    never a metal base on a corrugate top). Each finished stack is then placed
    on the container floor as one footprint, and the fewest-container search
    runs over those stacks.

    Parameters
    ----------
    df        : pandas DataFrame with columns:
                  "Rack / Finished Good", "Quantity",
                  "Length (MM)", "Width (MM)", "Height (MM)", "Weight (Kg)"
                Optional columns (used for stacking if present):
                  "Packaging Material", "Stackability"
    container : dict  { "L": float, "W": float, "H": float, "MAX_WT": float }

    Returns
    -------
    list of dict  { rack_name: quantity_in_this_container }

    Raises
    ------
    PackingInputError : if any rack cannot physically ship in this container
                        (too long / wide / tall / heavy, or invalid numbers).
                        Failing loudly is deliberate — silently dropping such a
                        rack would understate the containers you need to order.
    """

    problems = validate_inputs(df, container)
    if problems:
        raise PackingInputError(
            "These racks cannot ship in the selected container:\n- "
            + "\n- ".join(problems))

    # Group rows by rack NAME: sum duplicate rows, keep distinct racks separate.
    agg = {
        "Quantity":    "sum",
        "Length (MM)": "first",
        "Width (MM)":  "first",
        "Height (MM)": "first",
        "Weight (Kg)": "first",
    }
    for opt in ("Packaging Material", "Material", "Packaging",
                "Stackability", "Stack", "Max Stack",
                "Loading Access", "Access", "Loading Direction", "Way"):
        if opt in df.columns:
            agg[opt] = "first"

    grouped = df.groupby("Rack / Finished Good", as_index=False).agg(agg)
    grouped["Rack / Finished Good"] = grouped["Rack / Finished Good"].astype(str)

    initial = dict(zip(
        grouped["Rack / Finished Good"],
        grouped["Quantity"].astype(int),
    ))
    dims = grouped.set_index("Rack / Finished Good").to_dict("index")

    CL  = float(container["L"])
    CW  = float(container["W"])
    CH  = float(container["H"])
    MWT = float(container["MAX_WT"])

    # ── 1) Build vertical stacks (all the stacking rules live in stacking.py) ──
    stacks = build_stacks(initial, dims, CH, MWT)
    if not stacks:
        return []

    # ── 2) Represent finished stacks as floor items. IDENTICAL stacks (same
    #       footprint AND same total weight) are grouped into ONE item with a
    #       count, so the packer places thousands of identical stacks in bulk
    #       instead of one at a time. Height is set to the container height so
    #       the floor packer never re-stacks them (they are already stacked). ──
    from collections import defaultdict as _dd
    stack_groups = _dd(list)
    for stk in stacks:
        # LOADING ACCESS: a 2-way package can only be loaded one way round, so
        # store its footprint ALREADY TURNED the right way and remember that it
        # must not be rotated. "length" = package length runs along the
        # container length (L stays along); "width" = the package length runs
        # across the container width, so L/W are swapped here once. 4-way
        # packages stay as entered and may be rotated freely by the layouts.
        acc = stk[0].get("acc", "4way")
        sL, sW = stk[0]["L"], stk[0]["W"]
        if acc == "width":
            sL, sW = sW, sL
        rotatable = (acc == "4way")
        key = (round(sL, 2), round(sW, 2),
               round(sum(b["wt"] for b in stk), 2), rotatable)
        stack_groups[key].append(stk)

    stack_dims    = {}
    stack_pool    = {}     # sid -> list of the actual stacks in this group
    stack_initial = {}
    for j, (key, stklist) in enumerate(stack_groups.items()):
        sid = f"__g{j}"
        L, W, wt, rotatable = key
        stack_dims[sid] = {"Length (MM)": L, "Width (MM)": W,
                           "Height (MM)": CH, "Weight (Kg)": wt,
                           "Rotatable": rotatable}
        stack_pool[sid]    = stklist
        stack_initial[sid] = len(stklist)

    # ── 3) Try SEVERAL complete packing methods and keep the plan that uses
    #       the fewest containers. Each method is a different way of filling
    #       the container; taking the best means we never do worse than any
    #       single method, and we catch wins that one method alone would miss:
    #         (a) single-footprint mixed-orientation column optimum
    #         (b) lane bin-packing (mixes lengths in each lane; uniform width)
    #         (c) general greedy column packing (mixes footprints/orientations)
    #         (d) the multi-strategy greedy search (Pass 1/2/3) + merge
    # ──────────────────────────────────────────────────────────────────────────
    sids = list(stack_initial.keys())
    best_result = None
    best_key    = None            # (num_containers, -fill_c1, -fill_c2, ...) minimise

    def _consider(plan):
        nonlocal best_result, best_key
        plan = [c for c in plan if c]
        if not plan:
            return
        fk  = _plan_fill_key(plan, stack_dims)          # fullest first
        key = (len(plan),) + tuple(-x for x in fk)      # fewer conts, then fuller c1...
        if best_key is None or key < best_key:
            best_key    = key
            best_result = plan

    for method in (_single_footprint_pack, _lane_pack, _column_pack,
                   _lane_mixed_pack, _shelf_pack):
        try:
            cand = method(stack_initial, stack_dims, CL, CW, MWT)
        except Exception:
            cand = None
        if cand:
            _consider(_merge([dict(c) for c in cand if c],
                             stack_dims, CL, CW, CH, MWT, sids))

    # Run the accurate exhaustive search, bounded by a TIME BUDGET so a huge
    # job never runs longer than the operator will wait. The single best-sorted
    # (longest-first) ordering is the near-optimum for large jobs — extra
    # orderings don't improve it — so the budget just needs to be large enough
    # for that ONE accurate pass to finish. If it can't finish in the budget the
    # fast column/lane result (already found above) is used instead.
    #   Raise PACK_TIME_BUDGET_S to allow bigger jobs the accurate (fewer-
    #   container) count; lower it if you need a quicker, rougher answer.
    PACK_TIME_BUDGET_S = 300.0
    deadline = time.time() + PACK_TIME_BUDGET_S

    orderings = _strategies(sids, stack_dims)
    n_stacks = len(sids)
    if n_stacks > 150:
        orderings = orderings[:1]
    elif n_stacks > 60:
        orderings = orderings[:3]

    for order in orderings:
        if time.time() > deadline:
            break
        result = _greedy_run(stack_initial, stack_dims, CL, CW, CH, MWT,
                             order, deadline=deadline)
        if result is None:               # ran out of time -> keep fast result
            break
        result = _merge(result, stack_dims, CL, CW, CH, MWT, order)
        _consider(result)
        if best_key is not None and best_key[0] == 1:
            break

    # ── 3b) GUARANTEE the chosen plan is physically arrangeable: every
    #        container must pass the same layout finder the report draws with,
    #        so no rack can ever be drawn outside the container. ─────────────
    if best_result:
        best_result = _enforce_drawable(best_result, stack_dims, CL, CW, MWT)

    # ── 4) Map the stacks in each container back to real rack quantities.
    #       Pull `q` actual stacks from each group's pool (they are identical). ──
    containers = []
    for cont in (best_result or []):
        names = {}
        for sid, q in cont.items():
            pool = stack_pool.get(sid, [])
            for _ in range(int(q)):
                if not pool:
                    break
                stk = pool.pop()
                for b in stk:
                    names[b["name"]] = names.get(b["name"], 0) + 1
        containers.append(names)

    # ── 5) SAFETY NET: every unit that went in must come out. A silent loss
    #       here would understate the container count, so fail loudly instead. ─
    want = {}
    for nm, q in initial.items():
        want[str(nm)] = want.get(str(nm), 0) + int(q)
    got = {}
    for c in containers:
        for nm, q in c.items():
            got[str(nm)] = got.get(str(nm), 0) + int(q)
    missing = {k: want[k] - got.get(k, 0) for k in want if want[k] != got.get(k, 0)}
    if missing:
        raise PackingInputError(
            "Internal packing error: these racks were not fully placed "
            f"({missing}). Please report this input.")

    return containers