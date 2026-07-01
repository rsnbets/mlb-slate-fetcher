# mlb-slate-fetcher

The single SportsGameOdds puller for the whole stack. Every ~20 minutes it pulls
today's pre-game MLB props (all games / all books) and publishes the raw slate as
`mlb_slate.json` to the private **rsnbets/mlb-odds** repo.

Every downstream tool (HR projector, MLB_EV, K-prop, Underdog scanner) reads that
one shared file instead of calling SGO itself — **one pull, many consumers.**

This repo is **public on purpose**: GitHub Actions minutes are free for public
repos, so a tight fetch cadence costs nothing. It contains **no model logic** — just
the SGO fetch. All proprietary projection code stays in its private repos.

- `fetch.py` — pull SGO → write `mlb_slate.json`
- `.github/workflows/fetch.yml` — cron `*/20 * * * *` + manual dispatch → publish to mlb-odds

Secrets (encrypted; never printed): `SGO_API_KEY`, `SLATE_WRITE_KEY` (mlb-odds deploy key).
