# Jailer 2 booking watcher — Ariesplex, Trivandrum

Sends a Telegram alert when BookMyShow opens **Jailer 2** bookings for the
**opening day (Thu 15 Oct 2026)** at **Ariesplex SL Cinemas**, listing the
**earliest shows first**. It also watches Kairali Sree Nila, PVR Lulu, PVR Kripa,
Cinepolis MOT and Greenfield Moviemax (ex-Carnival).

- 🚨 Ariesplex opens → loud alert (3 pings) with the earliest shows, screen, format, price, availability, direct movie booking link on BookMyShow for that date, theatre showtimes link on BookMyShow for that date, and ariesplex.com
- ➕ More shows added later (for example a 4 AM fan show) → another alert with direct movie and theatre links for that date, marked 🔥 if it's earlier than anything seen before
- ⚠️ BookMyShow unreachable or rate-limiting → it backs off automatically and warns you, then ✅ when it recovers
- 👀 A daily "still alive" message at 9 AM IST, so silence never means "broken"

## Why it won't give false alarms

It **doesn't search page text**, so a movie title in a banner, a "coming soon"
tile, or ariesplex.com's own upcoming list can't trigger it. Instead it reads
BookMyShow's showtimes API, the same one Ariesplex's booking widget uses:

```
https://cinemas.bookmyshow.com/api/getShowtimesByVenue?venueCode=ASLC&companyCode=ASLC&dateCode=20261015
```

A show only counts if the response block's date **and** the show's own
date/time are 15 Oct, **and** the title matches `jailer 2` / `jailer ii`.
The plain 2023 "Jailer" (re-release) and "Jailer 20" don't match.

Important trap it handles: when a date isn't open yet, this API quietly returns
**today's** shows instead. See `test_jailer_watch.py` for this and other cases.

## Setup (5 minutes)

1. **Create a bot:** in Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Open your new bot and press **Start** (send it "hi").
3. Set up and get your chat id:
   ```bash
   cp .env.example .env         # put TELEGRAM_BOT_TOKEN in .env
   python3 jailer_watch.py --get-chat-id   # paste the printed TELEGRAM_CHAT_ID into .env
   python3 jailer_watch.py --test-telegram # you should get a message
   ```
4. **Prove it end to end** with a movie that's running now (it prints what it would send):
   ```bash
   python3 jailer_watch.py --once --dry-run --no-state --movie-regex '\bdhoomakethu\b' --dates today
   ```
5. Run it for real: `python3 jailer_watch.py`

No `pip install`. It needs Python 3.8+ and uses only the standard library.

## Where to run it (so it runs 24/7)

Your laptop sleeps, so it's not reliable. Pick one, or run **two for redundancy**
(duplicate alerts are harmless):

| Option | Cost | Reliability | Notes |
|---|---|---|---|
| **Oracle Cloud "Always Free" VM** (or any small VPS / Raspberry Pi) | Free / ~₹300/mo | ⭐⭐⭐ best | Checks every 60s, `systemd` restarts it on crash/reboot. See `deploy/jailer-watch.service`. |
| **GitHub Actions** (public repo) | Free | ⭐⭐ good | Already set up in `.github/workflows/watch.yml`. Add repo secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, then run it once from the Actions tab. Each run loops ~5h40m; the next one is queued to take over. Must be a **public** repo: private repos only get 2000 free min/month. |

On a new server, run `python3 jailer_watch.py --once --dry-run` first, to confirm
BookMyShow isn't blocking that server's IP. If it gets blocked later, you'll get
the ⚠️ Telegram warning.

**systemd quick start (Ubuntu VM):**
```bash
git clone <your repo> ~/bookmyshowchecker && cd ~/bookmyshowchecker
cp .env.example .env && nano .env
sudo cp deploy/jailer-watch.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now jailer-watch
journalctl -u jailer-watch -f
```

## Theatres watched (BookMyShow venue codes)

| Code | Theatre | Notes |
|---|---|---|
| `ASLC` | **Ariesplex SL Cinemas** (primary) | Audi 1 = RGB 4K laser + Dolby Atmos, the big screen. Prefer Audi 1 shows. |
| `KSNT` | Kairali Sree Nila (KSFDC), Thampanoor | ⚠️ No shows on BookMyShow right now. KSFDC also sells on its own site, [chithranjali.in](https://www.chithranjali.in), so check there too. |
| `PLTD` | PVR Lulu Mall | Only **IMAX** in the city, plus 4DX, Atmos and LUXE recliners. |
| `PKKT` | PVR Kripa, Thampanoor | Classic fan-show venue. |
| `CMTT` | Cinepolis, Mall of Travancore | Was Carnival MOT. Audi 4 = Dolby Atmos. |
| `CNGE` | Greenfield Moviemax, Karyavattom | Was Carnival Greenfield. The Carnival brand has shut down in India. |

Change the list with `VENUES=` in `.env` (the first code gets the loud alert).
Other codes: `TGNT` New Theatre, `SPTT` Sree Padmanabha, `LCTM` Lenin, `KBTT` Kalabhavan, `ATTR` Ajanta.

## Rate limits & IP blocking

The watcher is built to stay polite and to back off rather than get banned:

- **Low volume:** Ariesplex is checked about every 60s. The other theatres are checked every 3rd cycle (`OTHER_VENUES_EVERY`). That's about 2.7 requests a minute, not a burst of 6.
- **Spread out:** requests within a cycle are 1.5–4s apart, with ±5s jitter on the cycle itself.
- **Block detection:** HTTP 403 or 429, or an HTML bot-check page instead of JSON, counts as "blocked". It is **never retried right away**, and the rest of that cycle is skipped.
- **Backoff:** while blocked, the wait grows 2 min → 4 → 8 → 15 min (cap `MAX_BACKOFF_SECONDS`). It honours `Retry-After`, and returns to normal pace after the first success.
- **You're told:** a ⚠️ Telegram message after 10 failed checks or 15 minutes of failures (whichever comes first), and ✅ when it recovers.
- **Retries:** timeouts and 5xx errors get 2 quick retries, since those are real glitches rather than blocks.

## Config

See `.env.example`. Useful ones:

- `TARGET_DATES=20261014,20261015` also catches eve/premiere shows.
- If the release date moves, update `TARGET_DATES`.

## Tests

```bash
python3 -m unittest -v
```
