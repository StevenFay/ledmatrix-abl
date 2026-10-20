# Changelog

## 1.0.0 (2026-10-20)

First release, adapted from ChuckBuilds' baseball-scoreboard 1.66.0
(ledmatrix-plugins d43e302).

- Its minor-league path (MLB Stats API) reads the Australian Baseball League:
  sport 17, league 595.
- Fetched all season (the minor-league code stopped outside April-September),
  by Australian date, refreshed every 30 minutes; the last schedule stays on
  screen if the API can't be reached.
- Team logos from MLB's image server, downloaded with the games so the scroll
  and Vegas cards have them; retried after 30 minutes on failure.
- Seven-inning games read "Final", not "Final/7".
- Day/month dates by default.
- MLB, NCAA Baseball, the minor-league level picker and odds removed from the
  settings. Plugin id abl-scoreboard; modes abl_live / abl_recent /
  abl_upcoming; modules abl_baseball and abl_logo_manager so it can sit beside
  the Baseball Scoreboard.
