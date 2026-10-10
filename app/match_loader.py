"""Load a Cricsheet-format match into a flat, ordered list of ball events.

Works with the bundled simulated match and with any real T20 match JSON
downloaded from https://cricsheet.org/matches/.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass
class Player:
    id: int
    name: str
    team: str
    role: str          # WK | BAT | AR | BOWL
    credits: float
    popularity: float  # 0..1, drives simulated contest ownership


@dataclass
class Ball:
    seq: int
    innings: int                 # 1 or 2
    over: int                    # 0-based over index
    legal_in_over: int           # legal balls bowled in this over after this ball
    batting_team: str
    bowling_team: str
    batter: str
    non_striker: str
    bowler: str
    runs_batter: int
    runs_extras: int
    runs_total: int
    extras: dict = field(default_factory=dict)
    wicket: dict | None = None

    @property
    def legal(self) -> bool:
        return "wides" not in self.extras and "noballs" not in self.extras

    @property
    def label(self) -> str:
        return f"{self.over}.{self.legal_in_over}"

    def to_dict(self) -> dict:
        return {
            "seq": self.seq, "innings": self.innings, "over": self.over,
            "label": self.label, "batting_team": self.batting_team,
            "bowling_team": self.bowling_team, "batter": self.batter,
            "non_striker": self.non_striker, "bowler": self.bowler,
            "runs_batter": self.runs_batter, "runs_extras": self.runs_extras,
            "runs_total": self.runs_total, "extras": self.extras,
            "wicket": self.wicket, "legal": self.legal,
        }


@dataclass
class Match:
    title: str
    venue: str
    teams: list[str]
    players: list[Player]
    balls: list[Ball]
    simulated: bool

    def player_index(self) -> dict[str, int]:
        return {p.name: p.id for p in self.players}


def _infer_roles(info: dict, innings: list[dict]) -> dict[str, str]:
    """Fallback roles for real Cricsheet files without metadata."""
    bowled, batted, keepers = set(), set(), set()
    for inn in innings:
        for ov in inn["overs"]:
            for d in ov["deliveries"]:
                bowled.add(d["bowler"])
                batted.add(d["batter"])
                for w in d.get("wickets", []):
                    if w["kind"] == "stumped":
                        for f in w.get("fielders", []):
                            keepers.add(f.get("name"))
    roles = {}
    for team, names in info["players"].items():
        for n in names:
            if n in keepers:
                roles[n] = "WK"
            elif n in bowled and n in batted:
                roles[n] = "AR"
            elif n in bowled:
                roles[n] = "BOWL"
            else:
                roles[n] = "BAT"
        if not any(roles[n] == "WK" for n in names):
            bats = [n for n in names if roles[n] == "BAT"]
            if bats:
                roles[bats[-1]] = "WK"
    return roles


def load_match(match_file: str | Path | None = None, meta_file: str | Path | None = None) -> Match:
    path = Path(match_file) if match_file else DATA_DIR / "demo_match.json"
    raw = json.loads(path.read_text())
    info, innings = raw["info"], raw["innings"]

    meta = {}
    mpath = Path(meta_file) if meta_file else DATA_DIR / "players_meta.json"
    if mpath.exists():
        meta = json.loads(mpath.read_text())
    inferred = _infer_roles(info, innings)

    players: list[Player] = []
    for team in info["teams"]:
        for name in info["players"][team]:  # includes impact subs in real files
            m = meta.get(name, {})
            players.append(Player(
                id=len(players), name=name, team=team,
                role=m.get("role", inferred.get(name, "BAT")),
                credits=float(m.get("credits", 8.0)),
                popularity=float(m.get("popularity", 0.35)),
            ))

    balls: list[Ball] = []
    for i, inn in enumerate(innings[:2]):
        bat = inn["team"]
        bowl = [t for t in info["teams"] if t != bat][0]
        for ov in inn["overs"]:
            legal = 0
            for d in ov["deliveries"]:
                extras = d.get("extras", {})
                if "wides" not in extras and "noballs" not in extras:
                    legal += 1
                wk = d.get("wickets", [None])[0]
                balls.append(Ball(
                    seq=len(balls), innings=i + 1, over=ov["over"], legal_in_over=legal,
                    batting_team=bat, bowling_team=bowl, batter=d["batter"],
                    non_striker=d["non_striker"], bowler=d["bowler"],
                    runs_batter=d["runs"]["batter"], runs_extras=d["runs"]["extras"],
                    runs_total=d["runs"]["total"], extras=extras, wicket=wk,
                ))

    t1, t2 = info["teams"]
    simulated = "simulated" in json.dumps(raw.get("meta", {})).lower() or \
                "simulated" in info.get("event", {}).get("name", "").lower()
    return Match(
        title=f"{t1} vs {t2}", venue=info.get("venue", ""), teams=info["teams"],
        players=players, balls=balls, simulated=simulated,
    )
