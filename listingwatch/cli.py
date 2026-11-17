"""Command line: run (cron), import-play, archive, notes, timeline, diff."""
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


def load_watchlist(path: str) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """Return ({country: [app_store_ids]}, [(play_package, country)]) from the watchlist file."""
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    by_country: dict[str, list[str]] = {}
    play: list[tuple[str, str]] = []
    for app in cfg["apps"]:
        for country in app.get("countries") or cfg.get("countries") or ["us"]:
            if app.get("appstore"):
                ids = by_country.setdefault(country.lower(), [])
                if str(app["appstore"]) not in ids:
                    ids.append(str(app["appstore"]))
            if app.get("play") and (app["play"], country.lower()) not in play:
                play.append((app["play"], country.lower()))
    return by_country, play


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
    appstore, play = load_watchlist(args.watchlist)
    for country, ids in appstore.items():
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
    if not args.no_play:
        play_limiter = fetch.RateLimiter(args.play_interval)
        for package, country in play:
            try:
                snap = fetch.parse_play(fetch.fetch_play(package, country, play_limiter), package, country)
            except Exception as exc:
                print(f"play fetch failed for {package} {country}: {exc}", file=sys.stderr)
                failures += 1
                continue
            snap["taken_at"] = taken_at
            if not args.no_hash:
                got, bad = hash_screenshots(con, snap, images)
                stats["images_downloaded"] += got
                stats["image_failures"] += bad
            db.save_snapshot(con, snap)
            stats["snapshots"] += 1
            print(f"snapshot play {country} {package} {snap['title']}")
    if not args.no_notes:
        stats["notes"] = write_notes(con)
        stats["note_models"] = llm.models() if llm.provider() != "none" else None
        stats["llm_usage"] = dict(llm.usage)
    if args.html:
        render_all(con, args.html)
    stats.update(fetch_failures=failures, seconds=round(time.monotonic() - started, 1))
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
            prompt = llm.build_prompt(cur["title"], store, country, changes)
            key = llm.cache_key(prompt)
            result = db.cached_completion(con, key)
            if result is None:
                try:
                    result = llm.complete(prompt)
                except Exception as exc:
                    print(f"note skipped for {cur['title']} {country}: {exc}", file=sys.stderr)
                    continue
                if result is None:
                    continue
                db.store_completion(con, key, *result)
            text, model = result
            db.put_note(con, prev["id"], cur["id"], model, _THINK.sub("", text).strip(), now_utc())
            written += 1
            print(f"note {cur['title']} {country} ({prev['taken_at']} -> {cur['taken_at']}) by {model}")
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


ARCHIVE_STORE = "appstore-archive"


def cmd_archive(args) -> int:
    """Pair the oldest usable Internet Archive copy of each App Store page with the live page."""
    con = db.connect(args.db)
    archive, apple, images = (fetch.RateLimiter(args.archive_interval), fetch.RateLimiter(args.lookup_interval),
                              fetch.RateLimiter(args.image_interval))
    failures = 0
    for app_id in args.app_ids:
        latest = db.history(con, "appstore", app_id, args.country)
        if not latest:
            print(f"no Lookup snapshot for {app_id} {args.country}; run `run` first", file=sys.stderr)
            failures += 1
            continue
        page_url = latest[-1]["url"].split("?")[0]
        archived = None
        try:
            captures = fetch.wayback_captures(page_url, archive, since=args.since)
        except Exception as exc:
            print(f"capture index failed for {app_id}: {exc}", file=sys.stderr)
            failures += 1
            continue
        for stamp, original in captures[:args.tries]:
            try:
                archived = fetch.parse_appstore_page(fetch.fetch_wayback(stamp, original, archive), app_id, args.country)
            except Exception as exc:
                print(f"  capture {stamp} unusable: {exc}", file=sys.stderr)
                continue
            if not (archived.get("language") or "").lower().startswith(args.language):
                print(f"  capture {stamp} is in {archived.get('language')}, not {args.language}", file=sys.stderr)
                archived = None
                continue
            archived["taken_at"] = dt.datetime.strptime(stamp, "%Y%m%d%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ")
            archived["url"] = f"https://web.archive.org/web/{stamp}/{original}"
            break
        if archived is None:
            print(f"no usable archived capture for {app_id}", file=sys.stderr)
            failures += 1
            continue
        live = fetch.parse_appstore_page(fetch.fetch_appstore_page(page_url, apple), app_id, args.country)
        live.update(taken_at=now_utc(), url=page_url)
        for snap in (archived, live):
            snap["store"] = ARCHIVE_STORE
            hash_screenshots(con, snap, images)
            db.save_snapshot(con, snap)
        print(f"archive pair {app_id} {live['title']}: {archived['taken_at']} -> {live['taken_at']}")
    return 1 if failures else 0


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
    r.add_argument("--no-play", action="store_true", help="skip Google Play pages")
    r.add_argument("--play-interval", type=float, default=5.0, help="seconds between Play page requests")
    r.add_argument("--image-interval", type=float, default=0.5, help="seconds between image downloads")
    r.set_defaults(func=cmd_run)

    i = sub.add_parser("import-play", help="import manually captured Google Play snapshot(s) from JSON")
    i.add_argument("file")
    i.set_defaults(func=cmd_import_play)

    a = sub.add_parser("archive", help="diff an Internet Archive copy of App Store pages against the live page")
    a.add_argument("app_ids", nargs="+")
    a.add_argument("--country", default="us")
    a.add_argument("--language", default="en", help="page language a capture must have (captures vary by locale)")
    a.add_argument("--since", default="20251001", help="oldest capture date to consider (YYYYMMDD)")
    a.add_argument("--tries", type=int, default=6, help="captures to try per app before giving up")
    a.add_argument("--archive-interval", type=float, default=5.0, help="seconds between Internet Archive requests")
    a.add_argument("--lookup-interval", type=float, default=3.0, help="seconds between apps.apple.com requests")
    a.add_argument("--image-interval", type=float, default=0.5)
    a.set_defaults(func=cmd_archive)

    n = sub.add_parser("notes", help="write missing LLM notes")
    n.add_argument("--redo", action="store_true")
    n.set_defaults(func=cmd_notes)

    t = sub.add_parser("timeline", help="render the static HTML timeline")
    t.add_argument("--out", default="timeline")
    t.set_defaults(func=cmd_timeline)

    d = sub.add_parser("diff", help="print the latest diff for one listing as JSON")
    d.add_argument("store", choices=["appstore", "googleplay", ARCHIVE_STORE])
    d.add_argument("app_id")
    d.add_argument("country")
    d.set_defaults(func=cmd_diff)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
