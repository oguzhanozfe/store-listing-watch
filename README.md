# store-listing-watch

Track mobile game store listings over time and explain what competitors changed.

Every run takes a snapshot of each App Store and Google Play listing on your watchlist (title, subtitle or short
description, description, version, release notes, price, in-app-purchase flag, rating, rating count, screenshots)
into SQLite. Consecutive snapshots are diffed: text diffs, word-level
keyword shifts, screenshot set changes matched by perceptual hash, and rating moves. A language model then writes a
short "what changed and the likely intent" note for each change. The result is a static HTML timeline per app.

The diff is computed and is fact. The note is a guess, and the timeline labels it
**Interpretation · model · not verified**.

## Why

Live-ops and UA teams watch competitor store pages by hand: a new event in the release notes, a reworded first
line of the description, a screenshot swapped for a seasonal one. This does that watching on a schedule, keeps the
history, and puts the evidence (the diff) next to the interpretation so a reader can check the model's claim.

## How it works

| Step | What happens |
|---|---|
| Fetch: App Store | One call per storefront to Apple's public [iTunes Lookup API](https://performance-partners.apple.com/search-api) for all watched ids (3 s between calls). |
| Fetch: Google Play | One request per package and storefront for the public details page (5 s between requests), only after `robots.txt` allows that URL for this tool's user agent. Fields come from the page's schema.org JSON-LD block plus the description and "What's new" sections. See [Google Play](#google-play). |
| Screenshots | Each new screenshot URL is fetched once from the store's image CDN as a 32 px PNG rendition (0.5 s between downloads), decoded with `zlib` and reduced to a 64-bit difference hash (dHash). Hashes are cached by URL, so later runs download only new images. |
| Store | `data/listings.sqlite`: `snapshots`, `image_hashes`, `notes`. Each run appends a summary line to `data/runs.jsonl`. |
| Diff | Short fields compared directly; description and release notes as a line diff plus added/removed words; screenshots matched by URL or by hash distance ≤ 6 bits, so a re-uploaded identical image is not a change; rating moves ≥ 0.01 and rating count changes. |
| Note | Only for pairs with a change beyond rating drift. The prompt holds the diff only (screenshot URLs reduced to counts) and asks for at most three sentences. Cached per snapshot pair. |
| Timeline | `index.html` plus one page per store, app and storefront, newest change first. |

Standard library only, Python 3.10+. No Pillow: the PNG decoder handles 8-bit gray/RGB/RGBA and all five row
filters, and a test checks it against hand-encoded images for every filter type.

## Run it

```bash
git clone https://github.com/oguzhanozfe/store-listing-watch && cd store-listing-watch
python -m unittest -v                                # offline, uses recorded fixtures
python -m listingwatch run --html timeline           # snapshot the watchlist, write notes, render
open timeline/index.html
```

Notes use a cloud model by default, chosen from the keys in your environment: the Gemini API
(`GEMINI_API_KEY`), else DeepSeek (`DEEPSEEK_API_KEY`, `deepseek-chat`), else a local [Ollama](https://ollama.com) model. Without any key or model, snapshots, diffs and
the timeline still work; `LISTINGWATCH_LLM=none` skips notes on purpose. Every note stores the provider and model
that wrote it, and each run logs the call count and token usage in `data/runs.jsonl`.

Free-tier quotas on the Gemini API are small and counted per model, so the Gemini provider walks a list:
`gemma-4-31b-it`, `gemini-3.5-flash-lite`, `gemma-4-26b-a4b-it`, `gemini-3.1-flash-lite`, `gemini-3.5-flash`.
Inline `<thought>`/`<think>` reasoning blocks are stripped from replies.
Each model is tried once; a 429, 5xx or network error moves on to the next, and if all fail the note is skipped
and retried by the next `run` or `notes`. Responses are cached in SQLite by a hash of provider, model list, system
prompt and diff, so re-rendering or `notes --redo` on unchanged input makes no calls.

| Variable | Default | Meaning |
|---|---|---|
| `LISTINGWATCH_LLM` | `auto` | `auto`, `gemini`, `deepseek`, `openai` (any OpenAI-compatible endpoint), `ollama` or `none` |
| `LISTINGWATCH_MODEL` | per provider | one model or a comma-separated fallback list |
| `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` | – | cloud keys, read from the environment only |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | – | only for `openai` |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | only for `ollama` |

Only the diff of public listing text is sent to the model. Tests and CI never call a model.

Other commands:

```bash
python -m listingwatch notes                  # write any missing notes (e.g. after the model was unreachable)
python -m listingwatch timeline --out timeline
python -m listingwatch diff appstore 1482155847 us   # latest diff as JSON
python -m listingwatch import-play snapshot.json     # manual Google Play snapshot, see below
python -m listingwatch run --no-play                 # App Store only
```

Cron, once a day (Apple's Lookup responses are served with a CDN `max-age` of about 24 h, see Results):

```cron
17 6 * * * cd /path/to/store-listing-watch && python3 -m listingwatch run --html timeline >> run.log 2>&1
```

### Watchlist

`watchlist.json` lists App Store ids, Play packages and optional per-app storefronts:

```json
{"countries": ["us"],
 "apps": [{"name": "Royal Match", "appstore": "1482155847", "play": "com.dreamgames.royalmatch", "countries": ["us", "gb"]}]}
```

The committed watchlist has 10 public games (Royal Match, MONOPOLY GO!, Candy Crush Saga, Clash of Clans,
Clash Royale, Brawl Stars, Subway Surfers, Pokémon GO, Genshin Impact, Homescapes), US storefront, plus GB for
Royal Match and MONOPOLY GO!: 12 App Store listings and 12 Google Play listings.

### Google Play

Google offers no public API for other developers' Play listings (the Play Developer API covers only your own
apps), so the tool reads the public details page. The rules it follows:

- [Google's Terms of Service](https://policies.google.com/terms) treat automated access as abuse when it is
  "in violation of the machine-readable instructions on our web pages", with robots.txt as the example. The [Google Play Terms of Service](https://play.google.com/about/play-terms/)
  add no separate rule on automated access (checked 2026-11-17).
- [`play.google.com/robots.txt`](https://play.google.com/robots.txt) does not disallow `/store/apps/details`
  (checked 2026-11-17). The tool still parses `robots.txt` with `urllib.robotparser` on every run and skips any
  URL it disallows; if `robots.txt` cannot be read, it fetches nothing from that host.
- One request per listing per run, 5 s apart, with an identifying user agent. No login, no CAPTCHA handling, no
  proxy rotation. A layout change makes the parser fail loudly for that listing rather than guess.

If you would rather not fetch at all, run with `--no-play` and import snapshots you captured yourself:

```json
{"package": "com.example.game", "country": "us", "taken_at": "2026-11-17T09:00:00Z",
 "title": "Example Game", "short_description": "Match three and decorate", "description": "...",
 "version": "1.2.3", "recent_changes": "Winter event", "price": 0, "currency": "USD", "has_iap": true,
 "rating": 4.6, "rating_count": 120000, "screenshots": ["https://..."]}
```

Imported snapshots are diffed, annotated and rendered like fetched ones. Their screenshots are not downloaded, so
screenshot changes are matched by URL unless you supply `{"url", "hash"}` objects.

## Results

Two real runs over the committed watchlist (12 listings), logged in [`data/runs.jsonl`](data/runs.jsonl) and
stored in [`data/listings.sqlite`](data/listings.sqlite):

| Run (UTC) | Snapshots | Screenshots downloaded | Lookup failures | Notes | Wall time |
|---|---|---|---|---|---|
| 2026-11-17 01:02:39 | 12 | 83 (all new) | 0 | 0 (first run, nothing to diff) | 45.5 s |
| 2026-11-17 09:39:43 | 12 | 0 (all 83 URLs cached) | 0 | 0 | 3.1 s |

**Between the two runs, 8 h 37 min apart, none of the 12 listings changed**: same title, description, version,
release notes, price, screenshot set, rating and rating count. So the real timeline in
[`examples/timeline/`](examples/timeline/index.html) has two snapshots per listing and no change cards yet, and no
notes were written. Apple's Lookup data appears to refresh less often than every few hours (the rating counts
did not move either), so a daily or six-hourly cron is a sensible cadence.

To show what a change looks like, [`examples/demo-timeline/`](examples/demo-timeline/index.html) is a
**simulated** example, labelled as such on every page: the first snapshot is the real Royal Match US snapshot
from run 1, and the second is a copy edited by [`examples/make_demo.py`](examples/make_demo.py) (invented winter
event release notes, version suffix `-sim`, +2,000 ratings, last screenshot swapped for a different real Royal
Match image). Its note was written by `gemma-4-26b-a4b-it` on the Gemini API, as recorded in the `notes` table of
`examples/demo-timeline/demo.sqlite`. The note correctly read the
edit as a seasonal event; that is one example, not an evaluation.

## Known limits

- **No subtitle or in-app-purchase flag for the App Store.** The Lookup API does not return them; those columns
  stay empty for App Store snapshots. Promotional text is not returned either.
- **Only iPhone screenshots** are tracked (`screenshotUrls`), not iPad or Apple TV sets, and not preview videos.
- **dHash on a 32 px rendition** catches swapped or redesigned images, not small text edits inside a screenshot.
  The 6-bit threshold is a judgement call, not tuned on labelled data.
- **One storefront language per country.** The API returns the default localisation of each storefront.
- **Notes are unverified.** The model sees only the diff; it can still over-read a routine update as a campaign.
  The note sits beside the diff so the reader can check it. No evaluation of note quality has been run.
- **Rating moves** use Apple's rounded averages, so small moves are mostly noise; only changes ≥ 0.01 are shown
  and they never trigger a note on their own.
- **Snapshot cadence is yours.** Changes between two runs collapse into one diff; a change made and reverted
  between runs is invisible.
- **Apple's Lookup responses are cached for about a day.** The exact run URL comes back from Akamai with
  `cache-control: max-age` near 86,400 s; a URL variant never requested before returned the same values
  ([`data/cache_check.txt`](data/cache_check.txt)), so the stale data is Apple's, not just the CDN's. Running
  more often than daily adds snapshots, not information.
- **Google Play version is not captured.** The version string is not in the page's static HTML; the "Updated on"
  date is stored in `version_date` instead. Play rating counts are Google's rounded public figures.
- **The Play parser depends on an undocumented page layout** (JSON-LD block, `data-g-id="description"`, the
  "What's new" section). It is tested against a trimmed copy of a real page and fails loudly when those are
  missing; it will need updating when Google changes the page.

## Layout

```
listingwatch/fetch.py   Lookup API, Play page fetch (robots.txt checked) and parse, rate limiter, PNG decode, dHash
listingwatch/db.py      SQLite schema and queries
listingwatch/diff.py    snapshot diff
listingwatch/llm.py     note prompt; Gemini / DeepSeek / OpenAI-compatible / Ollama client with retries
listingwatch/html.py    static timeline
listingwatch/cli.py     run, import-play, notes, timeline, diff
tests/                  unittest suite and recorded fixtures (a real Lookup response, a trimmed real Play page,
                        a real 32 px screenshot)
data/                   committed SQLite database and run log from real runs
examples/timeline/      the timeline rendered from data/ (real)
examples/demo-timeline/ simulated change demo, built by examples/make_demo.py
```

## License

MIT. App names, descriptions and screenshots in `data/` and `examples/` belong to their publishers and are
included only as the factual record this tool collects. Screenshot images are not stored; the timeline links to
the stores' CDNs.
