"""Offline tests for the false-positive traps. Run: python3 -m unittest -v"""

import re
import unittest
from unittest import mock

import jailer_watch as jw

TARGET = "20261015"
RE = re.compile(r"\bjailer\s*(2|ii)\b")


def show(dt, sid, screen="AUDI 1"):
    return {"ShowDateTime": dt, "ShowDateCode": dt[:8], "ShowTime": dt[8:], "SessionId": sid,
            "ScreenName": screen, "Attributes": "RGB 4K ATMOS", "MinPrice": "200.00",
            "MaxPrice": "400.00", "AvailStatus": "3"}


def event(title, shows, name=None):
    return {"EventTitle": title, "ChildEvents": [{"EventName": name or f"{title} - Tamil",
            "EventDimension": "2D", "EventLanguage": "Tamil", "ShowTimes": shows}]}


def resp(date, events):
    return {"ShowDetails": [{"Date": date, "Event": events}], "ShowDatesArray": []}


class ExtractTests(unittest.TestCase):
    def test_fallback_to_today_is_ignored(self):
        # Before bookings open, asking for 20261015 returns *today's* shows.
        # Even if Jailer 2 were somehow listed today, it must not count.
        data = resp("20260928", [event("Jailer 2", [show("202609281000", "1")])])
        self.assertEqual(jw.extract_shows(data, "ASLC", TARGET, RE), [])

    def test_original_jailer_rerelease_does_not_match(self):
        data = resp(TARGET, [event("Jailer", [show("202610150900", "1")])])
        self.assertEqual(jw.extract_shows(data, "ASLC", TARGET, RE), [])

    def test_other_movie_same_day_does_not_match(self):
        data = resp(TARGET, [event("Dhoomakethu", [show("202610150900", "1")])])
        self.assertEqual(jw.extract_shows(data, "ASLC", TARGET, RE), [])

    def test_show_on_other_date_inside_target_block_ignored(self):
        data = resp(TARGET, [event("Jailer 2", [show("202610142300", "1")])])
        self.assertEqual(jw.extract_shows(data, "ASLC", TARGET, RE), [])

    def test_title_variants_match_and_sorted_earliest_first(self):
        for title in ("Jailer 2", "JAILER-2", "Jailer II", "Jailer2"):
            data = resp(TARGET, [event(title, [show("202610150900", "b"), show("202610150400", "a")])])
            got = jw.extract_shows(data, "ASLC", TARGET, RE)
            self.assertEqual([s.session for s in got], ["a", "b"], title)

    def test_jailer_20_does_not_match(self):
        data = resp(TARGET, [event("Jailer 20", [show("202610150900", "1")])])
        self.assertEqual(jw.extract_shows(data, "ASLC", TARGET, RE), [])


class LoopTests(unittest.TestCase):
    def cfg(self):
        c = type("C", (), {})()
        c.movie_re, c.dates, c.venues = RE, [TARGET], ["ASLC"]
        c.earliest_n, c.primary_repeat, c.fail_alert_after, c.heartbeat_hour = 5, 1, 3, -1
        return c

    def test_alerts_once_then_only_on_new_shows(self):
        tg = mock.Mock(); tg.send.return_value = True
        state = {}
        r1 = resp(TARGET, [event("Jailer 2", [show("202610150600", "s1")])])
        r2 = resp(TARGET, [event("Jailer 2", [show("202610150600", "s1"), show("202610150400", "s0")])])
        with mock.patch.object(jw, "fetch_showtimes", side_effect=[r1, r1, r2]):
            jw.check_once(self.cfg(), state, tg)
            self.assertIn("BOOKING OPEN", tg.send.call_args[0][0])
            jw.check_once(self.cfg(), state, tg)
            self.assertEqual(tg.send.call_count, 1)  # nothing new -> silent
            jw.check_once(self.cfg(), state, tg)
            self.assertIn("NEW EARLIER", tg.send.call_args[0][0])

    def test_failed_telegram_send_retries_next_cycle(self):
        tg = mock.Mock(); tg.send.side_effect = [False, True]
        state = {}
        r = resp(TARGET, [event("Jailer 2", [show("202610150600", "s1")])])
        with mock.patch.object(jw, "fetch_showtimes", return_value=r):
            jw.check_once(self.cfg(), state, tg)
            jw.check_once(self.cfg(), state, tg)
        self.assertEqual(tg.send.call_count, 2)
        self.assertIn("BOOKING OPEN", tg.send.call_args[0][0])

    def test_outage_alert_after_repeated_failures(self):
        tg = mock.Mock(); tg.send.return_value = True
        state = {}
        with mock.patch.object(jw, "fetch_showtimes", side_effect=jw.ApiError("boom")):
            for _ in range(5):
                jw.check_once(self.cfg(), state, tg)
        self.assertEqual(tg.send.call_count, 1)  # alerted once, not every cycle
        self.assertIn("can't reach", tg.send.call_args[0][0])


class RateLimitTests(unittest.TestCase):
    def cfg(self, venues=("ASLC", "PLTD", "CMTT")):
        c = LoopTests.cfg(self)
        c.venues, c.poll, c.max_backoff, c.other_every = list(venues), 60, 900, 1
        return c

    def test_block_stops_cycle_and_backs_off_exponentially(self):
        tg = mock.Mock(); tg.send.return_value = True
        state = {}
        fetch = mock.Mock(side_effect=jw.Blocked("HTTP 429"))
        with mock.patch.object(jw, "fetch_showtimes", fetch):
            waits = [jw.check_once(self.cfg(), state, tg) for _ in range(5)]
        self.assertEqual(fetch.call_count, 5)  # only the first venue per cycle, rest skipped
        self.assertEqual(waits, [120, 240, 480, 900, 900])

    def test_retry_after_is_honoured_and_success_resets(self):
        tg = mock.Mock(); tg.send.return_value = True
        state = {}
        ok = resp(TARGET, [])
        with mock.patch.object(jw, "fetch_showtimes", side_effect=[jw.Blocked("429", retry_after=1800), ok, ok, ok]):
            self.assertEqual(jw.check_once(self.cfg(), state, tg), 1800)
            self.assertEqual(jw.check_once(self.cfg(), state, tg), 0)
        self.assertEqual(state["backoff_level"], 0)

    def test_other_venues_polled_less_often(self):
        tg = mock.Mock(); tg.send.return_value = True
        state = {}
        c = self.cfg(); c.other_every = 3
        fetch = mock.Mock(return_value=resp(TARGET, []))
        with mock.patch.object(jw, "fetch_showtimes", fetch):
            for _ in range(6):
                jw.check_once(c, state, tg)
        venues = [call.args[0] for call in fetch.call_args_list]
        self.assertEqual(venues.count("ASLC"), 6)
        self.assertEqual(venues.count("PLTD"), 2)  # cycles 1 and 4

    def test_http_403_429_and_html_become_blocked_without_retry(self):
        import io, urllib.error
        for exc in (urllib.error.HTTPError("u", 429, "x", {"Retry-After": "120"}, None),
                    urllib.error.HTTPError("u", 403, "x", {}, None)):
            with mock.patch("urllib.request.urlopen", side_effect=exc) as uo, mock.patch("time.sleep"):
                with self.assertRaises(jw.Blocked) as cm:
                    jw.fetch_showtimes("ASLC", TARGET)
            self.assertEqual(uo.call_count, 1)
        self.assertEqual(jw.Blocked("x", jw.parse_retry_after("120")).retry_after, 120)
        page = mock.MagicMock(); page.__enter__.return_value = io.BytesIO(b"<html>captcha</html>")
        with mock.patch("urllib.request.urlopen", return_value=page) as uo:
            with self.assertRaises(jw.Blocked):
                jw.fetch_showtimes("ASLC", TARGET)
        self.assertEqual(uo.call_count, 1)


class LinkGenerationTests(unittest.TestCase):
    def test_theatre_booking_link_known_and_unknown(self):
        # Known venue in Trivandrum
        aslc_link = jw.theatre_booking_link("ASLC", TARGET)
        self.assertEqual(
            aslc_link,
            f"https://in.bookmyshow.com/cinemas/triv/ariesplex-sl-cinemas-cinionic-dolby-atmos/buytickets/ASLC/{TARGET}",
        )
        # Backward compatibility alias
        self.assertEqual(jw.booking_link("ASLC", TARGET), aslc_link)

        # Fallback for unknown venue
        unknown_link = jw.theatre_booking_link("XYZT", TARGET)
        self.assertEqual(
            unknown_link,
            f"https://in.bookmyshow.com/cinemas/triv/xyzt/buytickets/XYZT/{TARGET}",
        )

    def test_movie_booking_link(self):
        s = jw.Show(
            venue="ASLC",
            date=TARGET,
            dt=f"{TARGET}0900",
            time="09:00 AM",
            screen="AUDI 1",
            attrs="",
            fmt="2D",
            lang="Tamil",
            title="Jailer 2",
            min_price="200",
            max_price="400",
            avail="available",
            session="s1",
            event_code="ET00123456",
            event_url="jailer-2",
        )
        url = jw.movie_booking_link(s, TARGET, "Jailer 2")
        self.assertEqual(
            url,
            f"https://in.bookmyshow.com/buytickets/jailer-2-trivandrum/movie-triv-ET00123456-MT/{TARGET}",
        )

    def test_extract_shows_captures_event_metadata(self):
        ce_data = {
            "EventTitle": "Jailer 2",
            "ChildEvents": [{
                "EventName": "Jailer 2 - Tamil",
                "EventCode": "ET00998877",
                "EventUrl": "jailer-2-tamil",
                "EventDimension": "2D",
                "EventLanguage": "Tamil",
                "ShowTimes": [show(f"{TARGET}0900", "s1")]
            }]
        }
        data = resp(TARGET, [ce_data])
        shows = jw.extract_shows(data, "ASLC", TARGET, RE)
        self.assertEqual(len(shows), 1)
        self.assertEqual(shows[0].event_code, "ET00998877")
        self.assertEqual(shows[0].event_url, "jailer-2-tamil")

    def test_open_message_contains_movie_date_and_theatre_links(self):
        s = jw.Show(
            venue="ASLC",
            date=TARGET,
            dt=f"{TARGET}0900",
            time="09:00 AM",
            screen="AUDI 1",
            attrs="RGB 4K ATMOS",
            fmt="2D",
            lang="Tamil",
            title="Jailer 2",
            min_price="200",
            max_price="400",
            avail="available",
            session="s1",
            event_code="ET00123456",
            event_url="jailer-2",
        )
        msg = jw.open_message("ASLC", TARGET, [s], 5, True)
        self.assertIn("BOOKING OPEN", msg)
        self.assertIn(TARGET, msg)
        # Contains movie booking link with date
        self.assertIn(f"movie-triv-ET00123456-MT/{TARGET}", msg)
        # Contains theatre booking link with date
        self.assertIn(f"buytickets/ASLC/{TARGET}", msg)
        # ASLC includes direct ariesplex link
        self.assertIn("https://www.ariesplex.com/book-tickets", msg)

    def test_new_shows_message_contains_movie_date_and_theatre_links(self):
        s = jw.Show(
            venue="PLTD",
            date=TARGET,
            dt=f"{TARGET}0400",
            time="04:00 AM",
            screen="IMAX",
            attrs="",
            fmt="3D",
            lang="Tamil",
            title="Jailer 2",
            min_price="300",
            max_price="600",
            avail="available",
            session="s0",
            event_code="ET00123456",
            event_url="jailer-2",
        )
        msg = jw.new_shows_message("PLTD", TARGET, [s], [s], True)
        self.assertIn("NEW EARLIER", msg)
        self.assertIn(f"movie-triv-ET00123456-MT/{TARGET}", msg)
        self.assertIn(f"buytickets/PLTD/{TARGET}", msg)


if __name__ == "__main__":
    unittest.main()
