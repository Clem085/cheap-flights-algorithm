# Cheap Flights — Google Flights + Playwright (Arch Linux)

This version uses **Google Flights in a Chromium browser** instead of an airfare
API. There is no Duffel account, API token, or `.env` file.

The Python program opens Chromium, visits Google Flights for each requested
airport/date combination, extracts visible result-card information, ranks it,
and exports CSV/JSON.

## 1. Install Arch packages

If Python and Chromium are already installed, skip this:

```bash
cd ~/code/cheap-flights-playwright
sudo pacman -S --needed python chromium
```

## 2. Create the project-local `.venv`

From this project directory:

```bash
python -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
```

This project deliberately uses Arch's system Chromium (`/usr/bin/chromium`),
so you do **not** need:

```bash
playwright install chromium
```

Playwright itself is still isolated inside `.venv`.

Every time you open a new terminal:

```bash
cd ~/code/cheap-flights-playwright
source .venv/bin/activate
```

## 3. Smoke test

First verify Playwright can launch your Arch Chromium:

```bash
python smoke_test.py
```

It should open Chromium, visit Google Flights, print the page title, then close.

For a completely invisible smoke test:

```bash
python smoke_test.py --headless
```

## 4. Preview a flight search

Always start with `--dry-run`. It opens no browser and shows exactly how many
Google Flights pages would be visited.

```bash
python cheap_flights.py \
  --from RDU,CLT,ATL \
  --to MAN,LHR,BHX \
  --depart 2026-12-20:2026-12-23 \
  --days 7:9 \
  --dry-run
```

The number of searches is approximately:

```text
origin airports
× destination airports
× departure dates
× permitted trip lengths
```

So the example above is:

```text
3 × 3 × 4 × 3 = 108 Google Flights searches
```

The program refuses more than 40 searches by default. This is intentional:
start small, make sure Google Flights is rendering correctly, then expand.

## 5. Run a small real search

```bash
python cheap_flights.py \
  --from RDU,CLT \
  --to LHR,MAN \
  --depart 2026-12-20:2026-12-21 \
  --days 7:8 \
  --sort cheapest \
  --top 20
```

Chromium is **visible by default**. You can watch it perform the searches.

Outputs:

```text
flight_results.csv
flight_results.json
flight_search_cache.sqlite3
```

If extraction fails, the program stores a screenshot and page HTML under:

```text
debug/
```

That is useful if Google changes the Flights page.

## 6. Larger search

Once the small search works:

```bash
python cheap_flights.py \
  --from RDU,CLT,ATL \
  --to MAN,LHR,BHX \
  --depart 2026-12-20:2026-12-23 \
  --days 7:9 \
  --sort cheapest \
  --top 30 \
  --allow-large-search
```

The script intentionally runs searches sequentially and pauses between them.
It is a personal browser-automation tool, not a high-volume scraper.

## 7. Ranking modes

### Cheapest

```bash
--sort cheapest
```

Ranks by fare, then duration and stops where available.

### Fastest

```bash
--sort fastest
```

Ranks by parsed itinerary duration.

### Best value

```bash
--sort value
```

The default score is approximately:

```text
fare
+ 3 × travel hours
+ 35 × number of stops
```

You can change the penalties:

```bash
--sort value \
--hour-penalty 5 \
--connection-penalty 60
```

This helps prevent a very long multi-stop itinerary from winning merely
because it is a few dollars cheaper.

## 8. Useful options

```text
--from RDU,CLT,ATL
--to LHR,MAN,BHX
--depart 2026-12-20:2026-12-24
--days 6:10
--one-way
--currency USD
--sort cheapest
--sort fastest
--sort value
--top 30

--headless
--slow-mo 100
--pause 1500
--timeout 30000

--cache-minutes 30
--dry-run
--allow-large-search
--keep-open
```

### Keep the browser open after the search

Useful for inspecting the final Google Flights page yourself:

```bash
python cheap_flights.py ... --keep-open
```

### Headless

Once you know the extraction works:

```bash
python cheap_flights.py ... --headless
```

Visible/headed mode is preferable while troubleshooting.

## 9. Why the script uses system Chromium on Arch

Playwright officially documents browser binaries for several supported Linux
distributions, but Arch is not on its official Linux support list. Using the
Chromium that Arch itself packages is simpler here:

```text
/usr/bin/chromium
```

The program auto-detects, in order:

```text
chromium
google-chrome-stable
google-chrome
brave
```

You can override it:

```bash
python cheap_flights.py ... --browser /usr/bin/chromium
```

## 10. Important limitation

Google Flights is a dynamic website, not a public stable API. This script avoids
depending on Google's generated CSS class names, but Google can still alter the
page markup or present an interstitial.

If a search suddenly stops parsing:

1. Look in `debug/` for the saved screenshot.
2. Open the matching `.html`.
3. Re-run one small search with `--keep-open`.
4. Adjust the extraction code if Google changed the page.

The script does not attempt to bypass CAPTCHAs, anti-bot challenges, sign-in
requirements, or other access controls.

## 11. Cache behavior

Successful searches are cached in SQLite for 30 minutes by default:

```bash
--cache-minutes 30
```

To force every search to refresh:

```bash
--cache-minutes 0
```

This prevents unnecessary repeated browser searches while tuning your ranking
or output settings.
