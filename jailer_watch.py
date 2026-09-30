#!/usr/bin/env python3
"""
Watch BookMyShow for Jailer 2 opening-day bookings at Ariesplex (Trivandrum)
and a few other theatres, and alert on Telegram.

How it avoids false alarms
--------------------------
It never searches page text. It calls the same JSON showtimes API that
BookMyShow's cinema widget uses (cinemas.bookmyshow.com/api/getShowtimesByVenue)
and only counts a show when ALL of these hold:

  * the response block's Date equals the target date, AND
  * the show's own ShowDateCode / ShowDateTime is on the target date, AND
  * the movie title (not a banner, not a "coming soon" tile) matches the regex.

This matters: when a date is not open yet, the API silently returns *today's*
shows instead. A naive checker would see "Ariesplex" + a movie list and fire.

Standard library only - no pip install needed. Python 3.8+.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
API = "https://cinemas.bookmyshow.com/api/getShowtimesByVenue"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)

# BookMyShow venue codes in Trivandrum (from BMS's own cinema list).
VENUE_NAMES = {
    "ASLC": "Ariesplex SL Cinemas, Thampanoor",
    "PLTD": "PVR Lulu Mall (IMAX / 4DX / Atmos)",
    "TGNT": "New Theatre 4K RGB Laser Atmos, Thampanoor",
    "SPTT": "Sree Padmanabha Theatre 4K, East Fort",
    "CMTT": "Cinepolis, Mall of Travancore (ex-Carnival MOT)",
    "CNGE": "Greenfield Moviemax, Karyavattom (ex-Carnival)",
    "LCTM": "Lenin Cinemas 4K Atmos (KSFDC), Thampanoor",
    "KSNT": "Kairali Sree Nila (KSFDC), Thampanoor",
    "PKKT": "PVR Kripa, Thampanoor",
    "KBTT": "Kalabhavan Theatre",
    "ATTR": "Ajanta Theatre 4K, East Fort",
    "SPMT": "Sree Padmanabha Screen 2 (Devipriya)",
}

MOVIE = "Jailer 2"  # label used in messages; set via MOVIE_NAME

AVAIL = {"0": "SOLD OUT", "1": "almost full", "2": "filling fast", "3": "available"}


def env(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v if v else default


def load_dotenv(path: Path) -> None:
    """Minimal .env loader so the script works without extra packages."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now_ist() -> datetime:
    return datetime.now(IST)


def log(msg: str) -> None:
    print(f"[{now_ist():%Y-%m-%d %H:%M:%S IST}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Fetching + parsing
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    pass


class Blocked(ApiError):
    """BookMyShow is rate-limiting or bot-checking us. Never retry immediately."""

    def __init__(self, msg: str, retry_after: int = 0):
        super().__init__(msg)
        self.retry_after = retry_after


def parse_retry_after(value: str | None) -> int:
    try:
        return max(0, min(int(float(value or 0)), 3600))
    except ValueError:
        return 0  # HTTP-date form; fall back to our own backoff


def fetch_showtimes(venue: str, date: str, timeout: int = 20, attempts: int = 3) -> dict:
    qs = urllib.parse.urlencode({"venueCode": venue, "companyCode": venue, "dateCode": date})
    req = urllib.request.Request(
        f"{API}?{qs}",
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://cinemas.bookmyshow.com/iframe",
        },
    )
    last: Exception | None = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # A 200 with HTML is a bot-check / captcha page, not a glitch.
                raise Blocked(f"{venue} {date}: non-JSON response (likely bot-check page): {raw[:80]!r}")
            if not isinstance(data, dict) or not isinstance(data.get("ShowDetails"), list):
                raise ApiError(f"unexpected response shape (keys: {list(data)[:8] if isinstance(data, dict) else type(data)})")
            return data
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise Blocked(f"{venue} {date}: HTTP {e.code}", parse_retry_after(e.headers.get("Retry-After")))
            last = e  # 5xx etc: transient, retry below
        except Blocked:
            raise
        except (urllib.error.URLError, TimeoutError, ApiError, OSError) as e:
            last = e
        if i < attempts - 1:
            time.sleep(2 * (i + 1))
    raise ApiError(f"{venue} {date}: {last}")


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


@dataclass(frozen=True)
class Show:
    venue: str
    date: str
    dt: str  # YYYYMMDDHHMM
    time: str
    screen: str
    attrs: str
    fmt: str
    lang: str
    title: str
    min_price: str
    max_price: str
    avail: str
    session: str

    @property
    def key(self) -> str:
        return self.session or f"{self.dt}|{self.screen}|{self.fmt}"


def extract_shows(data: dict, venue: str, date: str, movie_re: re.Pattern) -> list[Show]:
    """Return shows of the target movie on exactly `date`. Anything else is ignored."""
    out: list[Show] = []
    for block in data.get("ShowDetails") or []:
        if str(block.get("Date")) != date:
            continue  # API fell back to another date -> not open yet
        for ev in block.get("Event") or []:
            for ce in ev.get("ChildEvents") or []:
                names = (ev.get("EventTitle", ""), ce.get("EventName", ""))
                if not any(movie_re.search(normalize(n)) for n in names):
                    continue
                for st in ce.get("ShowTimes") or []:
                    sdt = str(st.get("ShowDateTime", ""))
                    if str(st.get("ShowDateCode", date)) != date or not sdt.startswith(date):
                        continue
                    out.append(
                        Show(
                            venue=venue,
                            date=date,
                            dt=sdt,
                            time=st.get("ShowTime", sdt[-4:]),
                            screen=(st.get("ScreenName") or "").strip(),
                            attrs=(st.get("Attributes") or "").strip(),
                            fmt=ce.get("EventDimension", ""),
                            lang=ce.get("EventLanguage", ""),
                            title=ce.get("EventName") or ev.get("EventTitle", ""),
                            min_price=str(st.get("MinPrice", "")),
                            max_price=str(st.get("MaxPrice", "")),
                            avail=AVAIL.get(str(st.get("AvailStatus")), "?"),
                            session=str(st.get("SessionId", "")),
                        )
                    )
    out.sort(key=lambda s: s.dt)
    return out


def event_count(data: dict) -> int:
    return sum(len(b.get("Event") or []) for b in data.get("ShowDetails") or [])


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #


class Notifier:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
        self.token, self.chat_id, self.dry_run = token, chat_id, dry_run

    def send(self, text: str) -> bool:
        if self.dry_run or not (self.token and self.chat_id):
            print("----- TELEGRAM (not sent) -----\n" + re.sub(r"<[^>]+>", "", text) + "\n-------------------------------", flush=True)
            return self.dry_run
        body = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"}
        ).encode()
        for i in range(4):
            try:
                req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=body)
                with urllib.request.urlopen(req, timeout=20) as r:
                    if json.loads(r.read()).get("ok"):
                        return True
            except urllib.error.HTTPError as e:
                log(f"telegram HTTP {e.code}: {e.read()[:200]!r}")
                if e.code in (400, 401, 403, 404):
                    return False  # bad token / chat id: retrying won't help
            except Exception as e:  # network blip
                log(f"telegram error: {e}")
            time.sleep(3 * (i + 1))
        return False


def booking_link(venue: str, date: str) -> str:
    return f"https://in.bookmyshow.com/buytickets/x/cinema-triv-{venue}-MT/{date}"


def fmt_date(date: str) -> str:
    return datetime.strptime(date, "%Y%m%d").strftime("%a %d %b")


def fmt_show(s: Show, mark: str = "") -> str:
    extra = " · ".join(x for x in (s.screen, s.attrs, s.fmt if s.fmt != "2D" else "", s.lang) if x)
    lo, hi = s.min_price.split(".")[0], s.max_price.split(".")[0]
    price = "" if not lo else f"₹{lo}" if lo == hi or not hi else f"₹{lo}–{hi}"
    return f"{mark}<b>{html.escape(s.time)}</b>  {html.escape(extra)}  {price}  <i>{s.avail}</i>"


def open_message(venue: str, date: str, shows: list[Show], n: int, primary: bool) -> str:
    name = html.escape(VENUE_NAMES.get(venue, venue))
    head = f"🚨🚨 <b>{html.escape(MOVIE.upper())} BOOKING OPEN</b> 🚨🚨" if primary else f"🎟 <b>{html.escape(MOVIE)} booking open</b>"
    lines = [head, f"📍 <b>{name}</b>", f"📅 {fmt_date(date)} — {len(shows)} show(s)", "", "<b>Earliest shows:</b>"]
    lines += [fmt_show(s, "🔥 " if i == 0 else "• ") for i, s in enumerate(shows[:n])]
    if len(shows) > n:
        lines.append(f"…and {len(shows) - n} more")
    lines += ["", f'👉 <a href="{booking_link(venue, date)}">Book on BookMyShow</a>']
    if venue == "ASLC":
        lines.append('👉 <a href="https://www.ariesplex.com/book-tickets">Book on ariesplex.com</a>')
    return "\n".join(lines)


def new_shows_message(venue: str, date: str, added: list[Show], all_shows: list[Show], earlier: bool) -> str:
    name = html.escape(VENUE_NAMES.get(venue, venue))
    head = f"🔥 <b>NEW EARLIER {html.escape(MOVIE)} show added</b>" if earlier else f"➕ <b>More {html.escape(MOVIE)} shows added</b>"
    lines = [head, f"📍 {name} — {fmt_date(date)}", ""]
    lines += [fmt_show(s, "• ") for s in added[:10]]
    lines += ["", f"Earliest now: {html.escape(all_shows[0].time)} ({html.escape(all_shows[0].screen)})",
              f'👉 <a href="{booking_link(venue, date)}">Book on BookMyShow</a>']
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def load_state(path: Path | None) -> dict:
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            log("state file unreadable, starting fresh")
    return {}


def save_state(path: Path | None, state: dict) -> None:
    if not path:
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(path)  # atomic


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #


def check_once(cfg, state: dict, tg: Notifier) -> int:
    """One pass over the venues. Returns extra cooldown seconds (0 = normal pace)."""
    seen: dict = state.setdefault("seen", {})
    health: dict = state.setdefault("health", {})
    primary = cfg.venues[0]
    summary = []
    other_every = max(1, getattr(cfg, "other_every", 1))
    gap = getattr(cfg, "request_gap", (0, 0))
    cycle = state.get("cycle", 0) + 1
    state["cycle"] = cycle
    blocked: Blocked | None = None
    requests_made = 0
    any_ok = False

    for venue in cfg.venues:
        # Ariesplex every cycle; the others less often to keep request volume low.
        if venue != primary and (cycle - 1) % other_every:
            continue
        for date in cfg.dates:
            k = f"{venue}|{date}"
            if blocked:  # don't keep knocking once BMS pushes back
                summary.append(f"{venue}:SKIP")
                continue
            if requests_made and gap[1] > 0:
                time.sleep(random.uniform(*gap))  # spread requests instead of a burst
            requests_made += 1
            try:
                data = fetch_showtimes(venue, date)
            except ApiError as e:
                log(f"FETCH FAIL {e}")
                if isinstance(e, Blocked):
                    blocked = e
                h = health.setdefault(venue, {"fails": 0, "alerted": False})
                h["fails"] += 1
                h.setdefault("since", time.time())
                down_min = (time.time() - h["since"]) / 60
                if venue == primary and not h["alerted"] and (
                        h["fails"] >= cfg.fail_alert_after or down_min >= getattr(cfg, "fail_alert_minutes", 1e9)):
                    why = ("BookMyShow is <b>rate-limiting / blocking</b> this IP; backing off automatically."
                           if isinstance(e, Blocked) else "It keeps retrying.")
                    if tg.send(f"⚠️ <b>{MOVIE} checker can't reach BookMyShow</b> "
                               f"({h['fails']} failed checks, {down_min:.0f} min).\n{why}\n"
                               f"Last error: <code>{html.escape(str(e))[:300]}</code>\nCheck manually meanwhile."):
                        h["alerted"] = True
                summary.append(f"{venue}:{'BLOCKED' if isinstance(e, Blocked) else 'ERR'}")
                continue

            any_ok = True
            h = health.setdefault(venue, {"fails": 0, "alerted": False})
            if h.get("alerted"):
                tg.send(f"✅ {MOVIE} checker reconnected to BookMyShow ({VENUE_NAMES.get(venue, venue)}).")
            health[venue] = {"fails": 0, "alerted": False, "events": event_count(data)}

            shows = extract_shows(data, venue, date, cfg.movie_re)
            prev = set(seen.get(k, []))
            added = [s for s in shows if s.key not in prev]
            summary.append(f"{venue}:{len(shows)}")
            if not added:
                continue

            if not prev:
                msg = open_message(venue, date, shows, cfg.earliest_n, venue == primary)
                repeats = cfg.primary_repeat if venue == primary else 1
            else:
                prev_earliest = min((s.dt for s in shows if s.key in prev), default="999")
                msg = new_shows_message(venue, date, added, shows, added[0].dt < prev_earliest)
                repeats = 1

            log(f"ALERT {venue} {date}: {len(added)} new show(s), earliest {shows[0].time}")
            ok = tg.send(msg)
            for _ in range(repeats - 1):
                time.sleep(2)
                tg.send(f"🚨 {MOVIE} — Ariesplex booking is OPEN. Go book now! 🚨" if venue == "ASLC" else msg)
            if ok:  # only remember shows once the user has actually been told
                seen[k] = sorted(prev | {s.key for s in shows})
            else:
                log("telegram send failed; will re-alert next cycle")

    if any_ok:
        state["last_ok"] = now_ist().isoformat(timespec="seconds")
    log("checked " + " ".join(summary))
    maybe_heartbeat(cfg, state, tg)

    if blocked:
        # Exponential backoff: 2x, 4x, 8x ... the poll interval, capped. Honour Retry-After.
        level = min(state.get("backoff_level", 0) + 1, 8)
        state["backoff_level"] = level
        wait = max(blocked.retry_after, min(cfg.poll * 2 ** level, getattr(cfg, "max_backoff", 900)))
        log(f"BLOCKED/RATE-LIMITED - backing off {wait}s (level {level})")
        return wait
    state["backoff_level"] = 0
    return 0


def maybe_heartbeat(cfg, state: dict, tg: Notifier) -> None:
    now = now_ist()
    today = now.strftime("%Y-%m-%d")
    if cfg.heartbeat_hour < 0 or now.hour < cfg.heartbeat_hour or state.get("last_heartbeat") == today:
        return
    health = state.get("health", {})
    seen = state.get("seen", {})
    rows = []
    for v in cfg.venues:
        n = sum(len(seen.get(f"{v}|{d}", [])) for d in cfg.dates)
        ok = health.get(v, {}).get("fails", 0) == 0
        rows.append(f"{'✅' if ok else '⚠️'} {html.escape(VENUE_NAMES.get(v, v))}: "
                    f"{f'{n} {MOVIE} show(s)' if n else 'not open yet'}")
    days = (datetime.strptime(cfg.dates[-1], "%Y%m%d").date() - now.date()).days
    if tg.send(f"👀 <b>{html.escape(MOVIE)} watcher is alive</b> — {days} day(s) to go\n" + "\n".join(rows)):
        state["last_heartbeat"] = today


def parse_args():
    p = argparse.ArgumentParser(description="Alert on Telegram when Jailer 2 bookings open on BookMyShow.")
    p.add_argument("--once", action="store_true", help="run a single check and exit")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending to Telegram")
    p.add_argument("--no-state", action="store_true", help="don't read/write the state file (for testing)")
    p.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    p.add_argument("--get-chat-id", action="store_true", help="print chat ids that recently messaged your bot")
    p.add_argument("--movie-regex", help="override MOVIE_REGEX")
    p.add_argument("--dates", help="override TARGET_DATES (comma separated YYYYMMDD, or 'today')")
    p.add_argument("--venues", help="override VENUES (comma separated BMS venue codes, first = primary)")
    return p.parse_args()


def main() -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    a = parse_args()

    class Cfg:
        pass

    global MOVIE
    MOVIE = env("MOVIE_NAME", MOVIE)
    cfg = Cfg()
    cfg.movie_re = re.compile(a.movie_regex or env("MOVIE_REGEX", r"\bjailer\s*(2|ii)\b"))
    dates = (a.dates or env("TARGET_DATES", "20261015")).replace("today", now_ist().strftime("%Y%m%d"))
    cfg.dates = [d.strip() for d in dates.split(",") if d.strip()]
    cfg.venues = [v.strip().upper() for v in (a.venues or env("VENUES", "ASLC,KSNT,PLTD,PKKT,CMTT,CNGE")).split(",") if v.strip()]
    cfg.poll = max(30, int(env("POLL_SECONDS", "60")))
    cfg.earliest_n = int(env("EARLIEST_N", "6"))
    cfg.primary_repeat = int(env("PRIMARY_REPEAT", "3"))
    cfg.fail_alert_after = int(env("FAIL_ALERT_AFTER", "10"))
    cfg.heartbeat_hour = int(env("HEARTBEAT_HOUR_IST", "9"))
    cfg.fail_alert_minutes = int(env("FAIL_ALERT_MINUTES", "15"))
    cfg.other_every = int(env("OTHER_VENUES_EVERY", "3"))  # non-primary venues every Nth cycle
    cfg.max_backoff = int(env("MAX_BACKOFF_SECONDS", "900"))
    cfg.request_gap = (1.5, 4.0)  # seconds between requests within a cycle
    run_for = int(env("RUN_FOR_SECONDS", "0"))  # 0 = forever (GitHub Actions sets this)
    stop_after = now_ist().date() > datetime.strptime(cfg.dates[-1], "%Y%m%d").date() + timedelta(days=1)

    for d in cfg.dates:
        datetime.strptime(d, "%Y%m%d")  # fail fast on typos

    tg = Notifier(env("TELEGRAM_BOT_TOKEN", ""), env("TELEGRAM_CHAT_ID", ""), a.dry_run)
    state_path = None if a.no_state else Path(env("STATE_FILE", str(Path(__file__).with_name("state.json"))))

    if a.get_chat_id:
        if not tg.token:
            print("Set TELEGRAM_BOT_TOKEN first.", file=sys.stderr)
            return 2
        with urllib.request.urlopen(f"https://api.telegram.org/bot{tg.token}/getUpdates", timeout=20) as r:
            updates = json.loads(r.read()).get("result", [])
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name") or u["message"]["chat"].get("title")
                 for u in updates if "message" in u}
        if not chats:
            print("No messages yet. Open your bot in Telegram, press Start / send 'hi', then run this again.")
        for cid, who in chats.items():
            print(f"TELEGRAM_CHAT_ID={cid}   ({who})")
        return 0
    if a.test_telegram:
        ok = tg.send(f"✅ {MOVIE} watcher: Telegram is set up correctly.")
        print("sent" if ok else "FAILED - check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return 0 if ok else 1
    if not (tg.token and tg.chat_id) and not a.dry_run:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or use --dry-run).", file=sys.stderr)
        return 2
    if stop_after:
        log("target date has passed; nothing to watch")
        return 0

    log(f"watching {cfg.movie_re.pattern!r} on {cfg.dates} at {cfg.venues} every ~{cfg.poll}s")
    started = time.monotonic()
    while True:
        state = load_state(state_path)
        cooldown = 0
        try:
            cooldown = check_once(cfg, state, tg)
        except Exception:
            log("unexpected error:\n" + traceback.format_exc())
        save_state(state_path, state)
        elapsed = time.monotonic() - started
        if a.once or (run_for and elapsed > run_for):
            return 0
        wait = max(cfg.poll, cooldown) + random.uniform(-5, 5)
        if run_for:  # don't overshoot the GitHub Actions window while backing off
            wait = min(wait, max(1, run_for - elapsed + 1))
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
