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
        # Full alternate-line ladders per book (byBookmaker.<bk>.altLines[]).
        # Off by default in v2; verified available on the Rookie plan 2026-07-27.
        # ~2x response size, same entity cost.
        "includeAltLines": "true",
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



def _trim_for_boards(events):
    """A small PUBLIC odds feed for client-side boards: per market -> per player ->
    ALL lines, each with SGO fair + per-book over/under. Browser-fetchable
    (served from gh-pages with permissive CORS).

    Shape per player:
      { "line": <main line>, "fair_over": ..., "books": {...},   # legacy: main line
        "lines": { "0.5": {"fair_over": ..., "books": {bk: {"over","under"}}},
                   "1.5": {...}, ... } }                          # every alt line
    The legacy top-level fields mirror the MAIN line (most books) so existing
    consumers (Dinger live layer) keep working; per-line consumers (hit/K EV
    tools price each alt line separately) read `lines`."""
    markets = {}
    for e in events:
        players = e.get("players", {}) or {}
        odds = e.get("odds", {}) or {}
        teams = e.get("teams", {}) or {}
        ev_ctx = {
            "id": e.get("eventID"),
            "home": ((teams.get("home") or {}).get("names") or {}).get("long"),
            "away": ((teams.get("away") or {}).get("names") or {}).get("long"),
            "start": (e.get("status") or {}).get("startsAt"),
        }
        for oid, o in odds.items():
            if o.get("betTypeID") != "ou" or o.get("sideID") != "over":
                continue
            stat = o.get("statID")
            name = (players.get(o.get("playerID"), {}) or {}).get("name")
            if not stat or not name:
                continue
            under = odds.get(o.get("opposingOddID", ""), {}) or {}
            lk = str(o.get("bookOverUnder") or o.get("fairOverUnder"))
            player_entry = markets.setdefault(stat, {}).setdefault(name, {})
            player_entry.setdefault("ev", ev_ctx)   # game context for event-aware consumers
            lines = player_entry.setdefault("lines", {})
            entry = lines.setdefault(lk, {"books": {}})
            # SGO fair belongs to this market object. Its fair LINE can differ from
            # the booked line (SGO quotes fair at its own consensus number, e.g.
            # fair@1 while books post 0.5) — expose it so consumers can adjust.
            if o.get("fairOddsAvailable"):
                entry["fair_over"] = o.get("fairOdds")
                fl = str(o.get("fairOverUnder") or lk)
                if fl != lk:
                    entry["fair_line"] = fl
            # books file into this object's line; a book on a different number is
            # tagged with its own "ou" rather than silently mixed in. Each book's
            # ALT ladder (altLines[]) files under the alt line's own key, so the
            # lines{} map carries the full ladder per market.
            def _file(bd, bk, side):
                if bd.get("available"):
                    b = entry["books"].setdefault(bk, {})
                    b[side] = bd.get("odds")
                    if str(bd.get("overUnder", lk)) != lk and "ou" not in b:
                        b["ou"] = str(bd.get("overUnder"))
                for alt in (bd.get("altLines") or []):
                    if not alt.get("available"):
                        continue
                    alk = str(alt.get("overUnder"))
                    if alk in ("", "None"):
                        continue
                    ae = lines.setdefault(alk, {"books": {}})
                    ae["books"].setdefault(bk, {})[side] = alt.get("odds")
            for bk, bd in (o.get("byBookmaker", {}) or {}).items():
                _file(bd, bk, "over")
            for bk, bd in (under.get("byBookmaker", {}) or {}).items():
                _file(bd, bk, "under")
    # prune bookless lines; mirror the main (most-booked) line into legacy fields
    for stat, players_d in markets.items():
        for name in list(players_d):
            entry = players_d[name]
            lines = {lk: v for lk, v in entry.get("lines", {}).items() if v.get("books")}
            if not lines:
                del players_d[name]
                continue
            entry["lines"] = lines
            main = max(lines, key=lambda lk: len(lines[lk]["books"]))
            entry["line"] = main
            entry["fair_over"] = lines[main].get("fair_over")
            entry["books"] = lines[main]["books"]
    return markets


def _trim_periods(events):
    """Compact team run markets for the early-game periods SGO now covers
    (2026-08 expansion): 1i = first-inning runs (NRFI/YRFI), 1h = first 5
    innings (F5). Published as a NEW top-level key so existing consumers of
    `markets` (full-game player props) are untouched.

    Shape:
      { "1i": { "<eventID>": { "away": ..., "home": ..., "start": ...,
                "runs": { "all"|"home"|"away": {
                    "line": "0.5", "fair_over": ...,
                    "books": { bk: {"over": ..., "under": ...} } } } } },
        "1h": {...} }
    """
    PERIODS = ("1i", "1h")
    out = {p: {} for p in PERIODS}
    for e in events:
        odds = e.get("odds", {}) or {}
        teams = e.get("teams", {}) or {}
        for oid, o in odds.items():
            if (o.get("periodID") not in PERIODS or o.get("statID") != "points"
                    or o.get("betTypeID") != "ou" or o.get("sideID") != "over"):
                continue
            entity = o.get("statEntityID")
            if entity not in ("all", "home", "away"):
                continue
            under = odds.get(o.get("opposingOddID", ""), {}) or {}
            ev = out[o["periodID"]].setdefault(e.get("eventID"), {
                "away": ((teams.get("away") or {}).get("names") or {}).get("long"),
                "home": ((teams.get("home") or {}).get("names") or {}).get("long"),
                "start": (e.get("status") or {}).get("startsAt"),
                "runs": {},
            })
            entry = ev["runs"].setdefault(entity, {"books": {}})
            entry["line"] = str(o.get("bookOverUnder") or o.get("fairOverUnder"))
            if o.get("fairOddsAvailable"):
                entry["fair_over"] = o.get("fairOdds")
            for side, obj in (("over", o), ("under", under)):
                for bk, bd in (obj.get("byBookmaker", {}) or {}).items():
                    if bd.get("available"):
                        entry["books"].setdefault(bk, {})[side] = bd.get("odds")
    # prune bookless entries / empty events
    for p in PERIODS:
        for eid in list(out[p]):
            runs = {k: v for k, v in out[p][eid]["runs"].items() if v.get("books")}
            if runs:
                out[p][eid]["runs"] = runs
            else:
                del out[p][eid]
    return out


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
    board = {"fetched_at": slate["fetched_at"], "markets": _trim_for_boards(events),
             "periods": _trim_periods(events)}
    with open("board_odds.json", "w") as bf:
        json.dump(board, bf)
    print(f"wrote board_odds.json: {sum(len(v) for v in board['markets'].values())} player-markets, "
          f"{sum(len(v) for v in board['periods'].values())} period-events, "
          f"{os.path.getsize('board_odds.json')} bytes")


if __name__ == "__main__":
    main()
