"""Per-viewer match engine.

Every ball: update player stats -> fantasy points vector p -> user points
and rank against the simulated field. All numbers the AI later talks
about are computed here deterministically; the LLM only narrates them.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

import numpy as np

from .field import Field
from .match_loader import Ball, Match
from .scoring import RULES, PlayerStats, apply_ball

TOP_FRACTION = 0.10       # "top 10%" zone shown in the UI
WINNING_FRACTION = 0.50   # approximate paid zone in a mega contest


@dataclass
class UserTeam:
    player_ids: list[int]
    captain: int
    vice_captain: int

    def vector(self, n_players: int) -> np.ndarray:
        u = np.zeros(n_players, dtype=np.float32)
        u[self.player_ids] = 1.0
        u[self.captain] = RULES["captain"]
        u[self.vice_captain] = RULES["vice_captain"]
        return u

    def tag(self, pid: int) -> str:
        if pid == self.captain:
            return "captain"
        if pid == self.vice_captain:
            return "vice-captain"
        return "in your team" if pid in self.player_ids else "not in your team"


@dataclass
class InningsScore:
    team: str
    runs: int = 0
    wickets: int = 0
    legal: int = 0

    @property
    def overs(self) -> str:
        return f"{self.legal // 6}.{self.legal % 6}"


@dataclass
class MatchEngine:
    match: Match
    field: Field
    team: UserTeam
    cursor: int = 0
    stats: dict[str, PlayerStats] = dc_field(default_factory=dict)
    scores: list[InningsScore] = dc_field(default_factory=list)
    history: list[dict] = dc_field(default_factory=list)
    over_runs: list[list[int]] = dc_field(default_factory=lambda: [[], []])
    last_rank: int = 0

    def __post_init__(self):
        self.P = len(self.match.players)
        self.idx = self.match.player_index()
        self.names = [p.name for p in self.match.players]
        self.u = self.team.vector(self.P)
        self.stats = {p.name: PlayerStats(role=p.role) for p in self.match.players}
        first = self.match.balls[0]
        self.scores = [InningsScore(first.batting_team), InningsScore(first.bowling_team)]
        self.p = self._points_vector()
        self.window_start_p = self.p.copy()
        self.window_start_rank = self._rank_now()[0]
        self.last_rank = self.window_start_rank
        self.history.append(self._chart_point(None))

    # ---------- core numbers ----------
    def _points_vector(self) -> np.ndarray:
        return np.array([self.stats[n].points() for n in self.names], dtype=np.float32)

    def user_points(self, p: np.ndarray | None = None) -> float:
        return float(self.u @ (self.p if p is None else p))

    def _rank_now(self):
        fp = self.field.M @ self.p
        up = self.user_points()
        return self.field.rank_of(up, fp), fp, up

    @property
    def done(self) -> bool:
        return self.cursor >= len(self.match.balls)

    @property
    def contest_size(self) -> int:
        return self.field.size + 1

    # ---------- stepping ----------
    def step(self) -> dict:
        ball = self.match.balls[self.cursor]
        self.cursor += 1
        prev_p = self.p
        prev_rank = self.last_rank

        apply_ball(self.stats, ball)
        sc = self.scores[ball.innings - 1]
        sc.runs += ball.runs_total
        if ball.wicket:
            sc.wickets += 1
        if ball.legal:
            sc.legal += 1
        ovr = self.over_runs[ball.innings - 1]
        while len(ovr) <= ball.over:
            ovr.append(0)
        ovr[ball.over] += ball.runs_total

        self.p = self._points_vector()
        rank, fp, up = self._rank_now()
        self.last_rank = rank
        dp = self.p - prev_p

        nxt = self.match.balls[self.cursor] if not self.done else None
        over_end = nxt is None or nxt.innings != ball.innings or nxt.over != ball.over
        innings_end = nxt is None or nxt.innings != ball.innings

        point = self._chart_point(ball, rank=rank, fp=fp, up=up)
        self.history.append(point)

        movers = [
            {"id": int(i), "name": self.names[i], "delta": float(dp[i]),
             "points": float(self.p[i]), "tag": self.team.tag(int(i))}
            for i in np.nonzero(dp)[0]
        ]
        update = {
            "ball": ball.to_dict(),
            "commentary": describe_ball(ball),
            "score": self.score_state(),
            "user_points": up,
            "user_delta": float(self.u @ dp),
            "rank": rank,
            "rank_delta": prev_rank - rank,   # positive = climbed
            "chart": point,
            "movers": movers,
            "over_end": over_end,
            "innings_end": innings_end,
            "match_end": nxt is None,
        }
        if over_end:
            update["over_summary"] = self.window_facts(label=f"over {ball.over + 1} of innings {ball.innings}")
            self.window_start_p = self.p.copy()
            self.window_start_rank = rank
        return update

    # ---------- state for UI / AI ----------
    def _chart_point(self, ball: Ball | None, rank=None, fp=None, up=None) -> dict:
        if rank is None:
            rank, fp, up = self._rank_now()
        return {
            "seq": -1 if ball is None else ball.seq,
            "label": "start" if ball is None else f"I{ball.innings} {ball.label}",
            "innings": 1 if ball is None else ball.innings,
            "user_points": round(up, 1),
            "rank": rank,
            "top10_cutoff": round(self.field.cutoff(fp, TOP_FRACTION), 1),
            "median": round(float(np.median(fp)), 1),
            "leader": round(float(fp.max()), 1),
        }

    def score_state(self) -> dict:
        s1, s2 = self.scores
        inn = 1 if self.cursor == 0 else self.match.balls[self.cursor - 1].innings
        st = {
            "innings": inn,
            "batting": (s1 if inn == 1 else s2).team,
            "lines": [
                {"team": s.team, "runs": s.runs, "wickets": s.wickets, "overs": s.overs}
                for s in self.scores
            ],
            "worm": self.over_runs,
        }
        cur = s1 if inn == 1 else s2
        st["crr"] = round(cur.runs * 6 / cur.legal, 2) if cur.legal else 0.0
        if inn == 2:
            target = s1.runs + 1
            need = target - s2.runs
            balls_left = 120 - s2.legal
            st.update(target=target, need=max(need, 0), balls_left=balls_left,
                      rrr=round(need * 6 / balls_left, 2) if balls_left > 0 and need > 0 else 0.0)
        if self.done:
            st["result"] = self.result_text()
        return st

    def result_text(self) -> str:
        s1, s2 = self.scores
        if s2.runs > s1.runs:
            return f"{s2.team} won by {10 - s2.wickets} wickets"
        if s1.runs > s2.runs:
            return f"{s1.team} won by {s1.runs - s2.runs} runs"
        return "Match tied"

    def player_board(self) -> list[dict]:
        rows = []
        for p in self.match.players:
            st = self.stats[p.name]
            rows.append({
                "id": p.id, "name": p.name, "team": p.team, "role": p.role,
                "credits": p.credits, "points": float(self.p[p.id]),
                "line": st.line(), "tag": self.team.tag(p.id),
                "own": round(float(self.field.own[p.id]) * 100, 1),
                "cap": round(float(self.field.cap[p.id]) * 100, 1),
                "breakdown": st.breakdown(),
            })
        return rows

    # ---------- explanations (facts only, no prose) ----------
    def _neighbours(self, start_p: np.ndarray) -> np.ndarray:
        """Teams whose points were closest to yours at the start of the window.
        Rank is decided by these teams, not by the field average."""
        fp0 = self.field.M @ start_p
        up0 = float(self.u @ start_p)
        k = max(200, self.field.size // 20)
        return np.argpartition(np.abs(fp0 - up0), k)[:k]

    def _driver_rows(self, dp: np.ndarray, nb: np.ndarray, k: int = 3) -> list[dict]:
        eff_nb = self.field.M[nb].mean(axis=0)
        leverage = (self.u - eff_nb) * dp
        order = np.argsort(-np.abs(leverage))
        rows = []
        for i in order[:k]:
            if abs(leverage[i]) < 0.5:
                break
            rows.append({
                "player": self.names[i],
                "fantasy_points_gained": round(float(dp[i]), 1),
                "your_exposure": self.team.tag(int(i)),
                "field_ownership_pct": round(float(self.field.own[i]) * 100),
                "field_captaincy_pct": round(float(self.field.cap[i]) * 100),
                "ownership_near_your_rank_pct": round(float((self.field.M[nb, i] > 0).mean()) * 100),
                "effect_on_you": "helped" if leverage[i] > 0 else "hurt",
            })
        return rows

    def window_facts(self, label: str, start_p: np.ndarray | None = None,
                     start_rank: int | None = None) -> dict:
        sp = self.window_start_p if start_p is None else start_p
        sr = self.window_start_rank if start_rank is None else start_rank
        dp = self.p - sp
        rank, fp, up = self._rank_now()
        nb = self._neighbours(sp)
        return {
            "window": label,
            "rank_before": sr,
            "rank_now": rank,
            "ranks_moved": sr - rank,
            "direction": "up" if rank < sr else ("down" if rank > sr else "flat"),
            "contest_size": self.contest_size,
            "your_points_gained": round(float(self.u @ dp), 1),
            "teams_near_you_gained_avg": round(float((self.field.M[nb] @ dp).mean()), 1),
            "your_total_points": round(up, 1),
            "drivers": self._driver_rows(dp, nb),
        }

    def _current_innings(self) -> int:
        """Innings in progress (or about to start) from the viewer's POV."""
        return 2 if self.done else self.match.balls[self.cursor].innings

    def _player_status(self, pid: int) -> str | None:
        """Can this player still score? Returns a short status or None."""
        if self.done:
            return None
        p = self.match.players[pid]
        st = self.stats[p.name]
        cur = self._current_innings()
        bats_in = 1 if p.team == self.scores[0].team else 2
        bowls_in = 3 - bats_in
        if bats_in == cur and st.batted and not st.out:
            return f"batting, {st.runs} off {st.balls}"
        if bats_in > cur or (bats_in == cur and not st.batted):
            return "yet to bat"
        if bowls_in >= cur and (p.role in ("BOWL", "AR") or st.legal_bowled > 0) and st.legal_bowled < 24:
            left = 24 - st.legal_bowled
            return f"{left // 6}.{left % 6} overs left to bowl".replace(".0 overs", " overs")
        return None

    def recovery_facts(self) -> dict:
        rank, fp, up = self._rank_now()
        top_cut = self.field.cutoff(fp, TOP_FRACTION)
        win_cut = self.field.cutoff(fp, WINNING_FRACTION)
        live_diffs, threats = [], []
        for p in self.match.players:
            status = self._player_status(p.id)
            if status is None:
                continue
            row = {"player": p.name, "status": status,
                   "field_ownership_pct": round(float(self.field.own[p.id]) * 100),
                   "your_exposure": self.team.tag(p.id)}
            gap = float(self.u[p.id] - self.field.eff[p.id])
            if gap > 0.3:
                live_diffs.append((gap, row))
            elif gap < -0.6:
                threats.append((gap, row))
        live_diffs.sort(key=lambda x: -x[0])
        threats.sort(key=lambda x: x[0])
        return {
            "rank_now": rank,
            "contest_size": self.contest_size,
            "your_total_points": round(up, 1),
            "points_to_top_10pct": round(max(top_cut - up, 0), 1),
            "points_to_winning_zone": round(max(win_cut - up, 0), 1),
            "in_top_10pct": up >= top_cut,
            "in_winning_zone": up >= win_cut,
            "your_live_differentials": [r for _, r in live_diffs[:3]],
            "biggest_threats_you_dont_own": [r for _, r in threats[:3]],
            "match_finished": self.done,
        }

    def catchup_facts(self, since_seq: int) -> dict:
        """What changed between chart point `since_seq` and now."""
        since_seq = max(-1, min(since_seq, self.cursor - 1))
        start = next((h for h in self.history if h["seq"] == since_seq), self.history[0])
        balls = [b for b in self.match.balls[:self.cursor] if b.seq > since_seq]
        sp = _points_vector_at(self, since_seq)
        facts = self.window_facts(label="while you were away", start_p=sp, start_rank=start["rank"])
        key_moments = []
        for b in balls:
            if b.wicket:
                key_moments.append(f"{b.label} (inn {b.innings}): {b.wicket['player_out']} out, {b.wicket['kind']}")
            elif b.runs_batter == 6:
                key_moments.append(f"{b.label} (inn {b.innings}): six by {b.batter}")
        facts.update(
            balls_missed=len(balls),
            score_now=self.score_state()["lines"],
            key_moments=key_moments[-6:],
        )
        facts.update({k: v for k, v in self.score_state().items()
                      if k in ("target", "need", "balls_left", "rrr", "result")})
        return facts

    def ask_facts(self, insights: list[dict]) -> dict:
        """Everything the chat copilot may use to answer a free-form question."""
        fe = self.field
        players = []
        for p in self.match.players:
            st = self.stats[p.name]
            players.append({
                "player": p.name, "team": p.team, "role": p.role,
                "fantasy_points": float(self.p[p.id]), "scorecard": st.line(),
                "your_role": self.team.tag(p.id), "your_multiplier": float(self.u[p.id]),
                "field_avg_multiplier": round(float(fe.eff[p.id]), 2),
                "field_ownership_pct": round(float(fe.own[p.id]) * 100),
                "field_captaincy_pct": round(float(fe.cap[p.id]) * 100),
                "status": self._player_status(p.id) or "",
            })
        recent = [f"{b.label}: {describe_ball(b)}"
                  for b in self.match.balls[max(0, self.cursor - 18):self.cursor]
                  if b.wicket or b.runs_batter in (4, 6)]
        overs = [i for i in insights if i["kind"] == "over"]
        return {
            "standing": self.recovery_facts(),
            "score": {k: v for k, v in self.score_state().items() if k != "worm"},
            "players": players,
            "recent_moments": recent[-5:],
            "last_over_text": overs[-1]["text"] if overs else None,
            "last_over_facts": overs[-1]["facts"] if overs else None,
        }


def _points_vector_at(engine: MatchEngine, seq: int) -> np.ndarray:
    """Points vector as it was right after ball `seq` (replays <=250 balls)."""
    tmp = {p.name: PlayerStats(role=p.role) for p in engine.match.players}
    for b in engine.match.balls[: seq + 1]:
        apply_ball(tmp, b)
    return np.array([tmp[n].points() for n in engine.names], dtype=np.float32)


def describe_ball(b: Ball) -> str:
    who = f"{b.bowler} to {b.batter}"
    if b.wicket:
        w = b.wicket
        kind = w["kind"]
        if kind == "caught" and w.get("fielders"):
            return f"{who}, OUT! caught by {w['fielders'][0]['name']}"
        if kind == "run out":
            return f"{who}, RUN OUT! {w['player_out']} has to go"
        return f"{who}, OUT! {kind}"
    if "wides" in b.extras:
        return f"{who}, wide"
    if "noballs" in b.extras:
        return f"{who}, no ball{f' and {b.runs_batter} off the bat' if b.runs_batter else ''}"
    if "legbyes" in b.extras or "byes" in b.extras:
        return f"{who}, {b.runs_extras} leg bye"
    return {0: f"{who}, no run", 4: f"{who}, FOUR", 6: f"{who}, SIX"}.get(
        b.runs_batter, f"{who}, {b.runs_batter} run{'s' if b.runs_batter > 1 else ''}")
