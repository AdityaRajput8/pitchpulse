"""Rule-based answers for the copilot chat.

Used when no LLM key is set, or when an LLM answer fails the grounding
guard. It understands player names (full, surname or first name) and a
few common intents, so "why is Kohli hurting me?" gets an answer about
Kohli even with zero API keys.
"""
from __future__ import annotations

import re

CATCHUP_WORDS = ("catch", "missed", "happen", "update", "summary", "summar", "going on", "recap", "so far")
CLIMB_WORDS = ("climb", "recover", "win", "chance", "improve", "top 10", "top10", "move up", "get back", "comeback")


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def find_player(question: str, players: list[dict]) -> dict | None:
    words = _words(question)
    best, best_score = None, 0
    for p in players:
        parts = [w for w in re.findall(r"[a-z]+", p["player"].lower()) if len(w) >= 3]
        score = 0
        if p["player"].lower() in question.lower():
            score = 3
        elif parts and parts[-1] in words:
            score = 2
        elif any(w in words for w in parts):
            score = 1
        if score > best_score:
            best, best_score = p, score
    return best


def _player_answer(p: dict, q: str) -> str:
    name, pts, line = p["player"], p["fantasy_points"], p["scorecard"]
    mult, eff = p["your_multiplier"], p["field_avg_multiplier"]
    own, cap = p["field_ownership_pct"], p["field_captaincy_pct"]
    asked_hurt = any(w in q.lower() for w in ("hurt", "costing", "killing", "losing", "bad"))
    asked_help = any(w in q.lower() for w in ("help", "carry", "carrying", "good", "gaining"))

    s = f"{name} has {pts:g} fantasy points" + (f" from {line}." if line != "yet to play" else ", yet to play.")
    if mult == 0:
        if own >= 35:
            s += (f" You don't have him, but {own:g}% of teams do, so every point he scores lifts "
                  f"those teams past you.")
            verdict = "hurting"
        else:
            s += f" You don't have him, and only {own:g}% of teams do, so he barely moves your rank."
            verdict = "neutral"
    elif mult + 0.05 < eff:
        s += (f" He's in your team at {mult:g}x, but {cap:g}% of teams captained him, so the average "
              f"team gets about {eff:.1f}x his points. When he scores, the field gains more than you do.")
        verdict = "hurting"
    elif mult > eff + 0.05:
        role = p["your_role"] if p["your_role"] in ("captain", "vice-captain") else "pick"
        s += (f" He's your {role} at {mult:g}x while the average team holds about {eff:.1f}x, "
              f"so his points push you above the field.")
        verdict = "helping"
    else:
        s += " You hold him about as much as the field does, so he moves you and the field equally."
        verdict = "neutral"

    if asked_hurt and verdict == "helping":
        s += " So he's actually helping you, not hurting."
    elif asked_help and verdict == "hurting":
        s += " So right now he's costing you rank, even if your points go up."
    if p.get("status"):
        s += f" Status: {p['status']}."
    return s


def _standing(st: dict) -> str:
    if st["match_finished"]:
        return f"Match over. You finished #{st['rank_now']:,} of {st['contest_size']:,}."
    if st["in_top_10pct"]:
        s = f"You're #{st['rank_now']:,} of {st['contest_size']:,}, inside the top 10%."
    else:
        s = f"You're #{st['rank_now']:,} of {st['contest_size']:,}, {st['points_to_top_10pct']:g} pts off the top 10%."
    if st["your_live_differentials"]:
        d = st["your_live_differentials"][0]
        only = "only " if d["field_ownership_pct"] <= 35 else ""
        s += f" Your best lever is {d['player']} ({d['status']}, in {only}{d['field_ownership_pct']}% of teams)."
    if st["biggest_threats_you_dont_own"]:
        t = st["biggest_threats_you_dont_own"][0]
        s += f" Biggest threat: {t['player']}, in {t['field_ownership_pct']}% of teams and {t['status']}."
    return s


def _score_line(f: dict) -> str:
    sc = f["score"]
    parts = [f"{l['team']} {l['runs']}/{l['wickets']} ({l['overs']})" for l in sc["lines"] if l["overs"] != "0.0"]
    s = " vs ".join(parts) + "." if parts else "The match hasn't started yet."
    if sc.get("result"):
        s += f" {sc['result']}."
    elif sc.get("need") is not None:
        s += f" {sc['need']} needed off {sc['balls_left']}."
    return s


def answer(question: str, f: dict) -> str:
    q = question.lower()
    p = find_player(question, f["players"])
    if p:
        return _player_answer(p, question)
    if any(w in q for w in CATCHUP_WORDS):
        moments = f.get("recent_moments") or []
        s = _score_line(f)
        if moments:
            s += " Recently: " + "; ".join(moments[-3:]) + "."
        return s + " " + _standing(f["standing"])
    if any(w in q for w in CLIMB_WORDS):
        return _standing(f["standing"])
    if "captain" in q or "vice" in q:
        caps = [x for x in f["players"] if x["your_role"] in ("captain", "vice-captain")]
        return " ".join(_player_answer(x, question) for x in caps) or _standing(f["standing"])
    if "rank" in q or "why" in q:
        last = f.get("last_over_text")
        return (f"Last over: {last} " if last else "") + _standing(f["standing"])
    if "score" in q or "target" in q or "need" in q:
        return _score_line(f)
    return (_standing(f["standing"]) +
            " Ask me about a specific player (e.g. \"why is Kohli hurting me?\") or how to climb.")
