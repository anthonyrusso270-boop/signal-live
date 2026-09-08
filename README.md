# signal-live

Experiments in always-on market monitors: a GitHub Actions cron pulls public data, commits the
snapshot, and GitHub Pages serves a static page that reads it. No server, no keys in the repo.

**Kalshi macro monitor** — `pull_kalshi.py` reproduces the six kalshi_lab dashboards (Fed path,
inflation distribution, labour deterioration, growth/recession, Treasury curve, energy) from
Kalshi's public read-only API every six hours; `index.html` renders `data.json` and the
accumulating `history.json`.

Live: https://anthonyrusso270-boop.github.io/signal-live/

**MLB odds ledger** — `baseball/snapshot.py` records Bovada's MLB board (moneyline, runline, total, first-five markets) every two hours in season; `baseball/grade.py` settles closing lines against MLB Stats API box scores and keeps a cumulative ledger with calibration. `baseball/index.html` renders the board and the ledger.

Live: https://anthonyrusso270-boop.github.io/signal-live/baseball/
