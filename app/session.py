"""Per-viewer sessions.

Each viewer gets their own replay so a reviewer always starts from ball 1.

    producer ──XADD──▶ stream match:{sid} ──XREAD──▶ worker
                                                   │ engine.step()  (points, rank)
                                                   │ alerts.evaluate (relevance filter)
                                                   │ over end ─▶ llm.narrate (async, non-blocking)
                                                   ▼
                                           WebSocket fan-out to the viewer's tabs
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field as dc_field

from fastapi import WebSocket

from . import llm
from .alerts import AlertState, evaluate
from .engine import MatchEngine, UserTeam
from .field import Field
from .match_loader import Match
from .metrics import metrics

BALL_INTERVAL = float(os.getenv("BALL_INTERVAL_S", "2.0"))
SPEEDS = (1, 2, 5, 20)
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "100"))
IDLE_TTL = int(os.getenv("SESSION_IDLE_TTL_S", "900"))


class TeamError(ValueError):
    pass


def validate_team(match: Match, ids: list[int], captain: int, vice: int) -> UserTeam:
    players = match.players
    ids = list(dict.fromkeys(int(i) for i in ids))
    if len(ids) != 11:
        raise TeamError("Pick exactly 11 players.")
    if any(i < 0 or i >= len(players) for i in ids):
        raise TeamError("Unknown player in team.")
    if captain not in ids or vice not in ids or captain == vice:
        raise TeamError("Choose a captain and a different vice-captain from your 11.")
    credits = sum(players[i].credits for i in ids)
    if credits > 100:
        raise TeamError(f"Team uses {credits:g} credits; the limit is 100.")
    per_side: dict[str, int] = {}
    roles: dict[str, int] = {}
    for i in ids:
        per_side[players[i].team] = per_side.get(players[i].team, 0) + 1
        roles[players[i].role] = roles.get(players[i].role, 0) + 1
    if max(per_side.values()) > 10:
        raise TeamError("Max 10 players from one side.")
    for role in ("WK", "BAT", "AR", "BOWL"):
        if roles.get(role, 0) < 1:
            raise TeamError(f"Pick at least one {role}.")
        if roles.get(role, 0) > 8:
            raise TeamError(f"Max 8 {role} players.")
    return UserTeam(ids, captain, vice)


@dataclass
class Session:
    id: str
    match: Match
    field: Field
    team: UserTeam
    bus: object
    engine: MatchEngine = None
    alerts_state: AlertState = None
    sockets: set = dc_field(default_factory=set)
    alerts: list = dc_field(default_factory=list)
    insights: list = dc_field(default_factory=list)
    speed: int = 1
    paused: bool = False
    started: bool = False
    last_active: float = dc_field(default_factory=time.time)
    _tasks: list = dc_field(default_factory=list)
    _resume: asyncio.Event = dc_field(default_factory=asyncio.Event)
    _generation: int = 0

    def __post_init__(self):
        self.reset_state()
        self._resume.set()

    @property
    def stream(self) -> str:
        return f"match:{self.id}:{self._generation}"

    def reset_state(self):
        self.engine = MatchEngine(self.match, self.field, self.team)
        self.alerts_state = AlertState()
        self.alerts, self.insights = [], []

    # ---------- lifecycle ----------
    def start(self):
        if self.started:
            return
        self.started = True
        self._tasks = [asyncio.create_task(self._producer(self._generation)),
                       asyncio.create_task(self._consumer(self._generation))]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        self.started = False
        await self.bus.drop(self.stream)

    async def restart(self):
        await self.stop()
        self._generation += 1
        self.reset_state()
        self.paused = False
        self._resume.set()
        await self.broadcast({"type": "init", **self.snapshot()})
        self.start()

    def set_speed(self, s: int):
        self.speed = s if s in SPEEDS else 1

    def set_paused(self, p: bool):
        self.paused = p
        (self._resume.clear if p else self._resume.set)()

    # ---------- pipeline ----------
    async def _producer(self, gen: int):
        """Simulates the live feed: publishes one delivery at a time."""
        stream = self.stream
        for ball in self.match.balls:
            await self._resume.wait()
            await self.bus.publish(stream, {"type": "ball", "gen": gen, **ball.to_dict()})
            await asyncio.sleep(BALL_INTERVAL / self.speed)

    async def _consumer(self, gen: int):
        async for event in self.bus.consume(self.stream):
            if event.get("gen") != gen or self.engine.done:
                continue
            if event["seq"] != self.engine.cursor:  # idempotency: skip replays/dupes
                continue
            update = self.engine.step()
            metrics.inc("balls_processed")
            alerts = evaluate(self.engine, update, self.alerts_state)
            if alerts:
                metrics.inc("alerts_sent", len(alerts))
            else:
                metrics.inc("balls_filtered")
            await self.broadcast({"type": "tick", **_tick_payload(self.engine, update),
                                  "filtered": self.alerts_state.filtered})
            for a in alerts:
                a["at"] = update["chart"]["label"]
                a["seq"] = update["ball"]["seq"]
                self.alerts.append(a)
                await self.broadcast({"type": "alert", "alert": a})
            if update.get("over_summary"):
                asyncio.create_task(self._insight("over", update["over_summary"], update, gen))
            if update["innings_end"]:
                asyncio.create_task(self._insight("recovery", self.engine.recovery_facts(), update, gen))
            if update["match_end"]:
                await self.broadcast({"type": "end", "result": update["score"].get("result")})
                return

    async def _insight(self, kind: str, facts: dict, update: dict, gen: int):
        res = await llm.narrate(kind, facts)
        if gen != self._generation:
            return
        item = {"kind": kind, "at": update["chart"]["label"], "seq": update["ball"]["seq"],
                "facts": facts, **res}
        self.insights.append(item)
        await self.broadcast({"type": "insight", "insight": item})

    # ---------- fan-out ----------
    async def broadcast(self, msg: dict):
        dead = []
        for ws in list(self.sockets):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)

    def snapshot(self) -> dict:
        e = self.engine
        return {
            "session_id": self.id,
            "match": {"title": e.match.title, "venue": e.match.venue, "teams": e.match.teams,
                      "simulated": e.match.simulated, "total_balls": len(e.match.balls)},
            "contest_size": e.contest_size,
            "team": {"player_ids": self.team.player_ids, "captain": self.team.captain,
                     "vice_captain": self.team.vice_captain},
            "board": e.player_board(),
            "history": e.history,
            "score": e.score_state(),
            "rank": e.last_rank,
            "user_points": e.user_points(),
            "cursor": e.cursor,
            "alerts": self.alerts[-30:],
            "insights": self.insights[-30:],
            "speed": self.speed,
            "paused": self.paused,
            "filtered": self.alerts_state.filtered,
            "done": e.done,
        }


def _tick_payload(e: MatchEngine, u: dict) -> dict:
    changed = {m["id"] for m in u["movers"]}
    ball = e.match.balls[u["ball"]["seq"]]
    for name in (ball.batter, ball.non_striker, ball.bowler):
        if name in e.idx:
            changed.add(e.idx[name])
    board = [{"id": i, "points": float(e.p[i]), "delta": next((m["delta"] for m in u["movers"] if m["id"] == i), 0),
              "line": e.stats[e.names[i]].line(), "breakdown": e.stats[e.names[i]].breakdown()}
             for i in sorted(changed)]
    return {k: u[k] for k in ("ball", "commentary", "score", "user_points", "user_delta", "rank",
                              "rank_delta", "chart", "over_end", "innings_end", "match_end")} | {
        "board": board, "cursor": e.cursor}


class SessionManager:
    def __init__(self, match: Match, field: Field, bus):
        self.match, self.field, self.bus = match, field, bus
        self.sessions: dict[str, Session] = {}

    def create(self, team: UserTeam) -> Session:
        if len(self.sessions) >= MAX_SESSIONS:
            idle = sorted(self.sessions.values(), key=lambda s: s.last_active)
            for s in idle[: max(1, len(idle) // 10)]:
                asyncio.create_task(self.remove(s.id))
        sid = uuid.uuid4().hex[:12]
        s = Session(id=sid, match=self.match, field=self.field, team=team, bus=self.bus)
        self.sessions[sid] = s
        metrics.inc("sessions_created")
        return s

    def get(self, sid: str) -> Session | None:
        s = self.sessions.get(sid)
        if s:
            s.last_active = time.time()
        return s

    async def remove(self, sid: str):
        s = self.sessions.pop(sid, None)
        if s:
            await s.stop()

    async def janitor(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for sid, s in list(self.sessions.items()):
                if not s.sockets and now - s.last_active > IDLE_TTL:
                    await self.remove(sid)


async def attach(ws: WebSocket, s: Session):
    s.sockets.add(ws)
    await ws.send_json({"type": "init", **s.snapshot()})
