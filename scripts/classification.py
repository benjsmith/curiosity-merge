"""Classification labels shared with a mounted graph.

public < internal < confidential < restricted.

A missing label is public. A label this module does not know is treated
as restricted, so an unknown tag cannot sneak out under a lower ceiling.
"""

from __future__ import annotations

_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def rank(label: str | None) -> int:
    key = (label or "public").strip().lower()
    if key not in _RANK:
        return _RANK["restricted"]
    return _RANK[key]


def above(label: str | None, ceiling: str) -> bool:
    return rank(label) > rank(ceiling)


def highest(labels: list[str | None]) -> str:
    best = "public"
    for label in labels:
        key = (label or "public").strip().lower()
        if key not in _RANK:
            key = "restricted"
        if rank(key) > rank(best):
            best = key
    return best
