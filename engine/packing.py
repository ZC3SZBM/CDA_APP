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
import itertools


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
                rack_order: list) -> list:
    remaining = dict(initial)
    containers = []
    while any(v > 0 for v in remaining.values()):
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

    # Segregation: each rack type first, rest sorted by length desc
    segregations = []
    for r in racks:
        others = sorted([x for x in racks if x != r],
                        key=lambda x: dims[x]["Length (MM)"], reverse=True)
        segregations.append([r] + others)
        segregations.append(others + [r])

    return base + segregations


# ══════════════════════════════════════════════════════════════════════════════
#  Public entry point
# ══════════════════════════════════════════════════════════════════════════════

def pack_containers_exact(df, container):
    """
    Pack all racks in `df` into the minimum number of containers.

    Parameters
    ----------
    df        : pandas DataFrame with columns:
                  "Rack / Finished Good", "Quantity",
                  "Length (MM)", "Width (MM)", "Height (MM)", "Weight (Kg)"
    container : dict  { "L": float, "W": float, "H": float, "MAX_WT": float }
                Dimensions in mm, weight in kg.

    Returns
    -------
    list of dict  { rack_name: quantity_in_this_container }
    """

    # Group rows by rack NAME: sum duplicate rows, but keep DISTINCT racks
    # separate even when their dimensions happen to be identical. (Grouping by
    # dimensions and joining names with "|" produced names like "A|B" that the
    # results view and report could not find in the original data -> crash.)
    grouped = (
        df.groupby("Rack / Finished Good", as_index=False)
          .agg({
              "Quantity":    "sum",
              "Length (MM)": "first",
              "Width (MM)":  "first",
              "Height (MM)": "first",
              "Weight (Kg)": "first",
          })
    )
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

    racks = [r for r in initial if initial[r] > 0]

    best_result = None
    best_count  = float("inf")

    for order in _strategies(racks, dims):
        result = _greedy_run(initial, dims, CL, CW, CH, MWT, order)
        result = _merge(result, dims, CL, CW, CH, MWT, order)
        result = [c for c in result if c]
        if len(result) < best_count:
            best_count  = len(result)
            best_result = result
            if best_count == 1:
                break

    return best_result or []