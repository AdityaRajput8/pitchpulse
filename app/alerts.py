"""Personal relevance filter for notifications.

Every ball reaches the ticker, but only moments that move *this* fan's
team or rank become alerts. Each rule returns a relevance score; alerts
below ALERT_THRESHOLD are dropped and counted as filtered noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .engine import TOP_FRACTION, MatchEngine

ALERT_THRESHOLD = 0.5
BAT_MILESTONES = (25, 50, 75, 100)
WICKET_MILESTONES = (3, 4, 5)


@dataclass
class AlertState:
    fired: set = field(default_factory=set)
    in_top10: bool | None = None
    filtered: int = 0
    sent: int = 0


def _alert(kind, title, body, relevance, tone="info", player=None):
    return {"kind": kind, "title": title, "body": body, "relevance": round(relevance, 2),
            "tone": tone, "player": player}


def evaluate(engine: MatchEngine, update: dict, st: AlertState) -> list[dict]:
    team = engine.team
    ball = update["ball"]
    out: list[dict] = []

    def mult(pid):  # importance of a player to this user
        return {"captain": 1.0, "vice-captain": 0.85, "in your team": 0.6}.get(team.tag(pid), 0.0)

    def word(pid):
        t = team.tag(pid)
        return t if t in ("captain", "vice-captain") else "pick"

    # 1. Your player dismissed
    w = ball.get("wicket")
    if w and w["player_out"] in engine.idx:
        pid = engine.idx[w["player_out"]]
        m = mult(pid)
        if m:
            s = engine.stats[w["player_out"]]
            out.append(_alert("wicket", f"Your {word(pid)} is out",
                              f"{w['player_out']} goes for {s.runs} ({s.balls}), {w['kind']}.",
                              m, "down", w["player_out"]))

    # 2. Milestones for your players (and a heads-up when C/VC is close)
    for name in {ball["batter"], ball["bowler"]}:
        if name not in engine.idx:
            continue
        pid = engine.idx[name]
        m = mult(pid)
        if not m:
            continue
        s = engine.stats[name]
        for t in BAT_MILESTONES:
            key = ("bat", name, t)
            if s.runs >= t and key not in st.fired:
                st.fired.add(key)
                if t >= 50 or m >= 0.85:
                    out.append(_alert("milestone", f"{name} reaches {t}",
                                      f"{s.runs} off {s.balls} — he's your {word(pid)}.",
                                      m * (0.8 + t / 250), "up", name))
        if m >= 0.85 and 42 <= s.runs < 50 and not s.out and ("near50", name) not in st.fired:
            st.fired.add(("near50", name))
            mult_txt = "2x" if team.tag(pid) == "captain" else "1.5x"
            out.append(_alert("watch", f"{name} is {50 - s.runs} away from 50",
                              f"The half-century bonus is worth {8 * (2 if mult_txt == '2x' else 1.5):g} points to you at {mult_txt}.",
                              m * 0.7, "info", name))
        for t in WICKET_MILESTONES:
            key = ("wkt", name, t)
            if s.wickets >= t and key not in st.fired:
                st.fired.add(key)
                out.append(_alert("milestone", f"{name} has {t} wickets",
                                  f"{s.wickets}/{s.conceded} — haul bonus unlocked for your team.",
                                  m * 1.1, "up", name))

    # 3. Big single-ball swings from players you don't own (threats)
    #    and differentials you do own (your edge)
    for mv in update["movers"]:
        if mv["delta"] < 12:
            continue
        pid = mv["id"]
        own = float(engine.field.own[pid])
        if team.tag(pid) == "not in your team" and own >= 0.45:
            out.append(_alert("threat", f"{mv['name']} hurts you",
                              f"+{mv['delta']:g} pts for a player in {own:.0%} of teams. You don't have him.",
                              0.5 + own * 0.4, "down", mv["name"]))
        elif mult(pid) and own <= 0.35:
            out.append(_alert("edge", f"Your differential delivers",
                              f"{mv['name']} +{mv['delta']:g} pts — only {own:.0%} of teams own him.",
                              0.6 + (0.35 - own), "up", mv["name"]))

    # 4. Over-level rank swings
    summ = update.get("over_summary")
    if summ:
        moved = summ["ranks_moved"]
        frac = abs(moved) / engine.contest_size
        if frac >= 0.04 and engine.cursor > 12:  # first 2 overs: everyone starts tied
            up = moved > 0
            out.append(_alert("rank", f"{'Up' if up else 'Down'} {abs(moved):,} ranks this over",
                              f"Now #{summ['rank_now']:,} of {engine.contest_size:,}. Tap AI copilot for why.",
                              min(1.0, 0.5 + frac * 4), "up" if up else "down"))

    # 5. Entering / leaving the top 10%
    in_top = update["rank"] <= engine.contest_size * TOP_FRACTION
    if st.in_top10 is not None and in_top != st.in_top10 and engine.cursor > 12:
        out.append(_alert("zone", "You're in the top 10%" if in_top else "You dropped out of the top 10%",
                          f"Rank #{update['rank']:,}.", 0.9, "up" if in_top else "down"))
    st.in_top10 = in_top

    # 6. Structural moments always matter
    if update["innings_end"] and not update["match_end"]:
        sc = update["score"]["lines"][0]
        out.append(_alert("innings", "Innings break",
                          f"{sc['team']} {sc['runs']}/{sc['wickets']}. Target {sc['runs'] + 1}.", 1.0))
    if update["match_end"]:
        out.append(_alert("result", update["score"].get("result", "Match over"),
                          f"Final rank #{update['rank']:,} of {engine.contest_size:,}.", 1.0))

    kept = [a for a in out if a["relevance"] >= ALERT_THRESHOLD]
    kept.sort(key=lambda a: -a["relevance"])
    kept = kept[:2]  # never more than two pings per ball
    st.sent += len(kept)
    if not kept:
        st.filtered += 1
    return kept
