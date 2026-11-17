"""Static HTML timeline: index.html plus one page per (store, app, country)."""
from __future__ import annotations

import html
import os

from . import db
from .diff import diff_snapshots

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e3e1dc;--add:#e6f4ea;--del:#fde8e8;--note:#f3effc;--accent:#5b3fd1}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececef;--muted:#9a9aa2;--card:#1d1d21;--line:#2e2e34;--add:#16301f;--del:#3a1c1c;--note:#241d3d;--accent:#a996ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px}a{color:var(--accent)}h1{margin:.2em 0}.muted{color:var(--muted)}
table{width:100%;border-collapse:collapse}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0}
.chg{margin:8px 0}.field{font-weight:600}.diff{font:12px/1.45 ui-monospace,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere;margin:6px 0;max-height:320px;overflow:auto}
.diff .a{background:var(--add)}.diff .d{background:var(--del)}.words span{display:inline-block;margin:2px 4px 2px 0;padding:0 6px;border-radius:4px;font-size:13px}
.words .a{background:var(--add)}.words .d{background:var(--del)}.note{background:var(--note);border-radius:8px;padding:10px 12px;margin-top:10px}
.note b{font-size:12px;letter-spacing:.02em}.shots{display:flex;gap:6px;flex-wrap:wrap}.shots img{height:120px;border-radius:6px;border:2px solid var(--line)}
.shots .a{border-color:#2e9d57}.shots .d{border-color:#d0453f;opacity:.7}.quiet{font-size:13px}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


STORE_LABEL = {"appstore": "App Store", "googleplay": "Google Play",
               "appstore-archive": "App Store · archived snapshot vs live"}
ARCHIVE_NOTE = ("The first snapshot is an Internet Archive copy of the App Store page; the second is the live page. "
                "Months of changes collapse into one diff.")


def page_name(store: str, app_id: str, country: str) -> str:
    return f"{store}-{app_id}-{country}.html"


def _render_change(c: dict) -> str:
    f = c["field"]
    out = [f'<div class="chg"><span class="field">{_e(f.replace("_", " "))}</span> ']
    if "diff" in c:
        out.append(f'<span class="muted">{c["chars_before"]} → {c["chars_after"]} chars</span>')
        lines = "".join(
            f'<div class="{"a" if l.startswith("+") else "d"}">{_e(l)}</div>' for l in c["diff"]
        )
        out.append(f'<div class="diff">{lines}</div>')
    elif f == "screenshots":
        out.append(f'<span class="muted">{c["count_before"]} → {c["count_after"]}; '
                   f'{len(c["added"])} new, {len(c["removed"])} removed{", reordered" if c["reordered"] else ""}</span>')
        imgs = "".join(f'<img class="a" loading="lazy" src="{_e(u)}" alt="new screenshot">' for u in c["added"])
        imgs += "".join(f'<img class="d" loading="lazy" src="{_e(u)}" alt="removed screenshot">' for u in c["removed"])
        if imgs:
            out.append(f'<div class="shots">{imgs}</div>')
    elif "delta" in c:
        sign = "+" if c["delta"] > 0 else ""
        out.append(f'{_e(c["before"])} → {_e(c["after"])} <span class="muted">({sign}{_e(c["delta"])})</span>')
    else:
        out.append(f'<s class="muted">{_e(c["before"])}</s> → {_e(c["after"])}')
    if c.get("added_words") or c.get("removed_words"):
        chips = "".join(f'<span class="a">+{_e(w)}</span>' for w in c.get("added_words", []))
        chips += "".join(f'<span class="d">−{_e(w)}</span>' for w in c.get("removed_words", []))
        out.append(f'<div class="words">{chips}</div>')
    out.append("</div>")
    return "".join(out)


def render_listing(con, store: str, app_id: str, country: str, banner: str = "") -> tuple[str, dict]:
    snaps = db.history(con, store, app_id, country)
    last = snaps[-1]
    cards, quiet, changed = [], 0, 0
    for prev, cur in zip(snaps, snaps[1:]):
        changes = diff_snapshots(prev, cur)
        if not changes:
            quiet += 1
            continue
        changed += 1
        note = db.get_note(con, prev["id"], cur["id"])
        body = "".join(_render_change(c) for c in changes)
        if note:
            body += (f'<div class="note"><b>Interpretation · {_e(note["model"])} · not verified</b>'
                     f'<div>{_e(note["note"])}</div></div>')
        tag = "Archived snapshot vs live · " if store == "appstore-archive" else ""
        cards.append(f'<section class="card"><div class="muted">{tag}{_e(prev["taken_at"])} → {_e(cur["taken_at"])}'
                     f'</div>{body}</section>')
    cards.reverse()
    shots = "".join(f'<img loading="lazy" src="{_e(s["url"])}" alt="screenshot">' for s in last["screenshots"])
    head = (f'<p><a href="index.html">← all listings</a></p><h1>{_e(last["title"])}</h1>'
            f'<p class="muted">{_e(last["developer"])} · {_e(STORE_LABEL.get(store, store))} · {_e(country.upper())} · version {_e(last["version"])}'
            f' · rating {_e(last["rating"] and round(last["rating"], 2))} ({_e(last["rating_count"])})'
            f' · {len(snaps)} snapshots, {_e(snaps[0]["taken_at"])} to {_e(last["taken_at"])}'
            + (f' · <a href="{_e(last["url"])}">store page</a>' if last.get("url") else "") + "</p>"
            f'<div class="shots">{shots}</div>'
            + (f'<p class="muted">{ARCHIVE_NOTE}</p>' if store == "appstore-archive" else ""))
    quiet_line = f'<p class="quiet muted">{quiet} snapshot pair(s) with no change are not shown.</p>' if quiet else ""
    empty = "" if cards else '<p class="muted">No changes between snapshots yet.</p>'
    doc = _doc(last["title"], _banner(banner) + head + "<h2>Changes</h2>" + empty + "".join(cards) + quiet_line)
    return doc, {"title": last["title"], "snapshots": len(snaps), "changed": changed, "last": last["taken_at"]}


def _banner(text: str) -> str:
    return f'<p class="card" style="border-color:#d0453f"><b>{_e(text)}</b></p>' if text else ""


def _doc(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)}</title>'
            f'<style>{CSS}</style></head><body><main>{body}</main></body></html>\n')


def render_all(con, out_dir: str, banner: str = "") -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    rows, written = [], []
    for store, app_id, country in db.tracked(con):
        doc, info = render_listing(con, store, app_id, country, banner)
        name = page_name(store, app_id, country)
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write(doc)
        written.append(name)
        rows.append(f'<tr><td><a href="{name}">{_e(info["title"])}</a></td><td>{_e(STORE_LABEL.get(store, store))}</td>'
                    f'<td>{_e(country.upper())}</td><td>{info["snapshots"]}</td><td>{info["changed"]}</td>'
                    f'<td>{_e(info["last"])}</td></tr>')
    body = (_banner(banner) + '<h1>Store listing watch</h1><p class="muted">Listing snapshots over time. Diffs are computed; '
            'notes marked “Interpretation” are written by a language model and are guesses about intent.</p>'
            '<table><tr><th>Game</th><th>Store</th><th>Country</th><th>Snapshots</th><th>Changes</th>'
            f'<th>Last snapshot (UTC)</th></tr>{"".join(rows)}</table>')
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(_doc("Store listing watch", body))
    return written + ["index.html"]
