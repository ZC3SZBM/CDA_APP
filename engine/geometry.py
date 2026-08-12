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

    def can_place(self, item_W: float, item_L: float) -> bool:
        """True if (item_W × item_L) or its 90° rotation fits anywhere."""
        for fr in self.free:
            if fr.fits(item_W, item_L) or fr.fits(item_L, item_W):
                return True
        return False

    def place(self, item_W: float, item_L: float) -> bool:
        """
        Place one item of (item_W × item_L), 90° rotation allowed.

        item_W goes along the container WIDTH  axis.
        item_L goes along the container LENGTH axis.

        Returns True on success, False if no free rect can accommodate it.
        """
        best_fr  = None
        best_sc  = None
        best_iW  = item_W   # chosen width  dimension of placed item
        best_iL  = item_L   # chosen length dimension of placed item

        for fr in self.free:
            # Try both orientations: normal and 90°-rotated
            for iW, iL in ((item_W, item_L), (item_L, item_W)):
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
