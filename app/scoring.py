"""Fantasy points modelled on Dream11's T20 points system.

All values live in RULES so they are easy to check against the current
official table (https://www.dream11.com/fantasy-cricket/point-system)
and adjust without touching logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .match_loader import Ball

RULES = {
    "playing_xi": 4,
    "run": 1,
    "four_bonus": 4,
    "six_bonus": 6,
    "milestones": [(100, 16), (75, 12), (50, 8), (25, 4)],  # highest one applies
    "duck": -2,
    "dot_ball": 1,
    "wicket": 30,
    "lbw_bowled_bonus": 8,
    "wicket_hauls": [(5, 12), (4, 8), (3, 4)],  # highest one applies
    "maiden": 12,
    "catch": 8,
    "three_catch_bonus": 4,
    "stumping": 12,
    "runout_direct": 12,
    "runout_indirect": 6,
    # (min_overs) economy bands: (lower, upper, points)
    "economy_min_balls": 12,
    "economy": [(0, 5, 6), (5, 6, 4), (6, 7, 2), (10, 11, -2), (11, 12, -4), (12, 99, -6)],
    "sr_min_balls": 10,
    "strike_rate": [(170.01, 999, 6), (150.01, 170, 4), (130, 150, 2), (60, 70, -2), (50, 59.99, -4), (0, 49.99, -6)],
    "captain": 2.0,
    "vice_captain": 1.5,
}
BOWLER_CREDITED = {"bowled", "caught", "lbw", "stumped", "caught and bowled", "hit wicket"}


@dataclass
class PlayerStats:
    role: str
    runs: int = 0
    balls: int = 0
    fours: int = 0
    sixes: int = 0
    out: bool = False
    batted: bool = False
    legal_bowled: int = 0
    conceded: int = 0
    dots: int = 0
    wickets: int = 0
    lbw_bowled: int = 0
    maidens: int = 0
    catches: int = 0
    stumpings: int = 0
    runout_direct: int = 0
    runout_indirect: int = 0
    _over_conceded: dict = field(default_factory=dict)  # (innings, over) -> runs

    def breakdown(self) -> dict[str, int]:
        b: dict[str, int] = {"Playing XI": RULES["playing_xi"]}
        if self.batted or self.runs:
            b["Runs"] = self.runs * RULES["run"]
            if self.fours:
                b["Boundary bonus"] = self.fours * RULES["four_bonus"]
            if self.sixes:
                b["Six bonus"] = self.sixes * RULES["six_bonus"]
            for threshold, pts in RULES["milestones"]:
                if self.runs >= threshold:
                    b[f"{threshold}-run bonus"] = pts
                    break
            if self.out and self.runs == 0 and self.role != "BOWL":
                b["Duck"] = RULES["duck"]
            if self.balls >= RULES["sr_min_balls"] and self.role != "BOWL":
                sr = self.runs * 100 / self.balls
                for lo, hi, pts in RULES["strike_rate"]:
                    if lo <= sr <= hi:
                        b["Strike rate"] = pts
                        break
        if self.legal_bowled:
            if self.dots:
                b["Dot balls"] = self.dots * RULES["dot_ball"]
            if self.wickets:
                b["Wickets"] = self.wickets * RULES["wicket"]
            if self.lbw_bowled:
                b["LBW/bowled bonus"] = self.lbw_bowled * RULES["lbw_bowled_bonus"]
            for n, pts in RULES["wicket_hauls"]:
                if self.wickets >= n:
                    b[f"{n}-wicket bonus"] = pts
                    break
            if self.maidens:
                b["Maidens"] = self.maidens * RULES["maiden"]
            if self.legal_bowled >= RULES["economy_min_balls"]:
                econ = self.conceded * 6 / self.legal_bowled
                for lo, hi, pts in RULES["economy"]:
                    if lo <= econ < hi:
                        b["Economy"] = pts
                        break
        if self.catches:
            b["Catches"] = self.catches * RULES["catch"]
            if self.catches >= 3:
                b["3-catch bonus"] = RULES["three_catch_bonus"]
        if self.stumpings:
            b["Stumpings"] = self.stumpings * RULES["stumping"]
        if self.runout_direct:
            b["Run out (direct)"] = self.runout_direct * RULES["runout_direct"]
        if self.runout_indirect:
            b["Run out"] = self.runout_indirect * RULES["runout_indirect"]
        return {k: v for k, v in b.items() if v}

    def points(self) -> int:
        return sum(self.breakdown().values())

    def line(self) -> str:
        """Short human summary, e.g. '45 (31) · 2/24 (3)'."""
        parts = []
        if self.batted:
            parts.append(f"{self.runs}{'' if self.out else '*'} ({self.balls})")
        if self.legal_bowled:
            ov = f"{self.legal_bowled // 6}.{self.legal_bowled % 6}".replace(".0", "")
            parts.append(f"{self.wickets}/{self.conceded} ({ov})")
        f = self.catches + self.stumpings + self.runout_direct + self.runout_indirect
        if f:
            parts.append(f"{f} fielding")
        return " · ".join(parts) or "yet to play"


def apply_ball(stats: dict[str, PlayerStats], ball: Ball) -> None:
    """Mutate per-player stats for one delivery. Unknown names are ignored."""
    bat = stats.get(ball.batter)
    bowl = stats.get(ball.bowler)
    is_wide = "wides" in ball.extras
    if bat is not None:
        bat.batted = True
        if not is_wide:
            bat.balls += 1
        bat.runs += ball.runs_batter
        if ball.runs_batter == 4:
            bat.fours += 1
        elif ball.runs_batter == 6:
            bat.sixes += 1
    ns = stats.get(ball.non_striker)
    if ns is not None:
        ns.batted = True

    if bowl is not None:
        bowler_runs = ball.runs_batter + ball.extras.get("wides", 0) + ball.extras.get("noballs", 0)
        bowl.conceded += bowler_runs
        key = (ball.innings, ball.over)
        bowl._over_conceded[key] = bowl._over_conceded.get(key, 0) + bowler_runs
        if ball.legal:
            bowl.legal_bowled += 1
            if bowler_runs == 0:
                bowl.dots += 1
            if ball.legal_in_over == 6 and bowl._over_conceded[key] == 0:
                bowl.maidens += 1

    w = ball.wicket
    if w:
        out = stats.get(w["player_out"])
        if out is not None:
            out.out = True
            out.batted = True
        kind = w["kind"]
        if bowl is not None and kind in BOWLER_CREDITED:
            bowl.wickets += 1
            if kind in ("lbw", "bowled"):
                bowl.lbw_bowled += 1
        fielders = [f.get("name") for f in w.get("fielders", []) if f.get("name")]
        if kind == "caught and bowled" and bowl is not None:
            bowl.catches += 1
        elif kind == "caught":
            for f in fielders[:1]:
                if f in stats:
                    stats[f].catches += 1
        elif kind == "stumped":
            for f in fielders[:1]:
                if f in stats:
                    stats[f].stumpings += 1
        elif kind == "run out":
            if len(fielders) == 1 and fielders[0] in stats:
                stats[fielders[0]].runout_direct += 1
            else:
                for f in fielders[:2]:
                    if f in stats:
                        stats[f].runout_indirect += 1
