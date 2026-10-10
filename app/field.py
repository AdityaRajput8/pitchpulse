"""Simulated contest field.

Dream11 does not expose live contest leaderboards publicly, so we model a
mega contest: N teams built with realistic ownership bias (stars picked
more, captaincy concentrated on a few names). Each team is a row of
effective multipliers per player: 0 (not picked), 1, 1.5 (VC) or 2 (C).

With live points vector p, every team's score is one matrix product:
    field_points = M @ p
so ranking 20k teams after every ball costs well under a millisecond.
In production this would be the real contest snapshot from the platform.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .match_loader import Player

ROLE_MIN = {"WK": 1, "BAT": 3, "AR": 1, "BOWL": 3}
MAX_FROM_ONE_SIDE = 7
TEAM_SIZE = 11


@dataclass
class Field:
    M: np.ndarray            # (N, P) float32 effective multipliers
    own: np.ndarray          # (P,) fraction of teams picking player
    cap: np.ndarray          # (P,) fraction captaining
    vc: np.ndarray           # (P,) fraction vice-captaining
    eff: np.ndarray          # (P,) mean multiplier (expected exposure)

    @property
    def size(self) -> int:
        return self.M.shape[0]

    def rank_of(self, user_points: float, field_points: np.ndarray) -> int:
        return int(np.count_nonzero(field_points > user_points)) + 1

    def cutoff(self, field_points: np.ndarray, top_fraction: float) -> float:
        return float(np.quantile(field_points, 1 - top_fraction))


def _gumbel_keys(rng, n: int, weights: np.ndarray) -> np.ndarray:
    """Plackett-Luce sampling: top-k of log(w) + Gumbel noise is equivalent
    to drawing k items without replacement proportional to w."""
    return np.log(weights)[None, :] + rng.gumbel(size=(n, weights.shape[0]))


def _sample_teams(rng, n, players, pick_w, by_role, team_of):
    P = len(players)
    keys = _gumbel_keys(rng, n, pick_w)
    chosen = np.zeros((n, P), dtype=bool)
    rows = np.arange(n)[:, None]
    for role, need in ROLE_MIN.items():
        cols = np.array(by_role.get(role, []), dtype=int)
        k = min(need, len(cols))
        if k == 0:
            continue
        top = np.argsort(-keys[:, cols], axis=1)[:, :k]
        chosen[rows, cols[top]] = True
    remaining = TEAM_SIZE - chosen.sum(axis=1)
    masked = np.where(chosen, -np.inf, keys)
    order = np.argsort(-masked, axis=1)
    for r in np.unique(remaining):
        if r <= 0:
            continue
        sel = np.where(remaining == r)[0]
        chosen[sel[:, None], order[sel, :r]] = True
    side_a = (chosen & (team_of[None, :] == 0)).sum(axis=1)
    bad = np.maximum(side_a, TEAM_SIZE - side_a) > MAX_FROM_ONE_SIDE
    return chosen, bad


def build_field(players: list[Player], n_teams: int = 20000, seed: int = 11) -> Field:
    rng = np.random.default_rng(seed)
    P = len(players)
    pop = np.array([p.popularity for p in players], dtype=np.float64)
    pick_w = pop ** 1.4 + 0.02
    cap_w = pop ** 3.2 + 0.002
    vc_w = pop ** 2 + 0.01
    by_role: dict[str, list[int]] = {}
    for p in players:
        by_role.setdefault(p.role, []).append(p.id)
    team_of = np.array([0 if p.team == players[0].team else 1 for p in players])

    chosen, bad = _sample_teams(rng, n_teams, players, pick_w, by_role, team_of)
    for _ in range(30):  # resample only rows breaking the max-per-side rule
        idx = np.where(bad)[0]
        if idx.size == 0:
            break
        c2, b2 = _sample_teams(rng, idx.size, players, pick_w, by_role, team_of)
        chosen[idx], bad[idx] = c2, b2

    M = chosen.astype(np.float32)
    rows = np.arange(n_teams)
    ck = np.where(chosen, _gumbel_keys(rng, n_teams, cap_w), -np.inf)
    cap_idx = ck.argmax(axis=1)
    vk = np.where(chosen, _gumbel_keys(rng, n_teams, vc_w), -np.inf)
    vk[rows, cap_idx] = -np.inf
    vc_idx = vk.argmax(axis=1)
    M[rows, cap_idx] = 2.0
    M[rows, vc_idx] = 1.5

    return Field(
        M=M,
        own=(M > 0).mean(axis=0),
        cap=(M == 2.0).mean(axis=0),
        vc=(M == 1.5).mean(axis=0),
        eff=M.mean(axis=0),
    )
