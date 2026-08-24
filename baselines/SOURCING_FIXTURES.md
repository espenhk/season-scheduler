# Sourcing a full-season baseline

`baselines/sources/eliteserien_toppserien_2026.csv` currently holds only
round 1 of each league. This is the guide for extending it to the full
season, written by an agent that could not run any of it end-to-end: every
fixture-carrying host — fotball.no, eliteserien.no, fotmob, worldfootball,
transfermarkt, sofascore, Wikipedia — is refused by this environment's
egress proxy with a 403 policy denial. Web search was the only channel that
worked, and it corroborates opening-round pairings but not a full season.
What follows is therefore a best-effort recipe: reasoned from public,
generally-stable API documentation and tested offline wherever that was
possible, not verified against a live response. Treat the specifics —
league ids, the season-string format, exact JSON field names — as the part
most likely to need a small correction once you actually run it.

## Recommended: TheSportsDB

`terminliste/refdata/client.py` already talks to
[TheSportsDB](https://www.thesportsdb.com) for team/venue reference data,
using their published test key `3` — free, no signup, and per that file's
own docstring, already known to cover the Norwegian Eliteserien and
Toppserien. Reusing it here means one less service to trust and no new
account to create. `scripts/fetch_real_schedule.py` (added alongside this
guide) fetches fixtures the same way and converts them into a baseline CSV.

### 1. Find each league's numeric id

TheSportsDB indexes leagues by an internal id, not by name. Look it up:

```bash
curl "https://www.thesportsdb.com/api/v1/json/3/search_all_leagues.php?c=Norway&s=Soccer"
```

Find the entries for "Eliteserien" and "Toppserien" in the response and note
their `idLeague` values. (If that endpoint has moved or been paywalled since
this was written, the id is also visible in the URL of the league's page on
thesportsdb.com — open it in a browser and check "Lookup League" or the page
URL itself.)

### 2. Fetch each league's full-season fixture list

```bash
curl "https://www.thesportsdb.com/api/v1/json/3/eventsseason.php?id=<idLeague>&s=2026" \
    -o eliteserien_2026_raw.json
```

Repeat for Toppserien with its own id. **The `s=` season string is the one
thing here with a real chance of being wrong**: Eliteserien and Toppserien
both run within a single calendar year, so `2026` is the natural guess, but
some of TheSportsDB's other leagues use a split-year string
(`2025-2026`) even for calendar-year competitions. If `2026` comes back with
an empty `events` list, try `2025-2026` before concluding the league isn't
covered.

It's also possible full fixture lists for these two leagues sit behind
TheSportsDB's paid tier (their free test key covers team/venue lookups
reliably but gates some event endpoints for non-marquee leagues) — if both
season-string forms come back empty, that's the likely reason, and the
fallback options below apply.

### 3. Convert to a baseline CSV

```bash
python scripts/fetch_real_schedule.py \
    --competition eliteserien_2026 \
    --from-json eliteserien_2026_raw.json \
    --out baselines/sources/eliteserien_toppserien_2026.csv

python scripts/fetch_real_schedule.py \
    --competition toppserien_2026 \
    --from-json toppserien_2026_raw.json \
    --out baselines/sources/eliteserien_toppserien_2026.csv \
    --append
```

Both leagues land in the *same* file on purpose — see the module docstring
in `terminliste/baseline.py` and the "dual clubs" note in the main README:
the back-to-back-home-weekend scoring this project exists for only fires
when both leagues are scored together.

The script matches each fixture's API team names against `data/clubs.yml`,
restricted to the competition being converted (so "Brann" resolves to the
men's team for `eliteserien_2026` and the women's team for
`toppserien_2026` without ambiguity). It prints every fixture it could not
match confidently and leaves those out of the CSV rather than guessing —
review that output before trusting the file. Two failure modes to expect:

- **Unmatched entirely** — an API team name that doesn't overlap any
  candidate spelling at all. Usually means the roster changed (promotion/
  relegation) or the API uses a name variant the script's normalization
  doesn't cover; check `_team_candidates` in the script if you need to add one.
- **Fuzzy (substring) match, skipped by default** — close but not exact,
  e.g. the API's "Aalesund" against our "Aalesunds FK". Pass `--allow-fuzzy`
  to accept these instead of skipping, but read the printed pairs first —
  substring matching can also pair unrelated clubs whose names happen to
  overlap.

### 4. Update the sidecar

Edit `baselines/sources/eliteserien_toppserien_2026.yml` (or write a new
sidecar if you're keeping the partial one around):

- `coverage: complete` once both leagues run the full season
- `retrieved: <today's date>`
- `verified:` — set `true` only if you can point at a specific, re-fetchable
  source for the final numbers; TheSportsDB's own data quality varies by
  league, so a spot-check against the official site before flipping this is
  worth the ten minutes
- `sources:` — record the exact commands or URLs used, the way the existing
  entry does
- `expected_hard_violations:` — re-derive this. Run
  `python scripts/refresh_baselines.py` first and see what comes back; a
  full, real schedule should clear most or all of the current entries
  (`full_round_on_date` and the May-16 `fixed_requirement`s exist only
  because today's baseline stops in March), but don't assume it clears
  everything — a real published schedule breaking one of our rules on its
  own merits is exactly the kind of finding this baseline is for. Check
  each hard violation `refresh_baselines.py` reports against what actually
  happened in reality before deciding whether it belongs in
  `expected_hard_violations` or is a genuine finding worth a note.

### 5. Re-score and commit

```bash
python scripts/refresh_baselines.py
```

Commit `baselines/sources/` and `baselines/reports/` together.
`tests/integration/test_baselines.py` will fail the build if the committed reports drift
from what a fresh run produces, or if the baseline reports a hard violation
its sidecar hasn't declared — both are meant to catch exactly the kind of
mistake ("I regenerated the CSV but forgot to update the sidecar's
declared violations") that's easy to make here.

## Fallback: API-Football (api-sports.io)

If TheSportsDB doesn't have full fixture data for these leagues,
[API-Football](https://www.api-football.com/) is a well-documented
alternative with a free tier (100 requests/day at last check) covering a
much broader set of leagues, including lower-profile ones. It needs a free
account and an API key — more setup than TheSportsDB, which is why it's the
fallback rather than the default. `scripts/fetch_real_schedule.py` already
understands its response shape via `--schema api-football`:

```bash
curl -H "x-apisports-key: <your key>" \
    "https://v3.football.api-sports.io/fixtures?league=<id>&season=2026" \
    -o eliteserien_2026_raw.json

python scripts/fetch_real_schedule.py \
    --competition eliteserien_2026 --schema api-football \
    --from-json eliteserien_2026_raw.json \
    --out baselines/sources/eliteserien_toppserien_2026.csv
```

You'll need to look up API-Football's own league ids for Eliteserien and
Toppserien the same way — their docs have a league-search endpoint.

## Fallback: by hand

`baselines/sources/*.csv` is a plain CSV with four required columns
(`competition,date,home_team,away_team`) and one optional one (`venue`, safe
to leave blank). Copying a published fixture list from the official site
into that shape directly is always available as a last resort, tedious but
guaranteed to use whatever the site actually says rather than whatever an
API's data quality happens to be that week. `cli.py validate` lists the
exact team ids `data/clubs.yml` expects.
