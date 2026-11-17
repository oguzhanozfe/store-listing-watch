"""Command line: run (cron), import-play, notes, timeline, diff."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time

from . import db, fetch, llm
from .diff import diff_snapshots, is_material
from .html import render_all


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_watchlist(path: str) -> dict[str, list[str]]:
    """Return {country: [app_store_ids]} from the watchlist file."""
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    by_country: dict[str, list[str]] = {}
    for app in cfg["apps"]:
        if not app.get("appstore"):
            continue
        for country in app.get("countries") or cfg.get("countries") or ["us"]:
            ids = by_country.setdefault(country.lower(), [])
            if str(app["appstore"]) not in ids:
                ids.append(str(app["appstore"]))
    return by_country


def hash_screenshots(con, snap: dict, limiter: fetch.RateLimiter, log=print) -> tuple[int, int]:
    """Fill in screenshot hashes, downloading only URLs not seen before. Returns (downloaded, failed)."""
    got = bad = 0
    for shot in snap["screenshots"]:
        shot["hash"] = db.cached_hash(con, shot["url"])
        if shot["hash"]:
            continue
        try:
            shot["hash"] = fetch.screenshot_hash(shot["url"], limiter)
            db.store_hash(con, shot["url"], shot["hash"])
            got += 1
        except Exception as exc:  # one bad image must not lose the snapshot
            bad += 1
            log(f"  screenshot hash failed ({exc}): {shot['url']}")
    return got, bad


def cmd_run(args) -> int:
    con = db.connect(args.db)
    api = fetch.RateLimiter(args.lookup_interval)
    images = fetch.RateLimiter(args.image_interval)
    taken_at, failures, started = now_utc(), 0, time.monotonic()
    stats = {"taken_at": taken_at, "snapshots": 0, "images_downloaded": 0, "image_failures": 0, "notes": 0}
    for country, ids in load_watchlist(args.watchlist).items():
        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            try:
                data = fetch.lookup(batch, country, api)
            except Exception as exc:
                print(f"lookup failed for {country}: {exc}", file=sys.stderr)
                failures += 1
                continue
            found = set()
            for result in data.get("results", []):
                snap = fetch.normalize_appstore(result, country)
                snap["taken_at"] = taken_at
                found.add(snap["app_id"])
                if not args.no_hash:
                    got, bad = hash_screenshots(con, snap, images)
                    stats["images_downloaded"] += got
                    stats["image_failures"] += bad
                db.save_snapshot(con, snap)
                stats["snapshots"] += 1
                print(f"snapshot {country} {snap['app_id']} {snap['title']} v{snap['version']}")
            for missing in set(batch) - found:
                print(f"not found in {country} storefront: {missing}", file=sys.stderr)
    if not args.no_notes:
        stats["notes"] = write_notes(con)
        stats["note_model"] = llm.model_id() if llm.provider() != "none" else None
        stats["llm_usage"] = dict(llm.usage)
    if args.html:
        render_all(con, args.html)
    stats.update(lookup_failures=failures, seconds=round(time.monotonic() - started, 1))
    if args.log:
        with open(args.log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(stats) + "\n")
    return 1 if failures else 0


_THINK = re.compile(r"<think>.*?</think>", re.S)


def write_notes(con, redo: bool = False) -> int:
    if llm.provider() == "none":
        return 0
    written = 0
    for store, app_id, country in db.tracked(con):
        snaps = db.history(con, store, app_id, country)
        for prev, cur in zip(snaps, snaps[1:]):
            if not redo and db.get_note(con, prev["id"], cur["id"]):
                continue
            changes = diff_snapshots(prev, cur)
            if not is_material(changes):
                continue
            try:
                text = llm.complete(llm.build_prompt(cur["title"], store, country, changes))
            except Exception as exc:
                print(f"note skipped for {cur['title']} {country}: {exc}", file=sys.stderr)
                continue
            if text:
                db.put_note(con, prev["id"], cur["id"], llm.model_id(),
                            _THINK.sub("", text).strip(), now_utc())
                written += 1
                print(f"note {cur['title']} {country} ({prev['taken_at']} -> {cur['taken_at']})")
    return written


PLAY_KEYS = {"short_description": "subtitle", "recent_changes": "release_notes"}


def play_snapshot(raw: dict) -> dict:
    """Normalize one manually captured Google Play listing (see README for the format)."""
    snap = {PLAY_KEYS.get(k, k): v for k, v in raw.items()}
    snap["store"] = "googleplay"
    snap["app_id"] = snap.pop("package", None) or snap.get("app_id")
    if not snap["app_id"]:
        raise ValueError("Play snapshot needs 'package'")
    snap["country"] = (snap.get("country") or "us").lower()
    snap["taken_at"] = snap.get("taken_at") or now_utc()
    snap["screenshots"] = [s if isinstance(s, dict) else {"url": s, "hash": None} for s in snap.get("screenshots") or []]
    return snap


def cmd_import_play(args) -> int:
    con = db.connect(args.db)
    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)
    for raw in data if isinstance(data, list) else [data]:
        snap = play_snapshot(raw)
        db.save_snapshot(con, snap)
        print(f"imported googleplay {snap['country']} {snap['app_id']} @ {snap['taken_at']}")
    return 0


def cmd_notes(args) -> int:
    print(f"{write_notes(db.connect(args.db), redo=args.redo)} note(s) written")
    return 0


def cmd_timeline(args) -> int:
    files = render_all(db.connect(args.db), args.out)
    print(f"wrote {len(files)} files to {args.out}")
    return 0


def cmd_diff(args) -> int:
    snaps = db.history(db.connect(args.db), args.store, args.app_id, args.country)
    if len(snaps) < 2:
        print("need at least two snapshots", file=sys.stderr)
        return 1
    print(json.dumps(diff_snapshots(snaps[-2], snaps[-1]), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="listingwatch", description=__doc__)
    p.add_argument("--db", default="data/listings.sqlite")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="snapshot every watchlist listing (meant for cron)")
    r.add_argument("--watchlist", default="watchlist.json")
    r.add_argument("--html", help="also render the timeline into this folder")
    r.add_argument("--log", default="data/runs.jsonl", help="append a JSON summary line per run ('' to disable)")
    r.add_argument("--no-notes", action="store_true", help="skip LLM notes")
    r.add_argument("--no-hash", action="store_true", help="skip screenshot downloads")
    r.add_argument("--lookup-interval", type=float, default=3.0, help="seconds between Lookup calls")
    r.add_argument("--image-interval", type=float, default=0.5, help="seconds between image downloads")
    r.set_defaults(func=cmd_run)

    i = sub.add_parser("import-play", help="import manually captured Google Play snapshot(s) from JSON")
    i.add_argument("file")
    i.set_defaults(func=cmd_import_play)

    n = sub.add_parser("notes", help="write missing LLM notes")
    n.add_argument("--redo", action="store_true")
    n.set_defaults(func=cmd_notes)

    t = sub.add_parser("timeline", help="render the static HTML timeline")
    t.add_argument("--out", default="timeline")
    t.set_defaults(func=cmd_timeline)

    d = sub.add_parser("diff", help="print the latest diff for one listing as JSON")
    d.add_argument("store", choices=["appstore", "googleplay"])
    d.add_argument("app_id")
    d.add_argument("country")
    d.set_defaults(func=cmd_diff)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
