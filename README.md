# ABL Scoreboard

Live, recent and upcoming **Australian Baseball League** games on your
[LEDMatrix](https://github.com/ChuckBuilds/LEDMatrix) display: the baseball
scorebug (inning, outs, bases and count), finals and upcoming games, in switch,
scroll or Vegas mode.

This is ChuckBuilds' [Baseball Scoreboard](https://github.com/ChuckBuilds/ledmatrix-plugins/tree/main/plugins/baseball-scoreboard)
(1.66.0) with its minor-league path pointed at the ABL. ESPN doesn't carry the
ABL; the MLB Stats API does, and that plugin already reads minor-league games
from it. MLB and NCAA Baseball are switched off, so this is an ABL-only board.

## Install

1. In the LEDMatrix web interface, open **Plugin Manager**.
2. Under **Install from GitHub**, choose **Install Single Plugin** and paste
   `https://github.com/StevenFay/ledmatrix-abl`.
3. Accept the unverified-plugin warning, then restart the display.
4. Open **ABL Scoreboard**, add favourite teams under **ABL Settings**, and save.

## Teams

Favourite and excluded teams use the team codes:

| Code | Team |
| --- | --- |
| `ADE` | Adelaide Giants |
| `BRI` | Brisbane Bandits |
| `PER` | Perth Heat |
| `SYD` | Sydney Blue Sox |

Those four appear in the 2026-27 fixtures, which start on 19 November 2026.
Any other side uses the code the MLB Stats API gives it.

## What's different from the Baseball Scoreboard

- **The ABL, not the minor leagues:** the Stats API is asked for sport 17
  (the winter leagues), league 595 (the ABL).
- **All season:** the minor-league code only fetched April to September,
  which is the ABL's off-season. This fetches whenever there are games.
- **Australian dates:** games are looked up by their Australian date. The
  US Eastern date the minor-league code used is up to a day behind, which
  missed afternoon games here.
- **Fresher results:** the schedule behind the Recent and Upcoming screens is
  refreshed every 30 minutes. If the Stats API can't be reached, the last
  schedule fetched stays on screen.
- **Seven-inning games:** doubleheader games scheduled for seven innings show
  "Final", not "Final/7". Extra innings still show, e.g. "Final/11".
- **Team logos** are downloaded from MLB's image server
  (`midfield.mlbstatic.com/v1/team/<id>/spots/96`) whenever games are fetched,
  so the scroll and Vegas cards have them too. A failed download is retried
  after 30 minutes; until then the team shows its code.
- **Day/month dates** (21/11, 21 Nov) by default.
- **Hidden:** the MLB and NCAA Baseball sections, the minor-league level
  picker, and betting odds (from ESPN, which has no ABL games).
- **Names:** plugin id `abl-scoreboard`, display modes `abl_live`, `abl_recent`
  and `abl_upcoming`, settings under `abl`. The `baseball` and `logo_manager`
  modules are `abl_baseball` and `abl_logo_manager`, so this can be installed
  alongside the Baseball Scoreboard.

Everything else (durations, scroll speed, celebrations, colours, layout
offsets) works as in the Baseball Scoreboard; its
[README](https://github.com/ChuckBuilds/ledmatrix-plugins/blob/main/plugins/baseball-scoreboard/README.md)
describes each setting. Its pitcher/batter, player card and line-score screens
need ESPN's play data, which ABL games don't have, so leave those off.

## Development

The tests run against a LEDMatrix core checkout:

```bash
PYTHONPATH=~/LEDMatrix python3 test_abl_feed.py
```

`test/fixtures/abl_schedule.json` holds Stats API games built from real
2025-26 opening-round results, re-dated to November 2026. `test/harness.json`
and `test/fixtures/mock.json` drive LEDMatrix's safety harness
(`scripts/check_plugin.py`), frozen at 05:30 UTC on 21 November 2026.

## Credits and licence

Based on ChuckBuilds' [Baseball Scoreboard](https://github.com/ChuckBuilds/ledmatrix-plugins/tree/main/plugins/baseball-scoreboard)
and licensed, like it, under the GNU GPL v3 (see `LICENSE`). Game data and team
logos come from MLB.
