"""Build examples/demo-timeline: ONE real snapshot plus ONE hand-edited copy.

The real watchlist showed no listing changes between its first two runs, so this demo exists only to show
what a change looks like in the timeline. The "after" snapshot is simulated: its release notes, version,
rating count and one screenshot are edited by this script. Every page carries a banner saying so.

    python examples/make_demo.py      # needs the committed data/listings.sqlite; calls the note model once
"""
import copy
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from listingwatch import cli, db, fetch  # noqa: E402
from listingwatch.html import render_all  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BANNER = ("SIMULATED DEMO. The first snapshot is real (Apple Lookup API). The second is a hand-edited copy "
          "made by examples/make_demo.py: the event text, version, rating count and one screenshot are invented.")
SIMULATED_NOTES = (
    "Winter is coming to Royal Match!\n\n"
    "• NEW EVENT: Frost Festival! Collect snowflakes in every level to win exclusive rewards.\n"
    "• Limited-time Winter Pass with double coins on all purchases.\n"
    "• 100 NEW LEVELS in the Ice Palace area."
)


def main() -> None:
    real = db.connect(os.path.join(HERE, "..", "data", "listings.sqlite"))
    before = db.history(real, "appstore", "1482155847", "us")[0]
    after = copy.deepcopy(before)
    after.update(taken_at="2026-11-18T09:00:00Z", version=f"{before['version']}-sim",
                 release_notes=SIMULATED_NOTES, rating_count=before["rating_count"] + 2000)
    # swap the last screenshot for a different real Royal Match image (the first iPad one)
    ipad_url = fetch.lookup(["1482155847"], "us", fetch.RateLimiter(0))["results"][0]["ipadScreenshotUrls"][0]
    after["screenshots"][-1] = {"url": ipad_url, "hash": fetch.screenshot_hash(ipad_url, fetch.RateLimiter(0))}

    path = os.path.join(HERE, "demo-timeline", "demo.sqlite")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    keep_cache = []
    if os.path.exists(path):  # keep cached model replies so rebuilding costs no calls
        keep_cache = sqlite3.connect(path).execute("SELECT key, model, text FROM llm_cache").fetchall()
        os.remove(path)
    con = db.connect(path)
    for key, model, text in keep_cache:
        db.store_completion(con, key, text, model)
    for snap in (before, after):
        snap = {k: v for k, v in snap.items() if k != "id"}
        db.save_snapshot(con, snap)
    print(f"{cli.write_notes(con)} note(s) written")
    render_all(con, os.path.join(HERE, "demo-timeline"), banner=BANNER)


if __name__ == "__main__":
    main()
