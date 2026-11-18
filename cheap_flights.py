#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import re
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


GOOGLE_FLIGHTS_SEARCH = "https://www.google.com/travel/flights/search"
EXTRACTOR_VERSION = "v3"
DEFAULT_DB = Path("flight_search_cache.sqlite3")
DEFAULT_DEBUG_DIR = Path("debug")


@dataclass(frozen=True)
class SearchSpec:
    origin: str
    destination: str
    depart: str
    return_date: str | None

    @property
    def key(self) -> str:
        raw = f"{EXTRACTOR_VERSION}|{self.origin}|{self.destination}|{self.depart}|{self.return_date or ''}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def label(self) -> str:
        if self.return_date:
            return f"{self.origin}→{self.destination} {self.depart}→{self.return_date}"
        return f"{self.origin}→{self.destination} {self.depart}"


@dataclass
class FlightResult:
    searched_origin: str
    searched_destination: str
    depart_date: str
    return_date: str | None
    price: float
    currency: str
    duration_minutes: int | None
    stops: int | None
    nonstop: bool
    airline: str
    raw_text: str
    aria_label: str
    search_url: str
    fetched_at: str

    @property
    def duration_hours(self) -> float:
        if self.duration_minutes is None:
            return math.inf
        return self.duration_minutes / 60.0


def parse_codes(value: str) -> list[str]:
    codes = [x.strip().upper() for x in value.split(",") if x.strip()]
    if not codes:
        raise argparse.ArgumentTypeError("Provide at least one airport/city code.")
    bad = [x for x in codes if not re.fullmatch(r"[A-Z]{3}", x)]
    if bad:
        raise argparse.ArgumentTypeError(
            "Expected 3-letter IATA codes; invalid: " + ", ".join(bad)
        )
    return codes


def parse_date_range(value: str) -> tuple[dt.date, dt.date]:
    if ":" in value:
        start_s, end_s = value.split(":", 1)
    else:
        start_s = end_s = value
    start = dt.date.fromisoformat(start_s)
    end = dt.date.fromisoformat(end_s)
    if end < start:
        raise argparse.ArgumentTypeError("End date is before start date.")
    return start, end


def parse_int_range(value: str) -> tuple[int, int]:
    if ":" in value:
        lo_s, hi_s = value.split(":", 1)
    else:
        lo_s = hi_s = value
    lo, hi = int(lo_s), int(hi_s)
    if lo < 0 or hi < lo:
        raise argparse.ArgumentTypeError("Invalid integer range.")
    return lo, hi


def date_range(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


def build_specs(
    origins: list[str],
    destinations: list[str],
    departures: tuple[dt.date, dt.date],
    days: tuple[int, int],
    one_way: bool,
) -> list[SearchSpec]:
    specs: list[SearchSpec] = []
    for origin in origins:
        for destination in destinations:
            if origin == destination:
                continue
            for depart in date_range(*departures):
                if one_way:
                    specs.append(SearchSpec(origin, destination, depart.isoformat(), None))
                    continue
                for trip_days in range(days[0], days[1] + 1):
                    ret = depart + dt.timedelta(days=trip_days)
                    specs.append(
                        SearchSpec(
                            origin,
                            destination,
                            depart.isoformat(),
                            ret.isoformat(),
                        )
                    )
    return specs


def search_query(spec: SearchSpec) -> str:
    if spec.return_date:
        return (
            f"Round trip flights from {spec.origin} to {spec.destination} "
            f"on {spec.depart} returning {spec.return_date}"
        )
    return (
        f"One-way flights from {spec.origin} to {spec.destination} "
        f"on {spec.depart}"
    )


def search_url(
    spec: SearchSpec,
    currency: str,
    language: str = "en",
    country: str = "us",
) -> str:
    params = {
        "q": search_query(spec),
        "curr": currency.upper(),
        "hl": language,
        "gl": country,
    }
    return GOOGLE_FLIGHTS_SEARCH + "?" + urlencode(params)


def init_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS cache (
            search_key TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL,
            search_url TEXT NOT NULL,
            results_json TEXT NOT NULL
        )
        """
    )
    con.commit()
    return con


def cache_get(
    con: sqlite3.Connection,
    key: str,
    max_age_minutes: int,
) -> list[FlightResult] | None:
    if max_age_minutes <= 0:
        return None

    row = con.execute(
        "SELECT fetched_at, results_json FROM cache WHERE search_key = ?",
        (key,),
    ).fetchone()
    if not row:
        return None

    fetched_at = dt.datetime.fromisoformat(row[0])
    age = dt.datetime.now(dt.timezone.utc) - fetched_at
    if age.total_seconds() > max_age_minutes * 60:
        return None

    payload = json.loads(row[1])
    return [FlightResult(**item) for item in payload]


def cache_put(
    con: sqlite3.Connection,
    spec: SearchSpec,
    url: str,
    results: list[FlightResult],
) -> None:
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO cache(search_key, fetched_at, search_url, results_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(search_key) DO UPDATE SET
            fetched_at = excluded.fetched_at,
            search_url = excluded.search_url,
            results_json = excluded.results_json
        """,
        (
            spec.key,
            fetched_at,
            url,
            json.dumps([asdict(x) for x in results], separators=(",", ":")),
        ),
    )
    con.commit()


PRICE_PATTERNS = (
    # "From 559 US dollars." / "559 US dollars"
    re.compile(
        r"(?:From\s+)?(?P<amount>\d[\d,]*(?:\.\d+)?)\s+"
        r"(?P<name>US dollars?|Canadian dollars?|Australian dollars?|"
        r"British pounds?|euros?)",
        re.IGNORECASE,
    ),
    # "$559" / "US$559" / "CA$559"
    re.compile(
        r"(?P<symbol>US\$|CA\$|AU\$|\$|£|€)\s*"
        r"(?P<amount>\d[\d,]*(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    # "559 USD"
    re.compile(
        r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<code>USD|CAD|AUD|GBP|EUR)\b",
        re.IGNORECASE,
    ),
)

CURRENCY_FROM_NAME = {
    "us dollar": "USD",
    "us dollars": "USD",
    "canadian dollar": "CAD",
    "canadian dollars": "CAD",
    "australian dollar": "AUD",
    "australian dollars": "AUD",
    "british pound": "GBP",
    "british pounds": "GBP",
    "euro": "EUR",
    "euros": "EUR",
}

CURRENCY_FROM_SYMBOL = {
    "$": "USD",
    "US$": "USD",
    "CA$": "CAD",
    "AU$": "AUD",
    "£": "GBP",
    "€": "EUR",
}


def parse_price(text: str, requested_currency: str) -> tuple[float, str] | None:
    for pattern in PRICE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        amount = float(m.group("amount").replace(",", ""))
        if m.groupdict().get("name"):
            currency = CURRENCY_FROM_NAME.get(m.group("name").lower(), requested_currency)
        elif m.groupdict().get("symbol"):
            currency = CURRENCY_FROM_SYMBOL.get(m.group("symbol").upper(), requested_currency)
        elif m.groupdict().get("code"):
            currency = m.group("code").upper()
        else:
            currency = requested_currency
        return amount, currency
    return None


DURATION_PATTERNS = (
    re.compile(
        r"\b(?P<h>\d+)\s*(?:hr|hrs|hour|hours)\s*"
        r"(?P<m>\d+)\s*(?:min|mins|minute|minutes)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?P<h>\d+)\s*(?:hr|hrs|hour|hours)\b", re.IGNORECASE),
)


def parse_duration(text: str) -> int | None:
    for pattern in DURATION_PATTERNS:
        m = pattern.search(text)
        if m:
            return int(m.group("h")) * 60 + int(m.groupdict().get("m") or 0)
    return None


def parse_stops(text: str) -> tuple[int | None, bool]:
    if re.search(r"\b(?:nonstop|non-stop|direct flight)\b", text, re.IGNORECASE):
        return 0, True

    m = re.search(r"\b(\d+)\s+(?:stops?|connections?)\b", text, re.IGNORECASE)
    if m:
        return int(m.group(1)), False

    return None, False


def guess_airline(text: str) -> str:
    """
    Prefer Google's accessible "flight with ..." carrier description.
    Include the operating carrier when Google explicitly provides one.
    """
    marketing = ""
    operated_by = ""

    m = re.search(
        r"(?:nonstop|\d+\s+stops?)\s+flight\s+with\s+(.+?)\.\s*"
        r"(?:Operated by|Leaves|Total duration|Select flight)",
        text,
        re.IGNORECASE,
    )
    if m:
        marketing = re.sub(r"\s+", " ", m.group(1)).strip(" .")

    m = re.search(
        r"\bOperated by\s+(.+?)\.\s*(?:Leaves|Total duration|Select flight)",
        text,
        re.IGNORECASE,
    )
    if m:
        operated_by = re.sub(r"\s+", " ", m.group(1)).strip(" .")

    if marketing and operated_by:
        return f"{marketing} (operated by {operated_by})"
    if marketing:
        return marketing
    if operated_by:
        return operated_by

    known = re.search(
        r"\b(American Airlines|American|United Airlines|United|Delta|"
        r"British Airways|Virgin Atlantic|Aer Lingus|Lufthansa|Air France|"
        r"KLM|Iberia|JetBlue|Air Canada|Icelandair|TAP Air Portugal)\b",
        text,
        re.IGNORECASE,
    )
    return known.group(1).strip() if known else ""


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def visible_card_records(page: Page) -> list[dict[str, str]]:
    """
    Extract one representation per visible Google Flights result card.

    v2 collected both the accessible flight link and its containing visible
    list item, which duplicated the same itinerary. v3 uses accessible labels
    exclusively when available and falls back to visible list-item text only
    if the accessible result links disappear.
    """
    records = page.evaluate(
        """
        () => {
          const visible = e =>
            !!e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length);

          const clean = s => (s || '').replace(/\\s+/g, ' ').trim();

          const accessible = [
            ...document.querySelectorAll(
              '[role="link"][aria-label^="From "][aria-label$="Select flight"]'
            )
          ].filter(visible);

          if (accessible.length) {
            const seen = new Set();
            const out = [];

            for (const el of accessible) {
              const aria = clean(el.getAttribute('aria-label'));
              if (!aria || seen.has(aria)) continue;

              seen.add(aria);
              out.push({aria, text: ''});
            }

            return out;
          }

          const fallback = [
            ...document.querySelectorAll(
              'li.pIav2d, [aria-label*="Select flight"]'
            )
          ].filter(visible);

          const seen = new Set();
          const out = [];

          for (const el of fallback) {
            const aria = clean(el.getAttribute('aria-label'));
            const text = clean(el.innerText || el.textContent);
            const key = aria || text;

            if (!key || seen.has(key)) continue;

            seen.add(key);
            out.push({aria, text});
          }

          return out;
        }
        """
    )
    return records


def page_state(page: Page) -> dict[str, object]:
    return page.evaluate(
        """
        () => {
          const body = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
          const u = new URL(location.href);
          const blocked = /^\\/sorry(?:\\/|$)/i.test(u.pathname) ||
            /captcha|unusual traffic|automated queries|verify you are human|robot check/i.test(body);
          const consent = /before you continue|consent|accept all|reject all/i.test(body);
          return {
            title: document.title,
            url: location.href,
            blocked,
            consent,
            body_excerpt: body.slice(0, 4000)
          };
        }
        """
    )


def dismiss_consent(page: Page) -> None:
    for label in ("Accept all", "Reject all", "I agree", "No thanks"):
        try:
            button = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if button.count():
                button.first.click(timeout=2000)
                page.wait_for_timeout(800)
                return
        except Exception:
            pass


def wait_for_cards(page: Page, timeout_ms: int) -> None:
    selectors = (
        '[role="link"][aria-label^="From "][aria-label$="Select flight"]',
        'li.pIav2d',
        '[aria-label*="Select flight"]',
    )

    each_timeout = max(1500, timeout_ms // len(selectors))
    last_error = None
    for selector in selectors:
        try:
            page.locator(selector).first.wait_for(state="visible", timeout=each_timeout)
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc

    raise PlaywrightTimeoutError(
        "Timed out waiting for Google Flights result cards"
    ) from last_error


def verify_search_context(page: Page, spec: SearchSpec) -> tuple[bool, str]:
    """
    Verify Google interpreted the query as the requested route.
    Airport controls currently expose accessible labels/values.
    """
    try:
        from_field = page.locator('input[aria-label^="Where from?"]:visible').first
        to_field = page.locator('input[aria-label^="Where to?"]:visible').first

        from_label = normalize_text(from_field.get_attribute("aria-label"))
        from_value = normalize_text(from_field.input_value())
        to_label = normalize_text(to_field.get_attribute("aria-label"))
        to_value = normalize_text(to_field.input_value())

        from_blob = f"{from_label} {from_value}".upper()
        to_blob = f"{to_label} {to_value}".upper()

        if spec.origin not in from_blob:
            return False, f"Google did not confirm origin {spec.origin}: {from_blob!r}"
        if spec.destination not in to_blob:
            return False, f"Google did not confirm destination {spec.destination}: {to_blob!r}"

        return True, ""
    except Exception as exc:
        # Do not silently claim validation. Extraction can continue, but caller
        # gets a warning and debug evidence.
        return False, f"Could not verify route controls: {type(exc).__name__}: {exc}"


def parse_cards(
    spec: SearchSpec,
    url: str,
    records: list[dict[str, str]],
    requested_currency: str,
) -> list[FlightResult]:
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    results: list[FlightResult] = []
    seen: set[tuple] = set()

    for record in records:
        aria = normalize_text(record.get("aria"))
        text = normalize_text(record.get("text"))
        hay = normalize_text(f"{aria} {text}")

        parsed_price = parse_price(hay, requested_currency)
        if not parsed_price:
            continue

        # A real result card should carry at least one time and flight evidence.
        has_time = bool(
            re.search(r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*[AP]M)?\b", hay, re.I)
        )
        has_flight_signal = bool(
            re.search(
                r"\b(?:flight|nonstop|stop|stops|hr|hour|airline|airways)\b",
                hay,
                re.I,
            )
        )
        if not (has_time or has_flight_signal):
            continue

        price, currency = parsed_price
        duration = parse_duration(hay)
        stops, nonstop = parse_stops(hay)
        airline = guess_airline(hay)

        # aria labels are the strongest audit trail; fall back to visible text.
        raw = aria or text
        signature = (price, currency, duration, stops, raw)
        if signature in seen:
            continue
        seen.add(signature)

        results.append(
            FlightResult(
                searched_origin=spec.origin,
                searched_destination=spec.destination,
                depart_date=spec.depart,
                return_date=spec.return_date,
                price=price,
                currency=currency,
                duration_minutes=duration,
                stops=stops,
                nonstop=nonstop,
                airline=airline,
                raw_text=text,
                aria_label=aria,
                search_url=url,
                fetched_at=fetched_at,
            )
        )

    return results


def save_debug(
    page: Page,
    debug_dir: Path,
    spec: SearchSpec,
    records: list[dict[str, str]] | None = None,
) -> tuple[Path, Path, Path]:
    debug_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{spec.origin}-{spec.destination}_{spec.depart}_{spec.return_date or 'oneway'}"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem)

    png = debug_dir / f"{safe}.png"
    html = debug_dir / f"{safe}.html"
    txt = debug_dir / f"{safe}.txt"

    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception:
        pass

    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass

    try:
        state = page_state(page)
        payload = {
            "page_state": state,
            "candidate_cards": records or [],
        }
        txt.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass

    return png, html, txt


def search_one(
    page: Page,
    spec: SearchSpec,
    currency: str,
    country: str,
    timeout_ms: int,
    debug_dir: Path,
) -> tuple[list[FlightResult], list[str]]:
    url = search_url(spec, currency, country=country)
    warnings: list[str] = []
    records: list[dict[str, str]] = []

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        dismiss_consent(page)

        state = page_state(page)
        if state.get("blocked"):
            png, html, txt = save_debug(page, debug_dir, spec)
            return [], [
                f"Google presented an anti-bot/challenge page; "
                f"debug: {png}, {html}, {txt}"
            ]

        wait_for_cards(page, timeout_ms)

        # Give the dynamic list a moment to finish populating after the first card.
        page.wait_for_timeout(1500)

        route_ok, route_warning = verify_search_context(page, spec)
        if not route_ok:
            warnings.append(route_warning)

        records = visible_card_records(page)
        results = dedupe_results(parse_cards(spec, url, records, currency))

        if not results:
            png, html, txt = save_debug(page, debug_dir, spec, records)
            warnings.append(
                f"Result cards were present but none parsed; debug: {png}, {html}, {txt}"
            )
            return [], warnings

        return results, warnings

    except Exception as exc:
        png, html, txt = save_debug(page, debug_dir, spec, records)
        return [], warnings + [
            f"{type(exc).__name__}: {exc}; debug: {png}, {html}, {txt}"
        ]


def _first_departure_arrival_times(text: str) -> tuple[str, str]:
    """
    Extract the first departure/arrival time pair so distinct flights that
    share a fare, duration and stop count are not collapsed together.
    """
    times = re.findall(
        r"\b(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:AM|PM)?\b",
        text,
        re.IGNORECASE,
    )
    if len(times) >= 2:
        return times[0].upper(), times[1].upper()
    return "", ""


def dedupe_results(results: list[FlightResult]) -> list[FlightResult]:
    """
    Remove duplicate representations of the same Google Flights card while
    preserving genuinely different itineraries.
    """
    unique: list[FlightResult] = []
    seen: set[tuple] = set()

    for r in results:
        source = r.aria_label or r.raw_text
        depart_time, arrive_time = _first_departure_arrival_times(source)

        key = (
            r.searched_origin,
            r.searched_destination,
            r.depart_date,
            r.return_date,
            r.currency,
            round(float(r.price), 2),
            r.duration_minutes,
            r.stops,
            depart_time,
            arrive_time,
            normalize_text(r.airline).lower(),
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(r)

    return unique


def sort_results(
    results: list[FlightResult],
    mode: str,
    hour_penalty: float,
    connection_penalty: float,
) -> list[FlightResult]:
    def value_score(r: FlightResult) -> float:
        hours = r.duration_hours if math.isfinite(r.duration_hours) else 99.0
        stops = r.stops if r.stops is not None else 4
        return r.price + hour_penalty * hours + connection_penalty * stops

    if mode == "value":
        return sorted(
            results,
            key=lambda r: (r.currency, value_score(r), r.price, r.duration_hours),
        )
    if mode == "fastest":
        return sorted(
            results,
            key=lambda r: (r.duration_hours, r.currency, r.price),
        )
    return sorted(
        results,
        key=lambda r: (
            r.currency,
            r.price,
            r.duration_hours,
            r.stops if r.stops is not None else 99,
        ),
    )


def format_duration(minutes: int | None) -> str:
    if minutes is None:
        return "?"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def print_results(results: list[FlightResult], top: int) -> None:
    print()
    print(
        f"{'#':>3}  {'PRICE':>12}  {'DATES':<23}  {'DURATION':>9}  "
        f"{'STOPS':>5}  AIRLINE"
    )
    print("-" * 96)

    for idx, r in enumerate(results[:top], 1):
        dates = r.depart_date
        if r.return_date:
            dates += f" → {r.return_date}"

        stops = "0" if r.nonstop else "?" if r.stops is None else str(r.stops)
        price = f"{r.currency} {r.price:,.0f}"

        print(
            f"{idx:>3}  {price:>12}  {dates:<23}  "
            f"{format_duration(r.duration_minutes):>9}  {stops:>5}  "
            f"{r.airline or '?'}"
        )


def write_csv(path: Path, results: list[FlightResult]) -> None:
    fields = list(FlightResult.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def write_json(path: Path, results: list[FlightResult]) -> None:
    path.write_text(
        json.dumps([asdict(r) for r in results], indent=2),
        encoding="utf-8",
    )


def find_chromium(explicit: str | None) -> str:
    if explicit:
        found = shutil.which(explicit) or explicit
        if Path(found).exists():
            return found
        raise SystemExit(f"Chromium executable not found: {explicit}")

    for name in ("chromium", "google-chrome-stable", "google-chrome", "brave"):
        found = shutil.which(name)
        if found:
            return found

    raise SystemExit(
        "No Chromium-family browser found.\n"
        "On Arch: sudo pacman -S chromium"
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Search bounded Google Flights combinations using local Chromium "
            "and export the visible fares."
        )
    )
    p.add_argument("--from", dest="origins", required=True, type=parse_codes)
    p.add_argument("--to", dest="destinations", required=True, type=parse_codes)
    p.add_argument("--depart", required=True, type=parse_date_range)
    p.add_argument("--days", type=parse_int_range, default=(7, 7))
    p.add_argument("--one-way", action="store_true")
    p.add_argument("--currency", default="USD")
    p.add_argument("--country", default="us", help="Google gl= country code; default us")
    p.add_argument("--top", type=int, default=20)
    p.add_argument(
        "--sort",
        choices=("cheapest", "value", "fastest"),
        default="cheapest",
    )
    p.add_argument("--hour-penalty", type=float, default=3.0)
    p.add_argument("--connection-penalty", type=float, default=35.0)
    p.add_argument("--browser", help="Browser executable/name; auto-detected by default")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--slow-mo", type=int, default=0)
    p.add_argument("--timeout", type=int, default=30000)
    p.add_argument("--pause", type=int, default=1800)
    p.add_argument("--cache-minutes", type=int, default=30)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--csv", type=Path, default=Path("flight_results.csv"))
    p.add_argument("--json", type=Path, default=Path("flight_results.json"))
    p.add_argument("--debug-dir", type=Path, default=DEFAULT_DEBUG_DIR)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-searches", type=int, default=40)
    p.add_argument("--allow-large-search", action="store_true")
    p.add_argument("--keep-open", action="store_true")
    return p


def main() -> int:
    args = parser().parse_args()

    specs = build_specs(
        args.origins,
        args.destinations,
        args.depart,
        args.days,
        args.one_way,
    )

    print(f"Cheap Flights extractor: {EXTRACTOR_VERSION}")
    print(f"Search combinations: {len(specs)}")
    print(f"From: {', '.join(args.origins)}")
    print(f"To:   {', '.join(args.destinations)}")
    print(
        f"Departures: {args.depart[0]} through {args.depart[1]} | "
        + ("one way" if args.one_way else f"trip length {args.days[0]}–{args.days[1]} days")
    )

    if args.dry_run:
        print()
        for spec in specs[:30]:
            print(" ", spec.label)
            print("   ", search_url(spec, args.currency, country=args.country))
        if len(specs) > 30:
            print(f"  ... {len(specs) - 30} more")
        return 0

    if len(specs) > args.max_searches and not args.allow_large_search:
        print(
            f"\nRefusing to run {len(specs)} browser searches automatically.\n"
            f"Default threshold: {args.max_searches}.\n"
            "Verify the scope with --dry-run, then add --allow-large-search."
        )
        return 2

    browser_exe = find_chromium(args.browser)
    print(f"Browser: {browser_exe}")

    con = init_db(args.db)
    all_results: list[FlightResult] = []
    warnings: list[str] = []
    remaining: list[SearchSpec] = []

    for spec in specs:
        cached = cache_get(con, spec.key, args.cache_minutes)
        if cached is not None:
            print(f"[cache] {spec.label}: {len(cached)} result(s)")
            all_results.extend(cached)
        else:
            remaining.append(spec)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=browser_exe,
            headless=args.headless,
            slow_mo=args.slow_mo,
        )
        context = browser.new_context(
            locale="en-US",
            viewport={"width": 1440, "height": 1050},
        )
        page = context.new_page()

        for idx, spec in enumerate(remaining, 1):
            print(f"[{idx}/{len(remaining)}] {spec.label}")

            results, search_warnings = search_one(
                page=page,
                spec=spec,
                currency=args.currency.upper(),
                country=args.country.lower(),
                timeout_ms=args.timeout,
                debug_dir=args.debug_dir,
            )

            if results:
                all_results.extend(results)
                cache_put(
                    con,
                    spec,
                    search_url(spec, args.currency, country=args.country),
                    results,
                )
                cheapest = min(results, key=lambda r: r.price)
                print(
                    f"    {len(results)} parsed; cheapest visible: "
                    f"{cheapest.currency} {cheapest.price:,.0f}"
                )

            for warning in search_warnings:
                warnings.append(f"{spec.label}: {warning}")
                print(f"    WARNING: {warning}")

            if idx < len(remaining) and args.pause > 0:
                page.wait_for_timeout(args.pause)

        if args.keep_open and not args.headless:
            input("\nBrowser kept open. Press Enter to close...")

        context.close()
        browser.close()

    if not all_results:
        print("\nNo parsable flight results were collected.")
        if warnings:
            print("\nWarnings:")
            for item in warnings[:10]:
                print(" -", item)
        print(
            "\nInspect debug/*.png, debug/*.html, and especially debug/*.txt. "
            "The .txt file records the page state and candidate card labels."
        )
        return 1

    before_dedupe = len(all_results)
    all_results = dedupe_results(all_results)
    removed = before_dedupe - len(all_results)
    if removed:
        print(f"Removed {removed} duplicate result row(s) before export.")

    ordered = sort_results(
        all_results,
        args.sort,
        args.hour_penalty,
        args.connection_penalty,
    )

    print_results(ordered, args.top)
    write_csv(args.csv, ordered)
    write_json(args.json, ordered)

    print(f"\nCSV:  {args.csv.resolve()}")
    print(f"JSON: {args.json.resolve()}")
    print(f"DB:   {args.db.resolve()}")

    if warnings:
        print(f"\n{len(warnings)} warning(s) occurred.")
        print(f"Debug directory: {args.debug_dir.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
