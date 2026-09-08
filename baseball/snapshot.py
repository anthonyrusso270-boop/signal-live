"""MLB odds snapshot — Bovada's public board (no key) → odds/<date>.json + board.json.

Keeps the markets that matter for a ledger: full-game Moneyline / Runline / Total, and the
First-5-Innings Moneyline (2-way, tie = push), 3-Way Moneyline, Runline and Total. One row per
outcome per pull, so line HISTORY accumulates from board posting to first pitch; the closing
line is the last pre-match snapshot (grade.py).

Dates are Eastern (MLB's game day). Safe to run any time: appends only; if Bovada refuses the
runner it writes the refusal into board.json so the page says so instead of going stale silently.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).resolve().parent
ODDS = HERE / "odds"; ODDS.mkdir(exist_ok=True)
ET = ZoneInfo("America/New_York")
BASE = "https://www.bovada.lv/services/sports/event/coupon/events/A/description"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
KEEP = {("Game", "Moneyline"), ("Game", "Runline"), ("Game", "Total"),
        ("First 5 Innings", "Moneyline"), ("First 5 Innings", "3-Way Moneyline"), ("First 5 Innings", "Runline"), ("First 5 Innings", "Total")}
NOW = datetime.now(timezone.utc)


def american_to_prob(a):
    if a is None:
        return None
    a = 100.0 if str(a).upper() == "EVEN" else float(a)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def devig(ps):
    s = sum(p for p in ps if p is not None)
    return [None if p is None else p / s for p in ps], s - 1


def pull_board():
    r = requests.get(f"{BASE}/baseball/mlb", params={"preMatchOnly": "true", "lang": "en"}, headers=UA, timeout=30)
    r.raise_for_status()
    js = r.json()
    return js[0]["events"] if js else []


def pull_event(link):
    r = requests.get(f"{BASE}{link}", params={"lang": "en"}, headers=UA, timeout=30)
    r.raise_for_status()
    ev = r.json()[0]["events"][0]
    rows = []
    for g in ev["displayGroups"]:
        for m in g["markets"]:
            period = (m.get("period") or {}).get("description")
            if (period, m["description"]) not in KEEP or g["description"] not in ("Game Lines",) and m["description"] != "3-Way Moneyline":
                continue
            for o in m.get("outcomes", []):
                price = o.get("price") or {}
                rows.append({"period": period, "market": m["description"], "outcome": o["description"],
                             "american": price.get("american"), "handicap": price.get("handicap"),
                             "implied": american_to_prob(price.get("american"))})
    return rows


def load(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def build_board(snapshots_by_date):
    """Latest vs opening quote per outcome for every not-yet-started event on file today/tomorrow."""
    games = []
    for date, rows in snapshots_by_date.items():
        by_event = {}
        for r in rows:
            by_event.setdefault(r["event"], []).append(r)
        for event, rs in by_event.items():
            start = rs[-1]["start"]
            if datetime.fromisoformat(start) < NOW:
                continue                                   # started or finished: ledger territory
            pulls = sorted({r["t"] for r in rs})
            first, last = pulls[0], pulls[-1]
            keyf = lambda r: (r["period"], r["market"], r["outcome"])
            opening = {keyf(r): r for r in rs if r["t"] == first}
            latest = {keyf(r): r for r in rs if r["t"] == last}
            mk = {}
            for (period, market, outcome), r in latest.items():
                o = opening.get((period, market, outcome))
                mk.setdefault(f"{period} · {market}", []).append({
                    "outcome": outcome, "american": r["american"], "handicap": r["handicap"], "implied": r["implied"],
                    "open_american": o["american"] if o else None, "open_handicap": o["handicap"] if o else None,
                    "open_implied": o["implied"] if o else None})
            for k, outs in mk.items():
                fair, over = devig([x["implied"] for x in outs])
                for x, p in zip(outs, fair):
                    x["fair"] = None if p is None else round(p, 4)
                    x["move"] = None if (x["implied"] is None or x["open_implied"] is None) else round(x["implied"] - x["open_implied"], 4)
                mk[k] = {"outcomes": outs, "overround": round(over, 4)}
            games.append({"date": date, "event": event, "start": start, "start_et": datetime.fromisoformat(start).astimezone(ET).strftime("%a %H:%M ET"),
                          "pulls": len(pulls), "first_pull": first, "last_pull": last, "markets": mk})
    games.sort(key=lambda g: g["start"])
    return games


def main():
    status = {"generated_utc": NOW.strftime("%Y-%m-%d %H:%M UTC"), "source": "bovada.lv public coupon JSON", "ok": True}
    t_iso = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    touched = {}
    try:
        events = pull_board()
        n_rows = 0
        for e in events:
            start = datetime.fromtimestamp(e["startTime"] / 1000, tz=timezone.utc)
            date = start.astimezone(ET).strftime("%Y-%m-%d")
            try:
                rows = pull_event(e["link"])
            except Exception:
                continue                                    # event went live / vanished between the two calls
            time.sleep(0.4)
            path = ODDS / f"{date}.json"
            data = touched.get(date) or load(path)
            for r in rows:
                data.append({"t": t_iso, "event": e["description"], "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), **r})
            touched[date] = data; n_rows += len(rows)
        for date, data in touched.items():
            (ODDS / f"{date}.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        status.update(events=len(events), rows=n_rows, dates=sorted(touched))
        print(f"ok: {len(events)} events, {n_rows} rows -> {sorted(touched)}")
    except Exception as e:
        status.update(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}")
        print("FAIL", status["error"]); traceback.print_exc()
    # board from everything on file for today and tomorrow (ET)
    today = NOW.astimezone(ET).date()
    files = {p.stem: load(p) for p in sorted(ODDS.glob("*.json")) if abs((datetime.strptime(p.stem, "%Y-%m-%d").date() - today).days) <= 1}
    board = {"status": status, "games": build_board(files)}
    (HERE / "board.json").write_text(json.dumps(board, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"board: {len(board['games'])} upcoming games")
    return 0


if __name__ == "__main__":
    sys.exit(main())
