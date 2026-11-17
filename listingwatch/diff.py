"""Deterministic diff between two consecutive snapshots of one listing."""
from __future__ import annotations

import difflib
import re

from .fetch import hamming

SHORT_FIELDS = ["title", "subtitle", "developer", "version", "price", "currency", "has_iap"]
LONG_FIELDS = ["description", "release_notes"]
SAME_IMAGE_MAX_DISTANCE = 6   # dHash bits; re-encoded or re-uploaded art stays well under this
RATING_EPSILON = 0.01

_WORD = re.compile(r"[^\W\d_][\w'’-]{2,}", re.UNICODE)
_STOP = set("the and for you your with are this that from into our all can will its it's not get has have more new".split())


def words(text: str | None) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")} - _STOP


def _word_shift(before: str | None, after: str | None) -> dict:
    a, b = words(before), words(after)
    return {"added_words": sorted(b - a)[:40], "removed_words": sorted(a - b)[:40]}


def _match_screens(before: list[dict], after: list[dict]) -> list[int | None]:
    """For each `after` screenshot, the index of the same image in `before` (or None)."""
    used, out = set(), []
    for shot in after:
        found = None
        for i, old in enumerate(before):
            if i in used:
                continue
            if old["url"] == shot["url"] or (
                old.get("hash") and shot.get("hash") and hamming(old["hash"], shot["hash"]) <= SAME_IMAGE_MAX_DISTANCE
            ):
                found = i
                break
        if found is not None:
            used.add(found)
        out.append(found)
    return out


def diff_snapshots(prev: dict, cur: dict) -> list[dict]:
    changes = []
    for field in SHORT_FIELDS:
        if prev.get(field) != cur.get(field):
            change = {"field": field, "before": prev.get(field), "after": cur.get(field)}
            if field in ("title", "subtitle"):
                change.update(_word_shift(prev.get(field), cur.get(field)))
            changes.append(change)

    for field in LONG_FIELDS:
        a, b = prev.get(field) or "", cur.get(field) or ""
        if a.strip() != b.strip():
            lines = list(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0))[2:]
            changes.append({
                "field": field,
                "diff": [l for l in lines if not l.startswith("@@")][:60],
                "chars_before": len(a), "chars_after": len(b),
                **_word_shift(a, b),
            })

    before, after = prev.get("screenshots") or [], cur.get("screenshots") or []
    matches = _match_screens(before, after)
    added = [after[i]["url"] for i, m in enumerate(matches) if m is None]
    removed = [before[i]["url"] for i in range(len(before)) if i not in set(matches)]
    kept_order = [m for m in matches if m is not None]
    reordered = kept_order != sorted(kept_order)
    if added or removed or reordered:
        changes.append({
            "field": "screenshots", "added": added, "removed": removed, "reordered": reordered,
            "count_before": len(before), "count_after": len(after),
        })

    ra, rb = prev.get("rating"), cur.get("rating")
    if ra is not None and rb is not None and abs(rb - ra) >= RATING_EPSILON:
        changes.append({"field": "rating", "before": round(ra, 3), "after": round(rb, 3), "delta": round(rb - ra, 3)})
    ca, cb = prev.get("rating_count"), cur.get("rating_count")
    if ca is not None and cb is not None and ca != cb:
        changes.append({"field": "rating_count", "before": ca, "after": cb, "delta": cb - ca})
    return changes


def is_material(changes: list[dict]) -> bool:
    """Anything beyond rating drift is worth an interpretation note."""
    return any(c["field"] not in ("rating", "rating_count") for c in changes)
