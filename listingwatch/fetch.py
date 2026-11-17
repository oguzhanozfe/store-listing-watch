"""App Store fetching (iTunes Lookup API) and perceptual hashing of screenshots.

Only Apple's public, documented iTunes Search/Lookup API is called. Screenshots
are fetched from Apple's image CDN as a small PNG rendition so they can be
decoded with the standard library (zlib) and hashed without Pillow.
"""
from __future__ import annotations

import json
import re
import struct
import time
import urllib.parse
import urllib.request
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


# --- perceptual hash -------------------------------------------------------

_SIZE_SUFFIX = re.compile(r"/\d+x\d+[a-z]*\.(jpg|jpeg|png|webp)$")


def thumb_url(url: str) -> str:
    """Ask Apple's CDN for a 32px-high PNG rendition of a screenshot."""
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
