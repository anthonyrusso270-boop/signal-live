"""Grade the odds ledger against final scores — MLB Stats API (open, no key).

For every game on file whose start is > 4 h ago and that is not yet graded: fetch the day's
schedule with linescore, match the Bovada event to the MLB game by team names, take the
CLOSING line (last snapshot before first pitch) and the OPENING line (first snapshot) for each
kept market, and settle:
  full game   moneyline winner · runline cover · total over/under (push at the number)
  first five  2-way moneyline (tie = push) · 3-way · F5 runline · F5 total
Writes results/<date>.json (scores) and ledger.json (every graded game, cumulative, plus
calibration of closing de-vigged favourite probability vs realised win rate). No model picks
live here — this is the market record a model has to beat.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).resolve().parent
ODDS, RES = HERE / "odds", HERE / "results"; RES.mkdir(exist_ok=True)
LEDGER = HERE / "ledger.json"
ET = ZoneInfo("America/New_York")
NOW = datetime.now(timezone.utc)


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def american_to_prob(a):
    if a is None: return None
    a = 100.0 if str(a).upper() == "EVEN" else float(a)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def norm(s):
    return "".join(ch for ch in s.lower() if ch.isalnum())


def schedule(date):
    r = requests.get("https://statsapi.mlb.com/api/v1/schedule", params={"sportId": 1, "date": date, "hydrate": "linescore"}, timeout=30)
    r.raise_for_status()
    games = []
    for d in r.json().get("dates", []):
        for g in d["games"]:
            ls = g.get("linescore") or {}
            inn = ls.get("innings") or []
            f5 = [i for i in inn if i.get("num", 99) <= 5]
            final = g["status"].get("abstractGameState") == "Final" and g["status"].get("detailedState", "").startswith("Final")
            games.append({"away": g["teams"]["away"]["team"]["name"], "home": g["teams"]["home"]["team"]["name"], "pk": g["gamePk"],
                          "state": g["status"].get("detailedState"), "final": final, "gameDate": g.get("gameDate"),
                          "away_runs": (ls.get("teams") or {}).get("away", {}).get("runs"), "home_runs": (ls.get("teams") or {}).get("home", {}).get("runs"),
                          "innings_played": len(inn),
                          "f5_away": sum(i.get("away", {}).get("runs") or 0 for i in f5) if len(f5) >= 5 else None,
                          "f5_home": sum(i.get("home", {}).get("runs") or 0 for i in f5) if len(f5) >= 5 else None})
    return games


def match(event, games):
    away_s, home_s = [s.strip() for s in event.split("@")]
    a, h = norm(away_s), norm(home_s)
    for g in games:
        ga, gh = norm(g["away"]), norm(g["home"])
        if (a in ga or ga in a) and (h in gh or gh in h):
            return g
    # nickname fallback (last word)
    for g in games:
        if norm(away_s.split()[-1]) in norm(g["away"]) and norm(home_s.split()[-1]) in norm(g["home"]):
            return g
    return None


def settle(rows, event, start, g):
    """Closing/opening quotes → settlements per market. rows = all snapshots of this event."""
    pulls = sorted({r["t"] for r in rows})
    pre = [t for t in pulls if t < start]
    close_t = pre[-1] if pre else pulls[-1]; open_t = pulls[0]
    key = lambda r: (r["period"], r["market"], r["outcome"])
    closing = {key(r): r for r in rows if r["t"] == close_t}
    opening = {key(r): r for r in rows if r["t"] == open_t}
    away, home = [s.strip() for s in event.split("@")]
    ar, hr, fa, fh = g["away_runs"], g["home_runs"], g["f5_away"], g["f5_home"]
    out = {"close_t": close_t, "open_t": open_t, "pulls": len(pulls), "closed_pre_match": bool(pre), "markets": {}}

    def side(period, market, contains):
        for (p, m, o), r in closing.items():
            if p == period and m == market and contains(o):
                return r, opening.get((p, m, o))
        return None, None

    def rec(name, r, o, won, push=False):
        if r is None: return
        p = american_to_prob(r["american"]); po = american_to_prob(o["american"]) if o else None
        out["markets"][name] = {"outcome": r["outcome"], "close": r["american"], "open": o["american"] if o else None,
                                "handicap": r.get("handicap"), "close_implied": None if p is None else round(p, 4),
                                "open_implied": None if po is None else round(po, 4),
                                "clv": None if (p is None or po is None) else round(p - po, 4),
                                "result": "push" if push else ("win" if won else "loss")}

    # full game moneyline: settle from the favourite's perspective and record both sides' probs
    ra, oa = side("Game", "Moneyline", lambda o: norm(o).startswith(norm(away)[:6]) or norm(away) in norm(o))
    rh, oh = side("Game", "Moneyline", lambda o: norm(o).startswith(norm(home)[:6]) or norm(home) in norm(o))
    if ra and rh and ar is not None and hr is not None:
        pa, ph = american_to_prob(ra["american"]), american_to_prob(rh["american"]); s = pa + ph
        fav_home = ph >= pa
        fav_r, fav_o = (rh, oh) if fav_home else (ra, oa)
        won = (hr > ar) if fav_home else (ar > hr)
        rec("game_ml_favourite", fav_r, fav_o, won)
        out["markets"]["game_ml_favourite"].update(fair=round(max(pa, ph) / s, 4), overround=round(s - 1, 4), favourite=home if fav_home else away)
    # full game total
    ro, oo = side("Game", "Total", lambda o: o.lower().startswith("over"))
    if ro and ar is not None and hr is not None and ro.get("handicap") is not None:
        line = float(ro["handicap"]); tot = ar + hr
        rec("game_total_over", ro, oo, tot > line, push=(tot == line)); out["markets"]["game_total_over"]["line"] = line; out["markets"]["game_total_over"]["total"] = tot
    # full game runline (favourite side = negative handicap)
    rl, ol = side("Game", "Runline", lambda o: True)
    for (p, m, o), r in closing.items():
        if p == "Game" and m == "Runline" and r.get("handicap") is not None and float(r["handicap"]) < 0 and ar is not None:
            is_home = norm(home) in norm(o) or norm(o).startswith(norm(home)[:6])
            margin = (hr - ar) if is_home else (ar - hr); h = float(r["handicap"])
            rec("game_runline_favourite", r, opening.get((p, m, o)), margin + h > 0, push=(margin + h == 0)); break
    # first five
    if fa is not None and fh is not None:
        f5tot = fa + fh
        rA, oA = side("First 5 Innings", "Moneyline", lambda o: norm(away)[:6] in norm(o) or norm(away) in norm(o))
        rH, oH = side("First 5 Innings", "Moneyline", lambda o: norm(home)[:6] in norm(o) or norm(home) in norm(o))
        if rA and rH:
            pa, ph = american_to_prob(rA["american"]), american_to_prob(rH["american"]); fav_home = ph >= pa
            fav_r, fav_o = (rH, oH) if fav_home else (rA, oA)
            won = (fh > fa) if fav_home else (fa > fh)
            rec("f5_ml_favourite", fav_r, fav_o, won, push=(fa == fh)); out["markets"]["f5_ml_favourite"].update(fair=round(max(pa, ph) / (pa + ph), 4), favourite=home if fav_home else away, f5_score=f"{fa}-{fh}")
        rT, oT = side("First 5 Innings", "3-Way Moneyline", lambda o: "tie" in o.lower() or "draw" in o.lower())
        if rT:
            rec("f5_tie", rT, oT, fa == fh)
        r5, o5 = side("First 5 Innings", "Total", lambda o: o.lower().startswith("over"))
        if r5 and r5.get("handicap") is not None:
            line = float(r5["handicap"]); rec("f5_total_over", r5, o5, f5tot > line, push=(f5tot == line)); out["markets"]["f5_total_over"]["line"] = line; out["markets"]["f5_total_over"]["total"] = f5tot
    return out


def calibration(ledger):
    buckets = {}
    for g in ledger:
        for k in ("game_ml_favourite", "f5_ml_favourite"):
            m = g["markets"].get(k)
            if not m or m["result"] == "push" or m.get("fair") is None: continue
            b = f"{int(m['fair'] * 10) * 10}–{int(m['fair'] * 10) * 10 + 10}%"
            d = buckets.setdefault((k, b), {"market": k, "bucket": b, "n": 0, "wins": 0, "fair_sum": 0.0})
            d["n"] += 1; d["wins"] += m["result"] == "win"; d["fair_sum"] += m["fair"]
    rows = []
    for d in buckets.values():
        rows.append({"market": d["market"], "bucket": d["bucket"], "n": d["n"], "realised": round(d["wins"] / d["n"], 3), "mean_fair": round(d["fair_sum"] / d["n"], 3)})
    return sorted(rows, key=lambda r: (r["market"], r["bucket"]))


def summary(ledger):
    def rec(k):
        rs = [g["markets"][k]["result"] for g in ledger if k in g["markets"]]
        return {"n": len(rs), "win": rs.count("win"), "loss": rs.count("loss"), "push": rs.count("push")}
    fav = [g["markets"]["game_ml_favourite"] for g in ledger if "game_ml_favourite" in g["markets"]]
    f5 = [g["markets"]["f5_ml_favourite"] for g in ledger if "f5_ml_favourite" in g["markets"]]
    ties = [g["markets"]["f5_tie"] for g in ledger if "f5_tie" in g["markets"]]
    clv = [m["clv"] for g in ledger for m in g["markets"].values() if m.get("clv") is not None]
    return {"games": len(ledger), "game_ml_favourite": rec("game_ml_favourite"), "game_total_over": rec("game_total_over"), "game_runline_favourite": rec("game_runline_favourite"),
            "f5_ml_favourite": rec("f5_ml_favourite"), "f5_total_over": rec("f5_total_over"), "f5_tie": rec("f5_tie"),
            "fav_mean_fair": round(sum(m["fair"] for m in fav if m.get("fair")) / max(1, len(fav)), 3) if fav else None,
            "fav_realised": round(sum(m["result"] == "win" for m in fav) / max(1, len([m for m in fav if m["result"] != "push"])), 3) if fav else None,
            "f5_fav_mean_fair": round(sum(m["fair"] for m in f5 if m.get("fair")) / max(1, len(f5)), 3) if f5 else None,
            "f5_fav_realised": round(sum(m["result"] == "win" for m in f5) / max(1, len([m for m in f5 if m["result"] != "push"])), 3) if f5 else None,
            "f5_tie_rate": round(sum(m["result"] == "win" for m in ties) / len(ties), 3) if ties else None,
            "f5_tie_mean_implied": round(sum(m["close_implied"] for m in ties if m["close_implied"]) / len(ties), 3) if ties else None,
            "mean_abs_clv": round(sum(abs(c) for c in clv) / len(clv), 4) if clv else None}


def main():
    ledger = load(LEDGER, {"games": []})
    done = {(g["date"], g["event"]) for g in ledger["games"]}
    n_new = 0
    for path in sorted(ODDS.glob("*.json")):
        date = path.stem
        rows = load(path, [])
        by_event = {}
        for r in rows: by_event.setdefault(r["event"], []).append(r)
        pending = {ev: rs for ev, rs in by_event.items() if (date, ev) not in done and datetime.fromisoformat(rs[-1]["start"].replace("Z", "+00:00")) < NOW - timedelta(hours=4)}
        if not pending: continue
        try:
            games = schedule(date)
        except Exception as e:
            print("schedule failed", date, e); continue
        (RES / f"{date}.json").write_text(json.dumps(games, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        for ev, rs in pending.items():
            g = match(ev, games)
            if not g or not g["final"] or g["away_runs"] is None: continue
            start = rs[-1]["start"]
            s = settle([{**r, "start": r["start"]} for r in rs], ev, start, g)
            ledger["games"].append({"date": date, "event": ev, "start": start, "score": f"{g['away_runs']}-{g['home_runs']}", "innings": g["innings_played"],
                                    "f5": None if g["f5_away"] is None else f"{g['f5_away']}-{g['f5_home']}", "gamePk": g["pk"], **s})
            n_new += 1
    ledger["games"].sort(key=lambda g: g["start"])
    ledger["summary"] = summary(ledger["games"]); ledger["calibration"] = calibration(ledger["games"])
    ledger["graded_utc"] = NOW.strftime("%Y-%m-%d %H:%M UTC")
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"graded {n_new} new games; ledger {len(ledger['games'])} games; summary {ledger['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
