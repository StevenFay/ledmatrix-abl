#!/usr/bin/env python3
"""
The ABL feed: what changed from the baseball plugin's minor-league path.

- the Stats API is asked for the ABL only (sport 17, league 595), all season
  (the minor-league code fetched April-September, the ABL's off-season);
- games convert to the ESPN shape the shared screens read, from
  test/fixtures/abl_schedule.json (real 2025-26 opening results, re-dated),
  including a seven-inning final that is complete, not "Final/7";
- team logos are downloaded with the games, retried after a failure;
- end to end, the real managers sort the games into live, recent, upcoming.

Run: <core-venv>/bin/python test_abl_feed.py   (needs a LEDMatrix core on PYTHONPATH)
"""

import copy
import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytz

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
logging.disable(logging.CRITICAL)

try:
    import abl_managers
    from abl_managers import BaseABLManager
except ImportError as exc:  # no core checkout on the path
    print(f"SKIP: needs a LEDMatrix core on PYTHONPATH ({exc})")
    sys.exit(2)

SCHEDULE = json.loads((HERE / "test" / "fixtures" / "abl_schedule.json").read_text("utf-8"))
GAMES = {str(g["gamePk"]): g for d in SCHEDULE["dates"] for g in d["games"]}


def convert(pk, **changes):
    game = copy.deepcopy(GAMES[pk])
    game.update(changes)
    return BaseABLManager._convert_stats_game_to_espn_event(game)


def side(event, home_away):
    return next(c for c in event["competitions"][0]["competitors"] if c["homeAway"] == home_away)


def status(event):
    return event["competitions"][0]["status"]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, schedule=None):
        self.schedule = schedule or SCHEDULE
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return FakeResponse(copy.deepcopy(self.schedule))


class TestConversion(unittest.TestCase):
    def test_a_final(self):
        event = convert("900001")
        st = status(event)["type"]
        self.assertEqual((st["state"], st["name"], st["shortDetail"]), ("post", "STATUS_FINAL", "Final"))
        self.assertEqual((side(event, "away")["team"]["abbreviation"], side(event, "away")["score"]), ("BRI", "1"))
        self.assertEqual((side(event, "home")["team"]["abbreviation"], side(event, "home")["score"]), ("SYD", "11"))
        self.assertEqual(side(event, "home")["records"][0]["summary"], "1-0")

    def test_a_seven_inning_game_is_complete_not_final_7(self):
        self.assertEqual(status(convert("900003"))["type"]["shortDetail"], "Final")

    def test_extra_innings_still_say_so(self):
        game = copy.deepcopy(GAMES["900001"])
        game["linescore"]["currentInning"] = 11
        self.assertEqual(status(BaseABLManager._convert_stats_game_to_espn_event(game))
                         ["type"]["shortDetail"], "Final/11")
        game = copy.deepcopy(GAMES["900003"])            # scheduled for seven
        game["linescore"]["currentInning"] = 8
        self.assertEqual(status(BaseABLManager._convert_stats_game_to_espn_event(game))
                         ["type"]["shortDetail"], "Final/8")

    def test_a_live_game(self):
        event = convert("900004")
        st = status(event)
        self.assertEqual((st["type"]["state"], st["period"]), ("in", 5))
        self.assertTrue(st["type"]["shortDetail"].startswith("Bot 5"))
        situation = event["competitions"][0]["situation"]
        self.assertEqual((situation["outs"], situation["count"]["balls"], situation["count"]["strikes"]),
                         (1, 2, 1))
        self.assertEqual((situation["onFirst"], situation["onSecond"], situation["onThird"]),
                         (True, False, True))

    def test_scheduled_and_postponed(self):
        self.assertEqual(status(convert("900005"))["type"]["state"], "pre")
        game = copy.deepcopy(GAMES["900005"])
        game["status"].update(abstractGameState="Final", detailedState="Postponed: Rain")
        st = status(BaseABLManager._convert_stats_game_to_espn_event(game))["type"]
        self.assertEqual((st["state"], st["name"]), ("post", "STATUS_POSTPONED"))

    def test_logos_from_mlbs_image_server(self):
        event = convert("900001")
        self.assertEqual(side(event, "home")["team"]["logo"],
                         "https://midfield.mlbstatic.com/v1/team/4069/spots/96")
        self.assertEqual(side(event, "away")["team"]["logo"],
                         "https://midfield.mlbstatic.com/v1/team/4065/spots/96")


class TestFetch(unittest.TestCase):
    def manager(self):
        mgr = object.__new__(BaseABLManager)
        mgr.logger = logging.getLogger("test_abl_feed")
        mgr.session = FakeSession()
        mgr.headers = {}
        mgr.sport_key = "abl"
        mgr.logo_dir = Path(tempfile.mkdtemp())
        mgr.milb_sport_ids = BaseABLManager._normalise_sport_ids(None) if hasattr(BaseABLManager, "milb_sport_ids") else None
        mgr.abl_sport_ids = BaseABLManager._normalise_sport_ids(None)
        return mgr

    def setUp(self):
        BaseABLManager._logo_retry_at.clear()
        patcher = mock.patch.object(abl_managers, "download_missing_logo", return_value=False)
        self.download = patcher.start()
        self.addCleanup(patcher.stop)

    def test_asks_for_the_abl_only(self):
        mgr = self.manager()
        mgr._fetch_from_mlb_stats_api("2026-11-20", "2026-11-22")
        url = mgr.session.urls[0]
        self.assertIn("sportId=17&leagueId=595", url)
        self.assertIn("startDate=2026-11-20&endDate=2026-11-22", url)
        self.assertIn("hydrate=linescore,team", url)

    def test_the_level_setting_cannot_widen_it(self):
        self.assertEqual(BaseABLManager._normalise_sport_ids([11, 12, 13, 14]), [17])
        self.assertEqual(BaseABLManager._normalise_sport_ids(None), [17])

    def test_fetches_in_season_and_caches_for_thirty_minutes(self):
        mgr = self.manager()
        cache = mock.Mock()
        cache.get.return_value = None
        mgr.cache_manager = cache
        fixed = datetime(2026, 11, 21, 3, 0, tzinfo=pytz.utc)

        class _Fixed(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        with mock.patch.object(abl_managers, "datetime", _Fixed):
            data = mgr._fetch_abl_api_data(use_cache=True)
        self.assertEqual(len(data["events"]), len(GAMES), "November is the season, not the off-season")
        cache.get.assert_called_once_with("abl_schedule_2026", max_age=abl_managers.ABL_SCHEDULE_TTL)
        key, value = cache.set.call_args[0][:2]
        self.assertEqual(key, "abl_schedule_2026")
        self.assertEqual(cache.set.call_args[1]["ttl"], 30 * 60)
        self.assertIn("startDate=2026-11-14&endDate=2026-11-28", mgr.session.urls[0])

    def test_an_unreachable_api_shows_the_last_schedule(self):
        mgr = self.manager()
        stale = {"events": [convert("900001")]}
        cache = mock.Mock()
        cache.get.side_effect = lambda key, max_age=None: None if max_age else stale
        mgr.cache_manager = cache
        mgr._fetch_from_mlb_stats_api = lambda start, end, sport_ids=None: None
        self.assertEqual(mgr._fetch_abl_api_data(use_cache=True), stale)

    def test_missing_logos_are_downloaded_once_then_retried_later(self):
        mgr = self.manager()
        mgr._fetch_from_mlb_stats_api("2026-11-20", "2026-11-22")
        asked = sorted(call.args[2] for call in self.download.call_args_list)
        self.assertEqual(asked, ["ADE", "BRI", "PER", "SYD"])
        self.download.reset_mock()
        mgr._fetch_from_mlb_stats_api("2026-11-20", "2026-11-22")
        self.download.assert_not_called()                 # backing off
        with mock.patch.object(abl_managers.time, "time",
                               return_value=abl_managers.time.time() + 31 * 60):
            mgr._fetch_from_mlb_stats_api("2026-11-20", "2026-11-22")
        self.assertEqual(self.download.call_count, 4)

    def test_a_logo_already_there_is_not_fetched(self):
        from PIL import Image
        mgr = self.manager()
        for abbr in ("ADE", "BRI", "PER", "SYD"):
            Image.new("RGBA", (96, 96), (200, 30, 40, 255)).save(mgr.logo_dir / f"{abbr}.png")
        mgr._fetch_from_mlb_stats_api("2026-11-20", "2026-11-22")
        self.download.assert_not_called()


def _find_core():
    for c in (os.environ.get("LEDMATRIX_CORE", ""), str(HERE.parents[1] / "LEDMatrix"),
              str(HERE.parent / "LEDMatrix"), str(Path.home() / "LEDMatrix")):
        if c and (Path(c) / "src" / "plugin_system").is_dir():
            return Path(c)
    return None


@unittest.skipIf(_find_core() is None, "no LEDMatrix core checkout")
class TestEndToEnd(unittest.TestCase):
    def test_managers_sort_abl_games(self):
        from src.plugin_system.testing.harness import _instantiate
        from src.plugin_system.testing.loading import (build_full_config, load_harness_spec,
                                                       load_manifest)
        from src.plugin_system.testing.visual_display_manager import VisualTestDisplayManager
        spec = load_harness_spec(HERE)
        config = build_full_config(HERE, spec, {})
        config["abl"]["test_mode"] = False
        session = FakeSession()
        try:
            from freezegun import freeze_time
        except ImportError:
            self.skipTest("needs freezegun (the LEDMatrix harness uses it too)")

        # 05:30 UTC on 2026-11-21 (16:30 in Sydney): BRI @ SYD in progress.
        with mock.patch.object(abl_managers, "download_missing_logo", return_value=False), \
                freeze_time("2026-11-21 05:30:00"):
            plugin = _instantiate("abl-scoreboard", load_manifest(HERE), HERE, config, {},
                                  VisualTestDisplayManager(width=192, height=32))
            for mode in ("live", "recent", "upcoming"):
                manager = getattr(plugin, f"abl_{mode}")
                manager.session = session
                manager.cache_manager.get = lambda *a, **k: None
                manager.last_update = 0
                manager.update()
            live = plugin.abl_live.live_games
            recent = plugin.abl_recent.games_list
            upcoming = plugin.abl_upcoming.games_list

        self.assertFalse(plugin.mlb_enabled or plugin.ncaa_baseball_enabled)
        self.assertTrue(plugin.abl_enabled)
        self.assertEqual([(g["away_abbr"], g["home_abbr"]) for g in live], [("BRI", "SYD")])
        self.assertEqual((live[0]["away_score"], live[0]["home_score"]), ("4", "3"))
        pairs = lambda games: [(g["away_abbr"], g["home_abbr"]) for g in games]
        self.assertTrue(recent and all(g["is_final"] for g in recent), pairs(recent))
        self.assertIn(("BRI", "SYD"), pairs(recent), "the favourite's result is shown")
        self.assertTrue(upcoming and all(g["is_upcoming"] for g in upcoming), pairs(upcoming))
        self.assertTrue(session.urls)
        self.assertTrue(all("leagueId=595" in u for u in session.urls))
        self.assertEqual(plugin.modes and sorted({m.split("_")[0] for m in plugin.modes}), ["abl"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
