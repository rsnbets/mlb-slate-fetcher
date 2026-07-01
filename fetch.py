"""
fetch.py — pull today's MLB props from SportsGameOdds and write mlb_slate.json.

This is the ONLY component that calls SportsGameOdds. It publishes the raw slate
(every game / prop / book) to the private rsnbets/mlb-odds repo so every downstream
tool (HR projector, MLB_EV, K-prop, Underdog scanner, …) reads ONE shared pull
instead of hitting SGO itself. No model logic lives here — this repo is public so
its Actions minutes are free; the proprietary projectors stay in their private repos.

SGO: GET /v2/events?leagueID=MLB, auth via X-Api-Key header (env SGO_API_KEY).
Pre-game only (startsAfter=now), bounded to today's ET slate. Paginated by nextCursor.
"""

import os
import sys
import json
import datetime
import requests

SGO_BASE = "https://api.sportsgameodds.com/v2"
SGO_LEAGUE = "MLB"
SGO_KEY = os.environ.get("SGO_API_KEY", "")
OUT = os.environ.get("SLATE_OUT", "mlb_slate.json")


def _fetch_events():
    """Today's upcoming MLB events (with odds), paginated. 1 event = 1 SGO entity.

    PRE-GAME only: startsAfter=now drops games already underway (also trims entity
    cost as the day goes on). startsBefore is bounded to today's ET game-date (~4am
    ET tomorrow) so a player who plays both days can't overwrite today's slate.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    et_date = (now - datetime.timedelta(hours=4)).date()          # EDT = UTC-4 in season
    end = et_date + datetime.timedelta(days=1)                     # ~4am ET tomorrow
    params_base = {
        "leagueID": SGO_LEAGUE,
        "startsAfter": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "startsBefore": f"{end.isoformat()}T08:00:00Z",
        "limit": 50,
    }
    headers = {"X-Api-Key": SGO_KEY}
    events, cursor = [], None
    for _ in range(10):                                            # pagination safety cap
        params = dict(params_base)
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{SGO_BASE}/events/", headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        events.extend(data.get("data", []))
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return events


def main():
    if not SGO_KEY:
        print("SGO_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    events = _fetch_events()
    slate = {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "league": SGO_LEAGUE,
        "source": "sportsgameodds",
        "pre_game_only": True,
        "event_count": len(events),
        "events": events,
    }
    with open(OUT, "w") as f:
        json.dump(slate, f)
    print(f"wrote {OUT}: {len(events)} events, {os.path.getsize(OUT)} bytes")


if __name__ == "__main__":
    main()
