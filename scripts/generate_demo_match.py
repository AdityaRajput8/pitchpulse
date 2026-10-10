"""
Generate a realistic, SIMULATED T20 match in Cricsheet JSON format.

Why: reviewers may open the demo when no real match is live, so the app
replays a match through the same event pipeline a live feed would use.
To replay a REAL match instead, download any IPL match JSON from
https://cricsheet.org/matches/ and point MATCH_FILE at it.

Usage:  python scripts/generate_demo_match.py
Writes: app/data/demo_match.json, app/data/players_meta.json
"""
from __future__ import annotations

import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "data"

# role, credits, popularity (drives simulated field ownership),
# batting aggression (1.0 = average), batting quality, bowling quality
SQUADS = {
    "Royal Challengers Bengaluru": [
        ("Phil Salt",         "BAT", 9.0, 0.62, 1.30, 0.95, None),
        ("Virat Kohli",       "BAT", 10.5, 0.88, 1.05, 1.25, None),
        ("Rajat Patidar",     "BAT", 8.5, 0.40, 1.15, 1.00, None),
        ("Liam Livingstone",  "AR",  8.5, 0.35, 1.25, 0.90, 0.85),
        ("Jitesh Sharma",     "WK",  7.5, 0.30, 1.20, 0.85, None),
        ("Tim David",         "BAT", 8.0, 0.33, 1.35, 0.85, None),
        ("Krunal Pandya",     "AR",  8.0, 0.45, 0.95, 0.80, 1.05),
        ("Bhuvneshwar Kumar", "BOWL", 8.5, 0.52, 0.80, 0.40, 1.10),
        ("Josh Hazlewood",    "BOWL", 9.0, 0.70, 0.70, 0.30, 1.25),
        ("Yash Dayal",        "BOWL", 7.5, 0.25, 0.70, 0.30, 0.95),
        ("Suyash Sharma",     "BOWL", 7.0, 0.20, 0.60, 0.25, 1.00),
    ],
    "Chennai Super Kings": [
        ("Ruturaj Gaikwad",   "BAT", 9.5, 0.72, 1.05, 1.15, None),
        ("Rachin Ravindra",   "AR",  8.5, 0.48, 1.10, 1.00, 0.85),
        ("Ayush Mhatre",      "BAT", 7.0, 0.22, 1.25, 0.90, None),
        ("Shivam Dube",       "AR",  8.5, 0.55, 1.35, 0.95, 0.70),
        ("Dewald Brevis",     "BAT", 7.5, 0.28, 1.35, 0.90, None),
        ("Ravindra Jadeja",   "AR",  9.0, 0.66, 1.05, 0.90, 1.10),
        ("MS Dhoni",          "WK",  7.5, 0.45, 1.30, 0.80, None),
        ("Noor Ahmad",        "BOWL", 8.5, 0.50, 0.70, 0.25, 1.20),
        ("Khaleel Ahmed",     "BOWL", 8.0, 0.40, 0.70, 0.25, 1.05),
        ("Matheesha Pathirana", "BOWL", 8.5, 0.48, 0.60, 0.20, 1.15),
        ("Anshul Kamboj",     "BOWL", 7.0, 0.18, 0.80, 0.35, 0.95),
    ],
}
SPINNERS = {"Suyash Sharma", "Krunal Pandya", "Noor Ahmad", "Ravindra Jadeja", "Liam Livingstone"}
BOWLERS = {
    "Royal Challengers Bengaluru": ["Bhuvneshwar Kumar", "Josh Hazlewood", "Yash Dayal", "Suyash Sharma", "Krunal Pandya"],
    "Chennai Super Kings": ["Khaleel Ahmed", "Anshul Kamboj", "Noor Ahmad", "Ravindra Jadeja", "Matheesha Pathirana"],
}
WK = {"Royal Challengers Bengaluru": "Jitesh Sharma", "Chennai Super Kings": "MS Dhoni"}
VENUE = "M. Chinnaswamy Stadium, Bengaluru"


def meta_by_name():
    out = {}
    for team, rows in SQUADS.items():
        for name, role, credits, pop, agg, bat_q, bowl_q in rows:
            out[name] = dict(team=team, role=role, credits=credits, popularity=pop,
                             agg=agg, bat_q=bat_q, bowl_q=bowl_q or 0.0)
    return out


def bowling_plan(rng: random.Random, bowlers: list[str]) -> list[str]:
    """20 overs, max 4 each, never the same bowler twice in a row."""
    pattern = [0, 1, 0, 1, 2, 3, 2, 4, 3, 4, 2, 3, 4, 0, 1, 4, 3, 2, 0, 1]
    pace, rest = bowlers[:2], bowlers[2:]
    rng.shuffle(pace)
    rng.shuffle(rest)
    order = pace + rest  # new-ball pacers open
    return [order[i] for i in pattern]


def ball_outcome(rng, bat, bowl, over_idx, wkts_down, chasing_pressure):
    phase_agg = 1.15 if over_idx < 6 else (0.95 if over_idx < 15 else 1.35)
    a = bat["agg"] * phase_agg * chasing_pressure
    q = bat["bat_q"]
    b = bowl["bowl_q"] or 0.7
    p = {
        "dot": 0.34 * b / a,
        "1": 0.33,
        "2": 0.07,
        "3": 0.005,
        "4": 0.105 * a * q / b,
        "6": 0.045 * a * a * q / b,
        "W": 0.048 * b * a / max(q, 0.3) * (1.1 if wkts_down >= 6 else 1.0),
        "wd": 0.03,
        "nb": 0.004,
        "lb": 0.02,
    }
    keys = list(p)
    return rng.choices(keys, weights=[p[k] for k in keys])[0]


def simulate_innings(rng, bat_team, bowl_team, meta, target=None):
    order = [r[0] for r in SQUADS[bat_team]]
    fielders = [r[0] for r in SQUADS[bowl_team]]
    plan = bowling_plan(rng, BOWLERS[bowl_team])
    striker, non_striker = order[0], order[1]
    next_in = 2
    total, wkts = 0, 0
    overs = []
    for o in range(20):
        bowler = plan[o]
        deliveries = []
        legal = 0
        while legal < 6:
            pressure = 1.0
            if target is not None:
                balls_left = 120 - (o * 6 + legal)
                need = target - total
                rrr = need * 6 / max(balls_left, 1)
                pressure = min(1.35, max(0.9, rrr / 8.5))
            out = ball_outcome(rng, meta[striker], meta[bowler], o, wkts, pressure)
            d = {"batter": striker, "bowler": bowler, "non_striker": non_striker,
                 "runs": {"batter": 0, "extras": 0, "total": 0}}
            runs_batter, swap = 0, False
            if out == "wd":
                d["extras"] = {"wides": 1}
                d["runs"] = {"batter": 0, "extras": 1, "total": 1}
            elif out == "nb":
                rb = rng.choice([0, 0, 1, 4])
                d["extras"] = {"noballs": 1}
                d["runs"] = {"batter": rb, "extras": 1, "total": rb + 1}
                swap = rb % 2 == 1
            elif out == "lb":
                d["extras"] = {"legbyes": 1}
                d["runs"] = {"batter": 0, "extras": 1, "total": 1}
                legal += 1
                swap = True
            elif out == "W":
                legal += 1
                kind = rng.choices(["caught", "bowled", "lbw", "run out", "stumped"],
                                   weights=[62, 15, 10, 8, 5 if bowler in SPINNERS else 0])[0]
                w = {"player_out": striker, "kind": kind}
                if kind == "caught":
                    r = rng.random()
                    if r < 0.06:
                        w["kind"] = "caught and bowled"
                    else:
                        catcher = WK[bowl_team] if r < 0.24 else rng.choice([f for f in fielders if f != bowler])
                        w["fielders"] = [{"name": catcher}]
                elif kind == "stumped":
                    w["fielders"] = [{"name": WK[bowl_team]}]
                elif kind == "run out":
                    if rng.random() < 0.4:
                        w["player_out"] = non_striker
                    n = 1 if rng.random() < 0.55 else 2
                    w["fielders"] = [{"name": f} for f in rng.sample(fielders, n)]
                    runs_batter = rng.choice([0, 1])
                    d["runs"] = {"batter": runs_batter, "extras": 0, "total": runs_batter}
                d["wickets"] = [w]
                wkts += 1
                gone = w["player_out"]
                if wkts < 10:
                    newb = order[next_in]
                    next_in += 1
                    if gone == striker:
                        striker = newb
                    else:
                        non_striker = newb
            else:
                legal += 1
                runs_batter = 0 if out == "dot" else int(out)
                d["runs"] = {"batter": runs_batter, "extras": 0, "total": runs_batter}
                swap = runs_batter % 2 == 1
            total += d["runs"]["total"]
            deliveries.append(d)
            if swap:
                striker, non_striker = non_striker, striker
            if wkts >= 10 or (target is not None and total >= target):
                break
        overs.append({"over": o, "deliveries": deliveries})
        striker, non_striker = non_striker, striker
        if wkts >= 10 or (target is not None and total >= target):
            break
    return {"team": bat_team, "overs": overs}, total, wkts


def top_score(innings):
    runs = {}
    for ov in innings["overs"]:
        for d in ov["deliveries"]:
            runs[d["batter"]] = runs.get(d["batter"], 0) + d["runs"]["batter"]
    return max(runs.values())


def build(seed: int):
    rng = random.Random(seed)
    meta = meta_by_name()
    teams = list(SQUADS)
    inn1, t1, w1 = simulate_innings(rng, teams[0], teams[1], meta)
    inn2, t2, w2 = simulate_innings(rng, teams[1], teams[0], meta, target=t1 + 1)
    last_over = len(inn2["overs"])
    if t2 > t1:
        outcome = {"winner": teams[1], "by": {"wickets": 10 - w2}}
    elif t1 > t2:
        outcome = {"winner": teams[0], "by": {"runs": t1 - t2}}
    else:
        outcome = {"result": "tie"}
    match = {
        "meta": {"data_version": "1.1.0", "created": "2026-10-11", "note": "SIMULATED demo match"},
        "info": {
            "city": "Bengaluru", "venue": VENUE, "dates": ["2026-04-18"],
            "event": {"name": "Indian Premier League (simulated replay)"},
            "match_type": "T20", "overs": 20, "gender": "male",
            "teams": teams,
            "players": {t: [r[0] for r in SQUADS[t]] for t in teams},
            "toss": {"winner": teams[1], "decision": "field"},
            "outcome": outcome,
        },
        "innings": [inn1, inn2],
    }
    score = (abs(t1 - t2) <= 6, last_over >= 20, 160 <= t1 <= 205,
             top_score(inn1) >= 50, top_score(inn2) >= 45)
    return match, meta, score, (t1, w1, t2, w2)


def main():
    for seed in range(1, 5000):
        match, meta, checks, summary = build(seed)
        if all(checks):
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / "demo_match.json").write_text(json.dumps(match, indent=1))
            players_meta = {n: {k: v for k, v in m.items() if k in ("team", "role", "credits", "popularity")}
                            for n, m in meta.items()}
            (OUT / "players_meta.json").write_text(json.dumps(players_meta, indent=1))
            print(f"seed={seed} {summary} outcome={match['info']['outcome']}")
            return
    raise SystemExit("no seed produced a close match")


if __name__ == "__main__":
    main()
