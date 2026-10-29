"""Small helpers shared by the engine modules."""
from .lifecycle import ACTIVE  # noqa: F401  (re-exported: other modules import ACTIVE from here)


def overlaps(a, b) -> bool:
    lo_a, hi_a = sorted((a.proximal, a.distal))
    lo_b, hi_b = sorted((b.proximal, b.distal))
    return lo_a <= hi_b and lo_b <= hi_a
