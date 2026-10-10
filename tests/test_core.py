"""Core logic tests — run with `pytest -q` (no server or API keys needed)."""
import asyncio

import numpy as np

from app.alerts import AlertState, evaluate
from app.engine import MatchEngine, UserTeam
from app.field import build_field
from app.llm import grounded, narrate
from app.match_loader import Ball, load_match
from app.scoring import PlayerStats, apply_ball

MATCH = load_match()
FIELD = build_field(MATCH.players, n_teams=3000)
IDX = MATCH.player_index()
DEMO = ["Jitesh Sharma", "Phil Salt", "Virat Kohli", "Dewald Brevis", "Rachin Ravindra", "Shivam Dube",
        "Krunal Pandya", "Noor Ahmad", "Josh Hazlewood", "Suyash Sharma", "Matheesha Pathirana"]


def team():
    return UserTeam([IDX[n] for n in DEMO], IDX["Phil Salt"], IDX["Rachin Ravindra"])


def ball(**kw):
    base = dict(seq=0, innings=1, over=0, legal_in_over=1, batting_team="A", bowling_team="B",
                batter="X", non_striker="Y", bowler="Z", runs_batter=0, runs_extras=0, runs_total=0)
    base.update(kw)
    return Ball(**base)


def test_batting_points_and_bonuses():
    st = {"X": PlayerStats("BAT"), "Y": PlayerStats("BAT"), "Z": PlayerStats("BOWL")}
    apply_ball(st, ball(runs_batter=6, runs_total=6))
    apply_ball(st, ball(runs_batter=4, runs_total=4))
    b = st["X"].breakdown()
    assert b["Runs"] == 10 and b["Six bonus"] == 6 and b["Boundary bonus"] == 4


def test_wicket_lbw_bonus_and_duck():
    st = {"X": PlayerStats("BAT"), "Y": PlayerStats("BAT"), "Z": PlayerStats("BOWL")}
    apply_ball(st, ball(wicket={"player_out": "X", "kind": "lbw"}))
    assert st["Z"].wickets == 1 and st["Z"].lbw_bowled == 1
    assert st["X"].breakdown()["Duck"] == -2
    assert st["Z"].breakdown()["Wickets"] == 30


def test_run_out_not_credited_to_bowler():
    st = {"X": PlayerStats("BAT"), "Y": PlayerStats("BAT"), "Z": PlayerStats("BOWL"), "F": PlayerStats("AR")}
    apply_ball(st, ball(wicket={"player_out": "X", "kind": "run out", "fielders": [{"name": "F"}]}))
    assert st["Z"].wickets == 0 and st["F"].runout_direct == 1


def test_field_is_valid():
    M = FIELD.M
    assert ((M > 0).sum(1) == 11).all()
    assert ((M == 2).sum(1) == 1).all() and ((M == 1.5).sum(1) == 1).all()


def test_full_replay_consistency():
    e = MatchEngine(MATCH, FIELD, team())
    while not e.done:
        u = e.step()
        assert 1 <= u["rank"] <= e.contest_size
    # user points equal weighted sum of player points
    assert abs(e.user_points() - float(e.u @ e.p)) < 1e-6
    assert e.score_state()["result"]


def test_alerts_are_selective():
    e, st, n = MatchEngine(MATCH, FIELD, team()), AlertState(), 0
    while not e.done:
        n += len(evaluate(e, e.step(), st))
    assert 5 < n < len(MATCH.balls) * 0.3


def test_guard_blocks_invented_numbers():
    facts = {"rank_now": 1234, "drivers": [{"fantasy_points_gained": 22.0}]}
    assert grounded("Up to #1,234 thanks to a 22-point over", facts)
    assert not grounded("Up to #999", facts)


def test_template_fallback_without_keys():
    e = MatchEngine(MATCH, FIELD, team())
    for _ in range(36):
        e.step()
    out = asyncio.run(narrate("recovery", e.recovery_facts()))
    assert out["text"]


def test_rule_based_answers_follow_the_question():
    from app.answers import answer
    e = MatchEngine(MATCH, FIELD, team())
    for _ in range(60):
        e.step()
    f = e.ask_facts([])
    assert "Kohli" in answer("why is kohli hurting me?", f)
    assert "Dhoni" in answer("what about dhoni", f)
    assert answer("how do I climb", f).startswith("You're #")
