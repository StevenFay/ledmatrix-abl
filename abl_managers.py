import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

import pytz

from abl_baseball import Baseball, BaseballLive, BaseballRecent
from sports import SportsUpcoming
from src.common.sports_helpers import logo_needs_refresh
from src.logo_downloader import LogoDownloader, download_missing_logo

# The Australian Baseball League is in the MLB Stats API (ESPN does not
# carry it): sport 17 is the winter leagues, league 595 the ABL among them.
MLB_STATS_BASE_URL = "https://statsapi.mlb.com/api/v1"
ABL_SPORT_ID = 17
ABL_LEAGUE_ID = 595
DEFAULT_ABL_SPORT_IDS = [ABL_SPORT_ID]
#: Team logos, by Stats API team id (a PNG; the SVG at mlbstatic.com/team-logos
#: can't be decoded by Pillow).
ABL_LOGO_URL = "https://midfield.mlbstatic.com/v1/team/{team_id}/spots/96"
#: ABL games are scheduled and dated in Australia.
ABL_TIMEZONE = "Australia/Sydney"
#: How long the recent/upcoming schedule is reused before asking again.
ABL_SCHEDULE_TTL = 30 * 60
#: Seconds before a team whose logo failed to download is tried again.
ABL_LOGO_RETRY_SECONDS = 30 * 60


class BaseABLManager(Baseball):
    """Base class for ABL managers with common functionality.

    Unlike other sports that use ESPN's scoreboard API, ABL uses the MLB Stats API
    (statsapi.mlb.com) since ESPN does not provide a ABL scoreboard endpoint.
    Data is fetched from the Stats API and converted to ESPN-compatible format
    so it works seamlessly with the SportsCore extraction pipeline.
    """

    # Class variables shared across all ABL manager instances (Live/Recent/Upcoming)
    # so they can share API data and coordinate warning throttling
    _no_data_warning_logged: ClassVar[bool] = False
    _last_warning_time: ClassVar[float] = 0
    _warning_cooldown: ClassVar[int] = 60  # Only log warnings once per minute
    _shared_data: ClassVar[Optional[Dict]] = None
    _last_shared_update: ClassVar[float] = 0

    def __init__(self, config: Dict[str, Any], display_manager, cache_manager):
        self.logger = logging.getLogger("ABL")
        super().__init__(
            config=config,
            display_manager=display_manager,
            cache_manager=cache_manager,
            logger=self.logger,
            sport_key="abl",
        )

        # Check display modes to determine what data to fetch
        display_modes = self.mode_config.get("display_modes", {})
        self.live_enabled = display_modes.get("abl_live", False)
        self.recent_enabled = display_modes.get("abl_recent", False)
        self.upcoming_enabled = display_modes.get("abl_upcoming", False)

        # ABL sport IDs to fetch (configurable)
        self.abl_sport_ids = self._normalise_sport_ids(
            self.mode_config.get("sport_ids")
        )

        self.logger.info(
            f"Initialized ABL manager with display dimensions: {self.display_width}x{self.display_height}"
        )
        self.logger.info(f"Logo directory: {self.logo_dir}")
        self.logger.info(
            f"Display modes - Live: {self.live_enabled}, Recent: {self.recent_enabled}, Upcoming: {self.upcoming_enabled}"
        )
        self.league = "minor-league-baseball"

    @staticmethod
    def _normalise_sport_ids(raw) -> List[int]:
        """Always the ABL's sport (17). Kept so the shared code that passes a
        ``sport_ids`` setting still works; the league is fixed."""
        return list(DEFAULT_ABL_SPORT_IDS)

    @staticmethod
    def _convert_stats_game_to_espn_event(game: Dict) -> Dict:
        """Convert a single MLB Stats API game to ESPN event format.

        This allows the SportsCore extraction pipeline (_extract_game_details_common
        and _extract_game_details) to process ABL data identically to ESPN data.
        """
        home = game.get("teams", {}).get("home", {})
        away = game.get("teams", {}).get("away", {})
        home_team = home.get("team", {})
        away_team = away.get("team", {})
        status_obj = game.get("status", {})
        linescore = game.get("linescore", {})

        # Map status. abstractGameState alone is not enough: the Stats API
        # files Postponed (D) and Cancelled (C) under "Final", Suspended (T)
        # under "Live" and Warmup under "Live" too. Taken at face value a
        # rainout became a "Final 0-0" on Recent, a suspended game froze in
        # the live rotation all day, and a warming-up game drew "▼0". The
        # detailedState says what actually happened, so it decides first,
        # using ESPN's own status names so the shared extractor treats both
        # sources alike (not final, not live, not upcoming).
        abstract_state = status_obj.get("abstractGameState", "Preview")
        detailed_state = status_obj.get("detailedState", "") or ""
        detailed_lower = detailed_state.lower()

        if detailed_lower.startswith("postponed"):
            abstract_state = "NotPlayed"
            state = "post"
            status_name = "STATUS_POSTPONED"
        elif detailed_lower.startswith("cancel"):
            abstract_state = "NotPlayed"
            state = "post"
            status_name = "STATUS_CANCELED"
        elif detailed_lower.startswith("suspended"):
            abstract_state = "NotPlayed"
            state = "post"
            status_name = "STATUS_SUSPENDED"
        elif detailed_lower.startswith("warmup"):
            abstract_state = "Preview"
            state = "pre"
            status_name = "STATUS_SCHEDULED"
        elif abstract_state == "Live":
            state = "in"
            status_name = "STATUS_IN_PROGRESS"
        elif abstract_state == "Final":
            state = "post"
            status_name = "STATUS_FINAL"
        else:
            state = "pre"
            status_name = "STATUS_SCHEDULED"

        # Build inning detail text
        current_inning = linescore.get("currentInning") or None
        inning_state = linescore.get("inningState", "")
        inning_ordinal = linescore.get("currentInningOrdinal", f"{current_inning}" if current_inning else "")

        # Scheduled length: ABL doubleheader games are seven innings, which
        # read as "Final/7" when nine was assumed.
        try:
            scheduled = int(game.get("scheduledInnings") or 9)
        except (TypeError, ValueError):
            scheduled = 9
        try:
            current_inning = int(current_inning) if current_inning is not None else None
        except (TypeError, ValueError):
            current_inning = None

        if abstract_state == "Final":
            detail_text = "Final"
            short_detail = "Final"
            if current_inning is not None and current_inning != scheduled:
                detail_text = f"Final/{current_inning}"
                short_detail = f"Final/{current_inning}"
        elif abstract_state == "Live":
            # An empty inningState (the first pitch not yet thrown) is the
            # top of the inning, matching the ESPN parser's default.
            half = "Bot" if inning_state.lower().startswith("bot") else "Top"
            if "mid" in inning_state.lower():
                half = "Mid"
            elif "end" in inning_state.lower():
                half = "End"
            detail_text = f"{inning_state} {inning_ordinal}"
            short_detail = f"{half} {inning_ordinal}"
        else:
            detail_text = detailed_state or "Scheduled"
            short_detail = detail_text

        # Build records
        home_record = home.get("leagueRecord", {})
        away_record = away.get("leagueRecord", {})
        home_record_summary = ""
        away_record_summary = ""
        if home_record.get("wins") is not None:
            home_record_summary = f"{home_record.get('wins', 0)}-{home_record.get('losses', 0)}"
        if away_record.get("wins") is not None:
            away_record_summary = f"{away_record.get('wins', 0)}-{away_record.get('losses', 0)}"

        # Build situation (for live games)
        situation = {}
        if abstract_state == "Live" and linescore:
            offense = linescore.get("offense", {})
            situation = {
                "outs": linescore.get("outs", 0),
                "onFirst": bool(offense.get("first")),
                "onSecond": bool(offense.get("second")),
                "onThird": bool(offense.get("third")),
                "count": {
                    "balls": linescore.get("balls", 0),
                    "strikes": linescore.get("strikes", 0),
                },
            }

        # Build ESPN-compatible event structure
        event = {
            "id": str(game.get("gamePk", "")),
            "date": game.get("gameDate", ""),
            "name": f"{away_team.get('name', '')} at {home_team.get('name', '')}",
            "competitions": [
                {
                    "competitors": [
                        {
                            "homeAway": "home",
                            "team": {
                                "id": str(home_team.get("id", "")),
                                "abbreviation": home_team.get(
                                    "abbreviation",
                                    home_team.get("name", "???")[:3].upper(),
                                ),
                                "name": home_team.get("name", ""),
                                "displayName": home_team.get("name", ""),
                                "shortDisplayName": home_team.get("shortName", home_team.get("name", "")),
                                "logo": ABL_LOGO_URL.format(team_id=home_team["id"])
                                if home_team.get("id") else "",
                            },
                            "score": str(home.get("score", 0)),
                            "records": [{"summary": home_record_summary}]
                            if home_record_summary
                            else [],
                            "id": str(home_team.get("id", "")),
                        },
                        {
                            "homeAway": "away",
                            "team": {
                                "id": str(away_team.get("id", "")),
                                "abbreviation": away_team.get(
                                    "abbreviation",
                                    away_team.get("name", "???")[:3].upper(),
                                ),
                                "name": away_team.get("name", ""),
                                "displayName": away_team.get("name", ""),
                                "shortDisplayName": away_team.get("shortName", away_team.get("name", "")),
                                "logo": ABL_LOGO_URL.format(team_id=away_team["id"])
                                if away_team.get("id") else "",
                            },
                            "score": str(away.get("score", 0)),
                            "records": [{"summary": away_record_summary}]
                            if away_record_summary
                            else [],
                            "id": str(away_team.get("id", "")),
                        },
                    ],
                    "status": {
                        "type": {
                            "name": status_name,
                            "state": state,
                            "detail": detail_text,
                            "shortDetail": short_detail,
                            "completed": abstract_state == "Final",
                        },
                        # 1, not 0: a live game with no inning yet would
                        # otherwise draw "▲0" (the scorebug's own default
                        # never applies because the key is present).
                        "period": current_inning or 1,
                        "displayClock": "0:00",
                    },
                    "situation": situation or {},
                    "odds": [],
                    "series": {},
                }
            ],
        }

        return event

    def _fetch_from_mlb_stats_api(
        self, start_date: str, end_date: str, sport_ids: Optional[List[int]] = None
    ) -> Optional[Dict]:
        """Fetch ABL games from the MLB Stats API and convert to ESPN format.

        Uses the startDate/endDate parameters to fetch the entire range in a
        single request instead of one request per date.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            sport_ids: ABL sport IDs to fetch (default: self.abl_sport_ids)

        Returns:
            ESPN-compatible dict with {"events": [...]} or None on failure
        """
        if sport_ids is None:
            sport_ids = self.abl_sport_ids

        all_events = []
        sport_id_str = ",".join(str(s) for s in sport_ids)

        url = (
            f"{MLB_STATS_BASE_URL}/schedule"
            f"?sportId={sport_id_str}&leagueId={ABL_LEAGUE_ID}"
            f"&startDate={start_date}&endDate={end_date}"
            f"&hydrate=linescore,team"
        )
        try:
            response = self.session.get(
                url, headers=self.headers, timeout=30
            )
            response.raise_for_status()
            data = response.json()

            for date_entry in data.get("dates", []):
                for game in date_entry.get("games", []):
                    event = self._convert_stats_game_to_espn_event(game)
                    all_events.append(event)

        except Exception as e:
            self.logger.error(
                f"Failed to fetch ABL data for {start_date} to {end_date}: {e}"
            )
            return None

        self._download_missing_logos(all_events)

        if all_events:
            self.logger.info(
                f"Fetched {len(all_events)} ABL games from MLB Stats API"
            )
        else:
            self.logger.debug("No ABL games found from MLB Stats API")

        return {"events": all_events}

    #: Retry times by team, shared by the Live, Recent and Upcoming managers.
    _logo_retry_at: ClassVar[Dict[str, float]] = {}

    def _download_missing_logos(self, events: List[Dict]) -> None:
        """Download any team's logo not yet in the logo folder.

        The switch screens download a logo when they first draw a team, but
        the scroll and Vegas cards only read the folder -- for MLB and the
        minor leagues LEDMatrix ships the logos, for the ABL it has none, so a
        board that only scrolls would never get them. Fetching here, with the
        games, covers both. A failed download is retried after 30 minutes; a
        placeholder left by one is replaced then too.
        """
        now = time.time()
        for event in events:
            for competitor in event.get("competitions", [{}])[0].get("competitors", []):
                team = competitor.get("team") or {}
                abbr, url = team.get("abbreviation"), team.get("logo")
                if not abbr or not url:
                    continue
                path = Path(self.logo_dir) / f"{LogoDownloader.normalize_abbreviation(abbr)}.png"
                if path.exists() and not logo_needs_refresh(path):
                    continue
                if now < self._logo_retry_at.get(abbr, 0.0):
                    continue
                self._logo_retry_at[abbr] = now + ABL_LOGO_RETRY_SECONDS
                try:
                    download_missing_logo(self.sport_key, team.get("id", ""), abbr, path, url)
                except Exception as e:  # never let a logo stop the scores
                    self.logger.debug(f"Logo download failed for {abbr}: {e}")

    def _fetch_todays_games(self) -> Optional[Dict]:
        """Override SportsCore's ESPN-based fetch with MLB Stats API fetch."""
        # The Stats API files games under their local date -- Australian for
        # the ABL. The US Eastern date the minor-league code used is up to a
        # day behind, so an afternoon game here wasn't asked for until it was
        # over. Use the Australian date, with a one-day lookback for a game
        # still running past midnight.
        now = datetime.now(pytz.timezone(ABL_TIMEZONE))
        today = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        self.logger.debug(f"Fetching ABL games for {yesterday} to {today}")
        return self._fetch_from_mlb_stats_api(yesterday, today)

    def _fetch_abl_api_data(self, use_cache: bool = True) -> Optional[Dict]:
        """Fetch ABL season schedule data using MLB Stats API.

        Returns cached data if available, otherwise fetches from MLB Stats API.
        """
        now = datetime.now(pytz.timezone(ABL_TIMEZONE))
        # The ABL season runs November to February, so it is named by the
        # year it starts in.
        season_year = now.year if now.month >= 7 else now.year - 1
        cache_key = f"{self.sport_key}_schedule_{season_year}"

        # Check cache first
        if use_cache:
            cached_data = self.cache_manager.get(cache_key, max_age=ABL_SCHEDULE_TTL)
            if cached_data:
                if isinstance(cached_data, dict) and "events" in cached_data:
                    self.logger.debug(f"Using cached ABL schedule for {season_year}")
                    return cached_data
                elif isinstance(cached_data, list):
                    self.logger.debug(
                        f"Using cached ABL schedule for {season_year} (legacy format)"
                    )
                    return {"events": cached_data}
                else:
                    self.logger.warning(
                        f"Invalid cached data format for {season_year}: {type(cached_data)}"
                    )
                    self.cache_manager.clear_cache(cache_key)

        # No season gate: the minor-league code fetched only April-September,
        # which is the ABL's off-season, so it never asked during the season.
        # Out of season the window is simply empty.

        # Fetch recent + upcoming games (7 days back, 7 days ahead)
        start_date = (now + timedelta(days=-7)).strftime("%Y-%m-%d")
        end_date = (now + timedelta(days=7)).strftime("%Y-%m-%d")

        data = self._fetch_from_mlb_stats_api(start_date, end_date)

        # Cached for 30 minutes (the minor-league code kept it 4 hours), so a
        # result reaches the Recent screen soon after the game.
        if data and data.get("events"):
            self.cache_manager.set(cache_key, data, ttl=ABL_SCHEDULE_TTL)
            self.logger.info(
                f"Cached {len(data['events'])} ABL events for {season_year}"
            )
        elif data is None:
            # The Stats API couldn't be reached: show the last schedule we
            # have, however old, rather than empty Recent/Upcoming screens.
            try:
                stale = self.cache_manager.get(cache_key, max_age=None)
            except TypeError:  # a cache without max_age=None
                stale = None
            if isinstance(stale, dict) and stale.get("events"):
                self.logger.warning("ABL schedule fetch failed; showing the last one fetched")
                return stale

        return data

    def _fetch_data(self) -> Optional[Dict]:
        """Fetch cached schedule data. Subclasses may override."""
        return self._fetch_abl_api_data(use_cache=True)


class ABLLiveManager(BaseABLManager, BaseballLive):
    """Manager for live ABL games."""

    def __init__(self, config: Dict[str, Any], display_manager, cache_manager):
        super().__init__(config, display_manager, cache_manager)
        self.logger = logging.getLogger("ABLLiveManager")

        if self.test_mode:
            # A test game: Brisbane Bandits at Sydney Blue Sox
            self.current_game = {
                "id": "testABL001",
                "home_abbr": "SYD",
                "home_id": "4069",
                "away_abbr": "BRI",
                "away_id": "4065",
                "home_score": "5",
                "away_score": "3",
                "inning": 6,
                "inning_half": "bottom",
                "balls": 2,
                "strikes": 1,
                "outs": 1,
                "bases_occupied": [True, False, True],
                "home_logo_path": Path(self.logo_dir, "SYD.png"),
                "away_logo_path": Path(self.logo_dir, "BRI.png"),
                "is_live": True,
                "is_final": False,
                "is_upcoming": False,
                "is_halftime": False,
                "home_logo_url": "",
                "away_logo_url": "",
                "series_summary": "",
                "status_text": "Bot 6",
            }
            self.live_games = [self.current_game]
            self.logger.info("Initialized ABLLiveManager with test game: BRI vs SYD")
        else:
            self.logger.info("Initialized ABLLiveManager in live mode")

    def _fetch_data(self) -> Optional[Dict]:
        """Live games fetch only today's games, not entire season."""
        return self._fetch_todays_games()


class ABLRecentManager(BaseABLManager, BaseballRecent):
    """Manager for recently completed ABL games."""

    def __init__(self, config: Dict[str, Any], display_manager, cache_manager):
        super().__init__(config, display_manager, cache_manager)
        self.logger = logging.getLogger("ABLRecentManager")
        self.logger.info(
            f"Initialized ABLRecentManager with {len(self.favorite_teams)} favorite teams"
        )


class ABLUpcomingManager(BaseABLManager, SportsUpcoming):
    """Manager for upcoming ABL games."""

    def __init__(self, config: Dict[str, Any], display_manager, cache_manager):
        super().__init__(config, display_manager, cache_manager)
        self.logger = logging.getLogger("ABLUpcomingManager")
        self.logger.info(
            f"Initialized ABLUpcomingManager with {len(self.favorite_teams)} favorite teams"
        )
