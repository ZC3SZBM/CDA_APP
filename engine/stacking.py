# engine/stacking.py
# ─────────────────────────────────────────────────────────────────────────────
# Stack-building rules (shared by the packer AND the report so both agree).
#
#   1. Only boxes with the SAME FOOTPRINT (Length & Width match) may be stacked
#      on each other. Height may differ.
#   2. HEAVIER boxes go at the bottom; a box may only carry lighter-or-equal
#      boxes above it.
#   3. Max boxes in a stack:
#        - if a "Stackability" number is given for the box -> use it.
#        - else, by packaging material:
#            * non-metal -> limited by a 540 kg bearing capacity
#              (weight resting on any box must stay <= 540 kg).
#            * metal      -> not weight-limited; limited only by the container
#              height (and/or its Stackability).
#   4. EXCEPTION: a metal-pallet (metal-based) box may not sit on a
#      corrugate-topped box (the metal base would crush the corrugate).
#      Everything else may stack.
#   5. Total stack height must fit inside the container height.
#   6. A single stack's total weight must fit inside the container weight limit.
#
# The input rows may carry two optional columns:
#     "Stackability"        integer >= 1  (blank/0 = auto)
#     "Packaging Material"  text, e.g. "Metal", "Corrugate",
#                           "Metal Pallet + Corrugate", "Wood"
# If those columns are absent the sensible defaults above apply (540 kg,
# height-based), so existing inputs keep working unchanged.
# ─────────────────────────────────────────────────────────────────────────────

from collections import defaultdict

DEFAULT_CAPACITY_KG = 540.0     # bearing capacity for NON-metal packaging
METAL_NEST_MM = 50.8            # 2.0 inches: a metal rack nests this deep into the metal rack below
_EPS = 1e-6


def _f(row, *keys, default=0.0):
    for k in keys:
        if k in row and row[k] is not None:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                pass
    return default


def material_of(row) -> str:
    for k in ("Packaging Material", "Material", "Packaging", "Pkg Material"):
        if k in row and row[k] is not None:
            return str(row[k]).strip().lower()
    return ""


def is_metal_base(mat: str) -> bool:
    """Box has a metal base/pallet (high bearing capacity; can crush corrugate)."""
    return "metal" in mat


def is_corrugate_top(mat: str) -> bool:
    """Box has a corrugate top surface (can be crushed by a metal base)."""
    return "corrugate" in mat or "carton" in mat or "cardboard" in mat


def is_metal_rack(mat: str) -> bool:
    """
    A pure metal rack (metal, no corrugate). These NEST when stacked: a metal
    rack set on another metal rack drops METAL_NEST_MM into it.
    """
    return is_metal_base(mat) and not is_corrugate_top(mat)


def stack_height_mm(units) -> float:
    """
    Physical height of a bottom->top stack, accounting for metal-rack nesting.

    When a metal rack sits on another metal rack it drops METAL_NEST_MM
    (38.1 mm) into the one below, so every metal-on-metal joint shortens the
    stack by that much. Non-metal packaging (corrugate, wood, plastic) does not
    nest and keeps its full height. This is the height used to verify a stack
    fits inside the container.
    """
    if not units:
        return 0.0
    total = sum(float(u["H"]) for u in units)
    for lower, upper in zip(units, units[1:]):
        if is_metal_rack(lower.get("mat", "")) and is_metal_rack(upper.get("mat", "")):
            total -= METAL_NEST_MM
    return max(total, 0.0)


def capacity_kg(row) -> float:
    """
    Bearing capacity of a box (weight it can carry on top).
      * if the user gave an explicit Stackability -> trust it (no weight cap).
      * pure metal top (metal, no corrugate)      -> not weight-limited.
      * everything else (corrugate / wood / plastic / metal-pallet+corrugate)
        -> 540 kg default.
    """
    if stackability_of(row) is not None:
        return float("inf")
    mat = material_of(row)
    if is_metal_base(mat) and not is_corrugate_top(mat):
        return float("inf")          # e.g. "Metal Rack": strong metal top
    return DEFAULT_CAPACITY_KG


def stackability_of(row):
    """
    Explicit max boxes per stack, or None for 'auto' (use capacity rule).

    Accepts the dropdown values used in the app:
        "Non Stackable"      -> 1   (box must stand alone)
        "G+1" ... "G+20"     -> 2 ... 21   (Ground + N = N+1 boxes total)
    as well as a plain integer, or blank (-> None).
    """
    for k in ("Stackability", "Stack", "Stackable", "Max Stack"):
        if k in row and row[k] is not None:
            s = str(row[k]).strip()
            if s == "" or s.lower() == "nan":
                return None
            low = s.lower().replace(" ", "")
            if low in ("nonstackable", "nonstack", "no", "1", "g+0", "ground"):
                return 1
            if low.startswith("g+"):
                try:
                    return int(low[2:]) + 1        # Ground + N -> N+1 boxes
                except ValueError:
                    return None
            try:
                v = int(round(float(s)))
                return v if v > 0 else None
            except (TypeError, ValueError):
                return None
    return None


def can_place_on(upper_row, lower_row) -> bool:
    """
    May the `upper` box sit directly on the `lower` box?
    Forbidden only when a metal base rests on a corrugate top.
    """
    if is_metal_base(material_of(upper_row)) and is_corrugate_top(material_of(lower_row)):
        return False
    return True


def _unit(name, row):
    return {
        "name": name,
        "L":   _f(row, "Length (MM)"),
        "W":   _f(row, "Width (MM)"),
        "H":   _f(row, "Height (MM)"),
        "wt":  _f(row, "Weight (Kg)"),
        "mat": material_of(row),
        "stk": stackability_of(row),
        "cap": capacity_kg(row),
    }


def _can_add(stack, u, CH, MWT):
    """Can unit `u` be added on TOP of `stack` (bottom->top list)?"""
    top = stack[-1]

    # (2) heavier at the bottom: the new (upper) box must be <= the one below
    if u["wt"] > top["wt"] + _EPS:
        return False

    # (4) material exception: no metal base on a corrugate top
    if is_metal_base(u["mat"]) and is_corrugate_top(top["mat"]):
        return False

    new = stack + [u]

    # (5) total height must fit the container.
    #     Stackability (rule 3a below) is honoured first; this height check
    #     then uses the NESTED height (metal racks drop 38.1 mm into each other).
    if stack_height_mm(new) > CH + _EPS:
        return False

    # (6) whole stack must fit the container weight limit
    if sum(b["wt"] for b in new) > MWT + _EPS:
        return False

    # (3a) explicit stackability caps the box count (min among members that set it)
    stks = [b["stk"] for b in new if b["stk"]]
    if stks and len(new) > min(stks):
        return False

    # (3b) bearing capacity: weight resting on each box must not exceed its cap
    for i, b in enumerate(new):
        weight_above = sum(x["wt"] for x in new[i + 1:])
        if weight_above > b["cap"] + _EPS:
            return False

    return True


def _units_identical(units) -> bool:
    """True if every unit in the list is the same rack (same dims/wt/mat/stk)."""
    if len(units) <= 1:
        return True
    a = units[0]
    ka = (a["name"], round(a["L"], 3), round(a["W"], 3), round(a["H"], 3),
          round(a["wt"], 3), a["mat"], a["stk"])
    for u in units[1:]:
        if (u["name"], round(u["L"], 3), round(u["W"], 3), round(u["H"], 3),
                round(u["wt"], 3), u["mat"], u["stk"]) != ka:
            return False
    return True


def _max_identical_stack(u, CH, MWT) -> int:
    """How many identical units `u` fit in one stack (height/capacity/stackability)."""
    stack = [dict(u)]
    while _can_add(stack, dict(u), CH, MWT):
        stack.append(dict(u))
        if len(stack) > 100000:          # safety guard
            break
    return len(stack)


def build_stacks(load: dict, dims: dict, CH: float, MWT: float) -> list:
    """
    Turn a set of boxes into vertical stacks following all the rules above.

    Parameters
    ----------
    load : { rack_name: quantity }
    dims : { rack_name: {"Length (MM)":…, "Width (MM)":…, "Height (MM)":…,
                         "Weight (Kg)":…, optional "Packaging Material",
                         optional "Stackability"} }
    CH   : container height (mm)
    MWT  : container max weight (kg)

    Returns
    -------
    list of stacks. Each stack is a list of unit dicts, BOTTOM first, TOP last.
    Each unit dict has: name, L, W, H, wt, mat, stk, cap.
    """
    # expand to individual units
    units = []
    for name, qty in load.items():
        q = int(round(float(qty)))
        if q <= 0 or name not in dims:
            continue
        base = _unit(name, dims[name])
        for _ in range(q):
            units.append(dict(base))

    # group by footprint (Length, Width) — only same-footprint boxes may stack
    groups = defaultdict(list)
    for u in units:
        groups[(round(u["L"], 1), round(u["W"], 1))].append(u)

    stacks = []
    for _key, gunits in groups.items():
        # Split this footprint group by identical rack identity. Each identity
        # (the common large-quantity case) is chunked into full same-rack
        # stacks in bulk; only the small leftover partials from different racks
        # are then first-fitted together (this preserves mixed stacks for
        # small quantities while staying fast for huge ones).
        by_id = defaultdict(list)
        for u in gunits:
            idk = (u["name"], round(u["wt"], 3), u["mat"], u["stk"], round(u["H"], 3))
            by_id[idk].append(u)

        leftovers = []
        for _idk, ulist in by_id.items():
            per = max(1, _max_identical_stack(ulist[0], CH, MWT))
            n_full = len(ulist) // per
            for i in range(n_full):
                stacks.append(ulist[i * per:(i + 1) * per])
            rem = ulist[n_full * per:]
            if rem:
                leftovers.append(rem)

        # Merge the leftover partial stacks (different racks, same footprint).
        left_units = [u for part in leftovers for u in part]
        left_units.sort(key=lambda u: u["wt"], reverse=True)   # heavier at base
        open_stacks = []
        for u in left_units:
            placed = False
            for stk in open_stacks:
                if _can_add(stk, u, CH, MWT):
                    stk.append(u)
                    placed = True
                    break
            if not placed:
                open_stacks.append([u])
        stacks.extend(open_stacks)

    return stacks


def stack_footprint(stack):
    """(L, W, total_height, total_weight) for a built stack (nested height)."""
    L = stack[0]["L"]
    W = stack[0]["W"]
    return L, W, stack_height_mm(stack), sum(b["wt"] for b in stack)
