# store-listing-watch

Track mobile game store listings over time and explain what competitors changed.

Every run takes a snapshot of each listing on your watchlist (title, description, version, release notes,
price, rating, rating count, screenshots) into SQLite. Consecutive snapshots are diffed: text diffs, word-level
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
| Fetch | One call per storefront to Apple's public [iTunes Lookup API](https://performance-partners.apple.com/search-api) for all watched ids (3 s between calls). |
| Screenshots | Each new screenshot URL is fetched once from Apple's image CDN as a 32 px PNG rendition (0.5 s between downloads), decoded with `zlib` and reduced to a 64-bit difference hash (dHash). Hashes are cached by URL, so later runs download only new images. |
| Store | `data/listings.sqlite`: `snapshots`, `image_hashes`, `notes`. Each run appends a summary line to `data/runs.jsonl`. |
| Diff | Short fields compared directly; description and release notes as a line diff plus added/removed words; screenshots matched by URL or by hash distance ≤ 6 bits, so a re-uploaded identical image is not a change; rating moves ≥ 0.01 and rating count changes. |
| Note | Only for pairs with a change beyond rating drift. The prompt holds the diff only (screenshot URLs reduced to counts) and asks for at most three sentences. Cached per snapshot pair. |
| Timeline | `index.html` plus one page per store, app and storefront, newest change first. |

Standard library only, Python 3.10+. No Pillow: the PNG decoder handles 8-bit gray/RGB/RGBA and all five row
filters, and a test checks it against hand-encoded images (it also matched Pillow's output exactly on a real
screenshot during development).

## Run it

```bash
git clone https://github.com/oguzhanozfe/store-listing-watch && cd store-listing-watch
python -m unittest -v                                # offline, uses recorded fixtures
python -m listingwatch run --html timeline           # snapshot the watchlist, write notes, render
open timeline/index.html
```

Notes use a cloud model by default, chosen from the keys in your environment: Gemini (`GEMINI_API_KEY`,
default `gemini-3.5-flash`, free tier is enough for a watchlist this size), else DeepSeek (`DEEPSEEK_API_KEY`,
`deepseek-chat`), else a local [Ollama](https://ollama.com) model. Without any key or model, snapshots, diffs and
the timeline still work; `LISTINGWATCH_LLM=none` skips notes on purpose. Every note stores the provider and model
that wrote it, and each run logs the call count and token usage in `data/runs.jsonl`.

| Variable | Default | Meaning |
|---|---|---|
| `LISTINGWATCH_LLM` | `auto` | `auto`, `gemini`, `deepseek`, `openai` (any OpenAI-compatible endpoint), `ollama` or `none` |
| `LISTINGWATCH_MODEL` | per provider | `gemini-3.5-flash`, `deepseek-chat`, `gpt-4o-mini`, `qwen3.5:9b` |
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
```

Cron, every six hours:

```cron
0 */6 * * * cd /path/to/store-listing-watch && python3 -m listingwatch run --html timeline >> run.log 2>&1
```

### Watchlist

`watchlist.json` lists App Store ids, optional per-app storefronts, and the Play package for reference:

```json
{"countries": ["us"],
 "apps": [{"name": "Royal Match", "appstore": "1482155847", "play": "com.dreamgames.royalmatch", "countries": ["us", "gb"]}]}
```

The committed watchlist has 10 public games (Royal Match, MONOPOLY GO!, Candy Crush Saga, Clash of Clans,
Clash Royale, Brawl Stars, Subway Surfers, Pokémon GO, Genshin Impact, Homescapes), US storefront, plus GB for
Royal Match and MONOPOLY GO!: 12 listings.

### Google Play: manual snapshots only

Google offers no public API for other developers' Play listings (the Play Developer API covers only your own
apps), and Google's Terms of Service do not allow automated access to its services outside the interfaces it
provides. So this tool does not fetch from Google Play. You can still track Play listings by capturing them
yourself (open the page in a browser, copy the fields) and importing a JSON file:

```json
{"package": "com.example.game", "country": "us", "taken_at": "2026-11-17T09:00:00Z",
 "title": "Example Game", "short_description": "Match three and decorate", "description": "...",
 "version": "1.2.3", "recent_changes": "Winter event", "price": 0, "currency": "USD", "has_iap": true,
 "rating": 4.6, "rating_count": 120000, "screenshots": ["https://..."]}
```

Imported Play snapshots are diffed, annotated and rendered like App Store ones. Their screenshots are not
downloaded or hashed, so screenshot changes are matched by URL only unless you supply `{"url", "hash"}` objects.

## Results

Snapshots are being collected; results from the committed runs are added here once two runs exist.

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
- Google Play is manual, as explained above.

## Layout

```
listingwatch/fetch.py   Lookup API, rate limiter, PNG decode, dHash
listingwatch/db.py      SQLite schema and queries
listingwatch/diff.py    snapshot diff
listingwatch/llm.py     note prompt; Gemini / DeepSeek / OpenAI-compatible / Ollama client with retries
listingwatch/html.py    static timeline
listingwatch/cli.py     run, import-play, notes, timeline, diff
tests/                  unittest suite and recorded fixtures (a real Lookup response, a real 32 px screenshot)
data/                   committed SQLite database and run log from real runs
examples/timeline/      the timeline rendered from data/
```

## License

MIT. App names, descriptions and screenshots in `data/` and `examples/` belong to their publishers and are
included only as the factual record this tool collects. Screenshot images are not stored; the timeline links to
Apple's CDN.
