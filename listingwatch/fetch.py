"""Listing fetching and perceptual hashing of screenshots.

App Store: Apple's public, documented iTunes Lookup API.
Google Play: the public details page, fetched only when play.google.com/robots.txt allows the URL for our user
agent (Google's terms prohibit automated access only where it violates such machine-readable instructions),
rate-limited, and parsed from its schema.org JSON-LD block plus a few plain HTML sections. Screenshots
are fetched from Apple's image CDN as a small PNG rendition so they can be
decoded with the standard library (zlib) and hashed without Pillow.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import struct
import time
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib

USER_AGENT = "store-listing-watch/0.1 (+https://github.com/oguzhanozfe/store-listing-watch)"
LOOKUP_URL = "https://itunes.apple.com/lookup"


class RateLimiter:
    """Minimum gap between requests. Apple asks for roughly 20 lookup calls a minute."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()


def http_get(url: str, limiter: RateLimiter, timeout: float = 30) -> bytes:
    limiter.wait()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def lookup(app_ids: list[str], country: str, limiter: RateLimiter) -> dict:
    """One Lookup call for several ids in one storefront. Returns the raw JSON."""
    query = urllib.parse.urlencode({"id": ",".join(app_ids), "country": country, "entity": "software"})
    return json.loads(http_get(f"{LOOKUP_URL}?{query}", limiter))


def normalize_appstore(result: dict, country: str) -> dict:
    """Map one Lookup result to the snapshot fields we store.

    The Lookup API does not expose the subtitle or an in-app-purchase flag, so
    both stay None for App Store snapshots (see README, known limits).
    """
    return {
        "store": "appstore",
        "app_id": str(result["trackId"]),
        "country": country.lower(),
        "title": result.get("trackName"),
        "subtitle": None,
        "developer": result.get("artistName"),
        "description": result.get("description") or "",
        "version": result.get("version"),
        "version_date": result.get("currentVersionReleaseDate"),
        "release_notes": result.get("releaseNotes") or "",
        "price": result.get("price"),
        "currency": result.get("currency"),
        "has_iap": None,
        "rating": result.get("averageUserRating"),
        "rating_count": result.get("userRatingCount"),
        "screenshots": [{"url": u, "hash": None} for u in result.get("screenshotUrls") or []],
        "url": result.get("trackViewUrl"),
    }


# --- Google Play ----------------------------------------------------------

PLAY_DETAILS = "https://play.google.com/store/apps/details"
_robots: dict[str, urllib.robotparser.RobotFileParser] = {}


def robots_allows(url: str, limiter: RateLimiter) -> bool:
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    if origin not in _robots:
        parser = urllib.robotparser.RobotFileParser()
        try:
            parser.parse(http_get(f"{origin}/robots.txt", limiter).decode("utf-8", "replace").splitlines())
        except Exception:
            parser.parse(["User-agent: *", "Disallow: /"])  # cannot read robots.txt: do not crawl
        _robots[origin] = parser
    return _robots[origin].can_fetch(USER_AGENT, url)


def play_url(package: str, country: str) -> str:
    return f"{PLAY_DETAILS}?{urllib.parse.urlencode({'id': package, 'hl': 'en_US', 'gl': country.upper()})}"


def fetch_play(package: str, country: str, limiter: RateLimiter) -> str:
    url = play_url(package, country)
    if not robots_allows(url, limiter):
        raise PermissionError(f"robots.txt disallows {url}")
    return http_get(url, limiter).decode("utf-8", "replace")


def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    return htmllib.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def parse_play(page: str, package: str, country: str) -> dict:
    """Map a Play details page to snapshot fields. Version is not in the page's static HTML, so it stays None."""
    ld = None
    for block in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        data = json.loads(block)
        if data.get("@type") == "SoftwareApplication":
            ld = data
    if ld is None:
        raise ValueError("no SoftwareApplication JSON-LD block; the page layout may have changed")
    desc = re.search(r'data-g-id="description"[^>]*>(.*?)</div>', page, re.S)
    news = re.search(r'What’s new</h2>.*?<div itemprop="description">(.*?)</div>', page, re.S)
    updated = re.search(r"Updated on</div><div[^>]*>([^<]+)<", page)
    rating = ld.get("aggregateRating") or {}
    offer = (ld.get("offers") or [{}])[0]
    shots = re.findall(r'<img src="([^"]+)"[^>]*alt="Screenshot image"', page)
    return {
        "store": "googleplay",
        "app_id": package,
        "country": country.lower(),
        "title": ld.get("name"),
        "subtitle": ld.get("description"),          # Play's short description
        "developer": (ld.get("author") or {}).get("name"),
        "description": _text(desc.group(1)) if desc else "",
        "version": None,
        "version_date": updated.group(1).strip() if updated else None,
        "release_notes": _text(news.group(1)) if news else "",
        "price": float(offer["price"]) if offer.get("price") is not None else None,
        "currency": offer.get("priceCurrency"),
        "has_iap": ">In-app purchases<" in page,
        "rating": float(rating["ratingValue"]) if rating.get("ratingValue") else None,
        "rating_count": int(rating["ratingCount"]) if rating.get("ratingCount") else None,
        "screenshots": [{"url": htmllib.unescape(u), "hash": None} for u in dict.fromkeys(shots)],
        "url": play_url(package, country),
    }


# --- perceptual hash -------------------------------------------------------

_SIZE_SUFFIX = re.compile(r"/\d+x\d+[a-z]*\.(jpg|jpeg|png|webp)$")


def thumb_url(url: str) -> str:
    """Ask the store's image CDN for a small PNG rendition of a screenshot."""
    if "googleusercontent.com/" in url:
        return url.split("=")[0] + "=w32-h32-rp"  # -rp forces PNG; plain sizes may return JPEG
    if _SIZE_SUFFIX.search(url):
        return _SIZE_SUFFIX.sub("/32x32bb.png", url)
    return url


def decode_png_gray(data: bytes) -> tuple[int, int, list[list[float]]]:
    """Decode an 8-bit non-interlaced PNG (gray, RGB or RGBA) into luminance rows."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat = 8, b""
    width = height = depth = ctype = interlace = None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
        pos += 12 + length
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    if depth != 8 or channels is None or interlace:
        raise ValueError(f"unsupported PNG (depth={depth}, color type={ctype}, interlace={interlace})")
    raw = zlib.decompress(idat)
    stride = width * channels
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(height):
        ftype, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 255
            elif ftype == 2:
                line[x] = (line[x] + b) & 255
            elif ftype == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        if channels >= 3:
            rows.append([0.299 * line[x] + 0.587 * line[x + 1] + 0.114 * line[x + 2] for x in range(0, stride, channels)])
        else:
            rows.append([float(line[x]) for x in range(0, stride, channels)])
    return width, height, rows


def dhash(rows: list[list[float]], width: int, height: int) -> str:
    """64-bit difference hash: shrink to 9x8 (box average), compare neighbours."""
    def cell(cx: int, cy: int) -> float:
        x0, x1 = cx * width // 9, max(cx * width // 9 + 1, (cx + 1) * width // 9)
        y0, y1 = cy * height // 8, max(cy * height // 8 + 1, (cy + 1) * height // 8)
        vals = [rows[y][x] for y in range(y0, min(y1, height)) for x in range(x0, min(x1, width))]
        return sum(vals) / len(vals)

    grid = [[cell(x, y) for x in range(9)] for y in range(8)]
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (grid[y][x] > grid[y][x + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def screenshot_hash(url: str, limiter: RateLimiter) -> str:
    width, height, rows = decode_png_gray(http_get(thumb_url(url), limiter))
    return dhash(rows, width, height)


# --- App Store web page and Internet Archive copies ------------------------

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"


def _maybe_gunzip(data: bytes) -> bytes:
    return zlib.decompress(data, 16 + zlib.MAX_WBITS) if data[:2] == b"\x1f\x8b" else data


def fetch_appstore_page(url: str, limiter: RateLimiter) -> str:
    """The live apps.apple.com product page, only where its robots.txt allows the URL."""
    if not robots_allows(url, limiter):
        raise PermissionError(f"robots.txt disallows {url}")
    return _maybe_gunzip(http_get(url, limiter)).decode("utf-8", "replace")


def wayback_captures(page_url: str, limiter: RateLimiter, since: str = "", until: str = "") -> list[tuple[str, str]]:
    """(timestamp, original URL) of archived HTML captures of one product page, oldest first."""
    url = page_url.split("://", 1)[-1].split("?", 1)[0]
    query = {"url": url, "matchType": "prefix", "filter": ["statuscode:200", "mimetype:text/html"],
             "fl": "timestamp,original", "output": "json", "from": since, "to": until}
    rows = json.loads(http_get(f"{WAYBACK_CDX}?{urllib.parse.urlencode(query, doseq=True)}", limiter, timeout=120) or b"[]")
    return sorted((t, o) for t, o in rows[1:] if "see-all" not in o)


def fetch_wayback(timestamp: str, original: str, limiter: RateLimiter) -> str:
    """The archived page as originally served (the `id_` form, without the archive's toolbar)."""
    data = http_get(f"https://web.archive.org/web/{timestamp}id_/{original}", limiter, timeout=60)
    return _maybe_gunzip(data).decode("utf-8", "replace")


def parse_appstore_page(page: str, app_id: str, country: str) -> dict:
    """Map an apps.apple.com product page (the layout in use since 2025) to snapshot fields.

    Unlike the Lookup API, the page shows the subtitle and the in-app-purchase flag. Fields come from the
    schema.org JSON-LD block and the page's embedded `serialized-server-data` JSON.
    """
    ld = re.search(r'<script id=["]?software-application["]? type="application/ld\+json">(.*?)</script>', page, re.S)
    server = re.search(r'<script type="application/json" id="serialized-server-data">(.*?)</script>', page, re.S)
    if not (ld and server):
        raise ValueError("no JSON-LD or serialized-server-data block; older or changed page layout")
    ld = json.loads(ld.group(1))
    data = json.loads(server.group(1))
    data = data["data"] if isinstance(data, dict) else data  # 2025 pages: a list; 2026 pages: {"data": [...]}
    shelves = data[0]["data"]["shelfMapping"]
    language = (data[0].get("intent") or {}).get("language")
    recent = ((shelves.get("mostRecentVersion") or {}).get("items") or [{}])[0]
    desc = ((shelves.get("description") or {}).get("items") or [{}])[0].get("paragraph", {}).get("text")
    shots = [i["screenshot"]["template"].replace("{w}x{h}{c}.{f}", "392x696bb.jpg")
             for i in (shelves.get("product_media_phone_") or {}).get("items", []) if "screenshot" in i]
    subtitle = re.search(r'<(h2|p) class="subtitle[^"]*">(.*?)</\1>', page, re.S)
    attributes = re.search(r'<p class="attributes[^"]*">(.*?)</p>', page, re.S)
    rating, offer = ld.get("aggregateRating") or {}, ld.get("offers") or {}
    return {
        "store": "appstore",
        "app_id": str(app_id),
        "country": country.lower(),
        "title": ld.get("name"),
        "subtitle": _text(subtitle.group(2)) if subtitle else None,
        "developer": (ld.get("author") or {}).get("name"),
        "description": desc or ld.get("description") or "",
        "version": (recent.get("primarySubtitle") or "").replace("Version ", "") or None,
        "version_date": recent.get("secondarySubtitle"),
        "release_notes": recent.get("text") or "",
        "price": offer.get("price"),
        "currency": offer.get("priceCurrency"),
        "has_iap": bool(re.search(r"In.App Purchases", _text(attributes.group(1)))) if attributes else None,
        "rating": rating.get("ratingValue"),
        "rating_count": rating.get("reviewCount"),
        "screenshots": [{"url": u, "hash": None} for u in dict.fromkeys(shots)],
        "url": None,
        "language": language,  # not stored; archive captures are sometimes another locale of the same URL
    }
