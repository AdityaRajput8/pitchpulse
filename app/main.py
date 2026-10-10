"""PitchPulse API.

REST  : team builder data, session creation, catch-up / recovery / ask copilot
WS    : /ws/{sid} live ball-by-ball stream + controls
Static: the dashboard (served from /static, one deploy, one link)
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field as PField

from . import llm
from .bus import make_bus
from .field import build_field
from .match_loader import load_match
from .metrics import metrics
from .session import SPEEDS, SessionManager, TeamError, attach, validate_team

STATIC = Path(__file__).resolve().parent.parent / "static"
DEMO_TEAM = ["Jitesh Sharma", "Phil Salt", "Virat Kohli", "Dewald Brevis", "Rachin Ravindra",
             "Shivam Dube", "Krunal Pandya", "Noor Ahmad", "Josh Hazlewood", "Suyash Sharma",
             "Matheesha Pathirana"]
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    match = load_match(os.getenv("MATCH_FILE") or None, os.getenv("PLAYERS_META_FILE") or None)
    fld = build_field(match.players, n_teams=int(os.getenv("CONTEST_SIZE", "20000")))
    bus = await make_bus()
    mgr = SessionManager(match, fld, bus)
    state.update(match=match, field=fld, mgr=mgr)
    metrics.llm_providers = llm.providers()
    janitor = asyncio.create_task(mgr.janitor())
    print(f"[pitchpulse] {match.title}: {len(match.balls)} balls, field={fld.size}, bus={bus.name}, "
          f"llm={llm.providers() or 'templates only (no API key found in env)'}")
    yield
    janitor.cancel()


app = FastAPI(title="PitchPulse", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class TeamIn(BaseModel):
    player_ids: list[int] = PField(min_length=11, max_length=11)
    captain: int
    vice_captain: int


class CatchupIn(BaseModel):
    since_seq: int = -1


class AskIn(BaseModel):
    question: str = PField(min_length=2, max_length=300)


def _session(sid: str):
    s = state["mgr"].get(sid)
    if not s:
        raise HTTPException(404, "Session expired. Build your team again to restart the replay.")
    return s


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/match")
def match_info():
    m, f = state["match"], state["field"]
    idx = m.player_index()
    demo = [idx[n] for n in DEMO_TEAM if n in idx]
    return {
        "title": m.title, "venue": m.venue, "teams": m.teams, "simulated": m.simulated,
        "contest_size": f.size + 1, "total_balls": len(m.balls),
        "players": [{"id": p.id, "name": p.name, "team": p.team, "role": p.role, "credits": p.credits,
                     "selected_by": round(float(f.own[p.id]) * 100, 1),
                     "captained_by": round(float(f.cap[p.id]) * 100, 1)} for p in m.players],
        "demo_team": {"player_ids": demo, "captain": idx.get("Phil Salt"),
                      "vice_captain": idx.get("Rachin Ravindra")} if len(demo) == 11 else None,
    }


@app.post("/api/session")
async def create_session(body: TeamIn):
    try:
        team = validate_team(state["match"], body.player_ids, body.captain, body.vice_captain)
    except TeamError as e:
        raise HTTPException(400, str(e))
    s = state["mgr"].create(team)
    return {"session_id": s.id}


@app.post("/api/session/{sid}/catchup")
async def catchup(sid: str, body: CatchupIn):
    s = _session(sid)
    facts = s.engine.catchup_facts(body.since_seq)
    if facts["balls_missed"] == 0:
        return {"text": "You're all caught up — nothing new since you last looked.", "source": "template"}
    return {**await llm.narrate("catchup", facts), "facts": facts}


@app.post("/api/session/{sid}/recovery")
async def recovery(sid: str):
    s = _session(sid)
    facts = s.engine.recovery_facts()
    return {**await llm.narrate("recovery", facts), "facts": facts}


@app.post("/api/session/{sid}/ask")
async def ask(sid: str, body: AskIn):
    s = _session(sid)
    facts = s.engine.ask_facts(s.insights)
    return await llm.narrate("ask", facts, question=body.question)


@app.delete("/api/session/{sid}")
async def end_session(sid: str):
    await state["mgr"].remove(sid)
    return {"ok": True}


@app.get("/api/metrics")
def get_metrics():
    return metrics.snapshot(len(state["mgr"].sessions)) | {"llm_cooldowns": llm.cooldowns()}


@app.websocket("/ws/{sid}")
async def ws_endpoint(ws: WebSocket, sid: str):
    await ws.accept()
    s = state["mgr"].get(sid)
    if not s:
        await ws.send_json({"type": "error", "message": "Session expired. Build your team again."})
        await ws.close()
        return
    await attach(ws, s)
    s.start()
    try:
        while True:
            msg = await ws.receive_json()
            cmd = msg.get("cmd")
            s.last_active = time.time()
            if cmd == "speed" and msg.get("value") in SPEEDS:
                s.set_speed(int(msg["value"]))
            elif cmd == "pause":
                s.set_paused(True)
            elif cmd == "resume":
                s.set_paused(False)
            elif cmd == "restart":
                await s.restart()
                continue
            await s.broadcast({"type": "status", "speed": s.speed, "paused": s.paused})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        s.sockets.discard(ws)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
