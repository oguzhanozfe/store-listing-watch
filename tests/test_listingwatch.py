import copy
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from listingwatch import cli, db, fetch, html, llm
from listingwatch.diff import diff_snapshots, is_material

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"


def lookup_fixture() -> dict:
    return json.loads((FIX / "lookup_us.json").read_text(encoding="utf-8"))


def png(rows: list[list[int]], filter_type: int) -> bytes:
    """Encode a grayscale 8-bit PNG with one filter type, to exercise the decoder."""
    h, w = len(rows), len(rows[0])
    raw, prev = b"", [0] * w
    for row in rows:
        out = []
        for x, v in enumerate(row):
            a = row[x - 1] if x else 0
            b = prev[x]
            c = prev[x - 1] if x else 0
            if filter_type == 0:
                pred = 0
            elif filter_type == 1:
                pred = a
            elif filter_type == 2:
                pred = b
            elif filter_type == 3:
                pred = (a + b) // 2
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
            out.append((v - pred) & 255)
        raw += bytes([filter_type] + out)
        prev = row

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FetchTests(unittest.TestCase):
    def test_normalize_recorded_lookup(self):
        snap = fetch.normalize_appstore(lookup_fixture()["results"][0], "US")
        self.assertEqual(snap["store"], "appstore")
        self.assertEqual(snap["country"], "us")
        self.assertEqual(snap["title"], "Royal Match")
        self.assertIsNone(snap["subtitle"])  # not exposed by the Lookup API
        self.assertTrue(snap["screenshots"])
        self.assertIsInstance(snap["rating_count"], int)

    def test_thumb_url_rewrites_size_and_format(self):
        self.assertEqual(fetch.thumb_url("https://h/x.jpg/392x696bb.jpg"), "https://h/x.jpg/32x32bb.png")
        self.assertEqual(fetch.thumb_url("https://h/plain"), "https://h/plain")

    def test_png_decoder_handles_every_filter(self):
        rows = [[(x * 37 + y * 91) % 256 for x in range(11)] for y in range(9)]
        for ftype in range(5):
            w, h, out = fetch.decode_png_gray(png(rows, ftype))
            self.assertEqual((w, h), (11, 9))
            self.assertEqual(out, [[float(v) for v in r] for r in rows], f"filter {ftype}")

    def test_dhash_recorded_screenshot(self):
        w, h, rows = fetch.decode_png_gray((FIX / "shot.png").read_bytes())
        self.assertEqual((w, h), (18, 32))
        value = fetch.dhash(rows, w, h)
        self.assertEqual(len(value), 16)
        # a left-right mirrored image must hash far away; a slightly brightened one must not
        mirrored = fetch.dhash([r[::-1] for r in rows], w, h)
        brighter = fetch.dhash([[min(255.0, v + 8) for v in r] for r in rows], w, h)
        self.assertGreater(fetch.hamming(value, mirrored), 16)
        self.assertLessEqual(fetch.hamming(value, brighter), 6)


def base_snap() -> dict:
    snap = fetch.normalize_appstore(lookup_fixture()["results"][0], "us")
    for i, shot in enumerate(snap["screenshots"]):
        shot["hash"] = f"{(0x0F0F0F0F << i) & 0xFFFFFFFFFFFFFFFF:016x}"
    snap["taken_at"] = "2026-11-17T04:00:00Z"
    return snap


class AppStorePageTests(unittest.TestCase):
    PAGE = (FIX / "appstore_page_royalmatch_20251126.html").read_text(encoding="utf-8")

    def test_parse_archived_page(self):
        snap = fetch.parse_appstore_page(self.PAGE, "1482155847", "US")
        self.assertEqual((snap["title"], snap["subtitle"], snap["developer"]),
                         ("Royal Match", "King Robert’s Match 3 Puzzles", "Dream Games"))
        self.assertEqual((snap["version"], snap["has_iap"], snap["rating"], snap["rating_count"]),
                         ("32473", True, 4.7, 3396270))
        self.assertEqual(snap["language"], "en-US")
        self.assertIn("100 NEW LEVELS", snap["release_notes"])
        self.assertTrue(snap["description"].startswith("King Robert needs your help"))
        self.assertEqual(len(snap["screenshots"]), 2)  # the preview video is not a screenshot
        self.assertTrue(snap["screenshots"][0]["url"].endswith("/392x696bb.jpg"))

    def test_newer_server_data_shape_and_subtitle_tag(self):
        page = self.PAGE.replace('id="serialized-server-data">[', 'id="serialized-server-data">{"data": [', 1)
        page = page.replace("}]</script>", "}]}</script>", 1).replace('<h2 class="subtitle', '<p class="subtitle')
        page = page.replace("</h2>", "</p>", 1)
        snap = fetch.parse_appstore_page(page, "1482155847", "us")
        self.assertEqual((snap["subtitle"], snap["version"]), ("King Robert’s Match 3 Puzzles", "32473"))

    def test_old_layout_fails_loudly(self):
        with self.assertRaises(ValueError):
            fetch.parse_appstore_page("<html><body>Royal Match</body></html>", "1482155847", "us")

    def test_gzipped_archive_body_is_decoded(self):
        import gzip
        self.assertEqual(fetch._maybe_gunzip(gzip.compress(b"<html>")), b"<html>")
        self.assertEqual(fetch._maybe_gunzip(b"<html>"), b"<html>")


class PlayTests(unittest.TestCase):
    PAGE = (FIX / "play_royalmatch_trimmed.html").read_text(encoding="utf-8")

    def test_parse_recorded_play_page(self):
        snap = fetch.parse_play(self.PAGE, "com.dreamgames.royalmatch", "US")
        self.assertEqual((snap["store"], snap["country"], snap["title"]), ("googleplay", "us", "Royal Match"))
        self.assertEqual(snap["subtitle"], "Decorate King Robert's Castle by solving puzzles along the way!")
        self.assertTrue(snap["description"].startswith("Welcome to Royal Match"))
        self.assertIn("100 NEW LEVELS", snap["release_notes"])
        self.assertTrue(snap["has_iap"])
        self.assertEqual((snap["price"], snap["currency"]), (0.0, "USD"))
        self.assertIsInstance(snap["rating_count"], int)
        self.assertEqual(len(snap["screenshots"]), 3)
        self.assertIsNone(snap["version"])
        self.assertTrue(fetch.thumb_url(snap["screenshots"][0]["url"]).endswith("=w32-h32-rp"))

    def test_layout_change_fails_loudly(self):
        with self.assertRaises(ValueError):
            fetch.parse_play("<html></html>", "x", "us")

    def test_robots_disallow_blocks_fetch(self):
        fetch._robots.clear()
        robots = b"User-agent: *\nDisallow: /store/apps/details\n"
        with mock.patch.object(fetch, "http_get", return_value=robots) as get:
            with self.assertRaises(PermissionError):
                fetch.fetch_play("com.example", "us", fetch.RateLimiter(0))
            self.assertEqual(get.call_count, 1)  # only robots.txt was requested
        fetch._robots.clear()


class DiffTests(unittest.TestCase):
    def test_identical_snapshots_have_no_changes(self):
        self.assertEqual(diff_snapshots(base_snap(), base_snap()), [])

    def test_text_screenshot_and_rating_changes(self):
        before, after = base_snap(), base_snap()
        after["title"] = before["title"] + ": Winter Puzzle"
        after["release_notes"] = "Winter Festival is here! Collect snowflakes for rewards."
        after["version"] = "99"
        after["rating"] = before["rating"] + 0.05
        after["rating_count"] = before["rating_count"] + 1200
        # same image re-uploaded under a new URL: not a change
        after["screenshots"][0] = {"url": "https://new/url.png", "hash": before["screenshots"][0]["hash"]}
        # one genuinely new image replaces the last one
        after["screenshots"][-1] = {"url": "https://new/winter.png", "hash": "ffffffffffffffff"}

        changes = {c["field"]: c for c in diff_snapshots(before, after)}
        self.assertEqual(set(changes), {"title", "release_notes", "version", "screenshots", "rating", "rating_count"})
        self.assertIn("winter", changes["title"]["added_words"])
        self.assertIn("snowflakes", changes["release_notes"]["added_words"])
        self.assertTrue(any(l.startswith("+Winter Festival") for l in changes["release_notes"]["diff"]))
        self.assertEqual(changes["screenshots"]["added"], ["https://new/winter.png"])
        self.assertEqual(len(changes["screenshots"]["removed"]), 1)
        self.assertFalse(changes["screenshots"]["reordered"])
        self.assertEqual(changes["rating_count"]["delta"], 1200)

    def test_reorder_and_rating_only(self):
        before, after = base_snap(), base_snap()
        after["screenshots"] = after["screenshots"][::-1]
        after["rating_count"] += 5
        changes = diff_snapshots(before, after)
        self.assertTrue(changes[0]["reordered"])
        self.assertTrue(is_material(changes))
        self.assertFalse(is_material([c for c in changes if c["field"] == "rating_count"]))

    def test_rating_noise_below_epsilon_is_ignored(self):
        before, after = base_snap(), base_snap()
        after["rating"] += 0.001
        self.assertEqual(diff_snapshots(before, after), [])


class StorageAndNotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = db.connect(os.path.join(self.tmp.name, "t.sqlite"))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _pair(self):
        a, b = base_snap(), base_snap()
        b["taken_at"] = "2026-11-17T09:00:00Z"
        b["release_notes"] = "Halloween event <script>alert(1)</script>"
        return db.save_snapshot(self.con, a), db.save_snapshot(self.con, b)

    def test_roundtrip_and_history_order(self):
        first, second = self._pair()
        snaps = db.history(self.con, "appstore", base_snap()["app_id"], "us")
        self.assertEqual([s["id"] for s in snaps], [first, second])
        self.assertEqual(snaps[0]["screenshots"], base_snap()["screenshots"])

    def test_notes_cached_and_skipped_without_material_change(self):
        first, second = self._pair()
        with mock.patch.dict(os.environ, {"LISTINGWATCH_LLM": "ollama", "LISTINGWATCH_MODEL": "qwen3.5:9b"}), \
                mock.patch.object(llm, "complete",
                                  return_value=("<think>x</think>Likely a Halloween live event.", "ollama:qwen3.5:9b")) as fake:
            self.assertEqual(cli.write_notes(self.con), 1)
            self.assertEqual(cli.write_notes(self.con), 0)  # note already exists
            self.assertEqual(cli.write_notes(self.con, redo=True), 1)  # same input: served from llm_cache
            self.assertEqual(fake.call_count, 1)
            prompt = fake.call_args[0][0]
            self.assertIn("Halloween", prompt)
        note = db.get_note(self.con, first, second)
        self.assertEqual(note["note"], "Likely a Halloween live event.")
        self.assertTrue(note["model"].startswith("ollama:qwen3.5:9b"))

    def test_auto_provider_prefers_gemini_then_deepseek_then_ollama(self):
        env = {"LISTINGWATCH_LLM": "auto", "GEMINI_API_KEY": "", "DEEPSEEK_API_KEY": ""}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(llm.provider(), "ollama")
            os.environ["DEEPSEEK_API_KEY"] = "x"
            self.assertEqual((llm.provider(), llm.model_name()), ("deepseek", "deepseek-chat"))
            os.environ["GEMINI_API_KEY"] = "x"
            self.assertEqual(llm.provider(), "gemini")

    def test_cloud_call_shape_and_usage(self):
        reply = {"choices": [{"message": {"content": "<thought>hmm\nmaybe</thought> Seasonal event. "}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
        with mock.patch.dict(os.environ, {"LISTINGWATCH_LLM": "gemini", "GEMINI_API_KEY": "k"}), \
                mock.patch.object(llm, "_post", return_value=reply) as post:
            self.assertEqual(llm.complete("diff"), ("Seasonal event.", f"gemini:{llm.model_name()}"))
            url, payload, headers = post.call_args[0]
            self.assertTrue(url.endswith("/v1beta/openai/chat/completions"))
            self.assertEqual(headers["Authorization"], "Bearer k")
            self.assertEqual(payload["model"], llm.model_name())

    def test_falls_through_models_on_rate_limit_without_looping(self):
        import urllib.error
        busy = urllib.error.HTTPError("u", 429, "quota", {}, None)
        reply = {"choices": [{"message": {"content": "ok"}}]}
        env = {"LISTINGWATCH_LLM": "gemini", "GEMINI_API_KEY": "k", "LISTINGWATCH_MODEL": "a, b, c"}
        with mock.patch.dict(os.environ, env), mock.patch.object(llm, "_post", side_effect=[busy, reply]) as post:
            self.assertEqual(llm.complete("diff"), ("ok", "gemini:b"))
            self.assertEqual([c[0][1]["model"] for c in post.call_args_list], ["a", "b"])
        with mock.patch.dict(os.environ, env), mock.patch.object(llm, "_post", side_effect=[busy] * 3) as post:
            with self.assertRaises(RuntimeError):
                llm.complete("diff")
            self.assertEqual(post.call_count, 3)

    def test_provider_none_writes_nothing(self):
        self._pair()
        with mock.patch.dict(os.environ, {"LISTINGWATCH_LLM": "none"}):
            self.assertEqual(cli.write_notes(self.con), 0)

    def test_timeline_labels_interpretation_and_escapes(self):
        first, second = self._pair()
        db.put_note(self.con, first, second, "ollama:test", "Seasonal <b>event</b>.", "2026-11-17T09:01:00Z")
        out = os.path.join(self.tmp.name, "site")
        files = html.render_all(self.con, out)
        self.assertIn("index.html", files)
        page = Path(out, files[0]).read_text(encoding="utf-8")
        self.assertIn("Interpretation · ollama:test · not verified", page)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<b>event</b>", page)

    def test_import_play_snapshot(self):
        raw = {"package": "com.example.game", "country": "GB", "title": "Example", "short_description": "Match 3",
               "recent_changes": "Bug fixes", "screenshots": ["https://example.invalid/1.png"]}
        snap = cli.play_snapshot(raw)
        self.assertEqual((snap["store"], snap["app_id"], snap["country"], snap["subtitle"]),
                         ("googleplay", "com.example.game", "gb", "Match 3"))
        self.assertEqual(snap["screenshots"][0]["hash"], None)
        db.save_snapshot(self.con, snap)
        self.assertEqual(db.tracked(self.con), [("googleplay", "com.example.game", "gb")])


class CliTests(unittest.TestCase):
    def test_watchlist_file_is_valid(self):
        by_country, play = cli.load_watchlist(str(ROOT / "watchlist.json"))
        self.assertEqual(len(by_country["us"]), 10)
        self.assertEqual(len(by_country["gb"]), 2)
        self.assertEqual(len(play), 12)

    def test_run_offline_with_recorded_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl = Path(tmp, "w.json")
            wl.write_text(json.dumps({"apps": [{"appstore": "1482155847"}, {"appstore": "1621328561"}]}))
            dbp, site = os.path.join(tmp, "x.sqlite"), os.path.join(tmp, "site")
            with mock.patch.object(fetch, "lookup", return_value=lookup_fixture()):
                for _ in range(2):
                    rc = cli.main(["--db", dbp, "run", "--watchlist", str(wl), "--no-hash", "--no-notes",
                                   "--html", site, "--lookup-interval", "0", "--log", "", "--no-play"])
                    self.assertEqual(rc, 0)
            con = db.connect(dbp)
            self.assertEqual(len(db.tracked(con)), 2)
            self.assertEqual(len(db.history(con, "appstore", "1482155847", "us")), 2)
            self.assertTrue(Path(site, "index.html").exists())


if __name__ == "__main__":
    unittest.main()
