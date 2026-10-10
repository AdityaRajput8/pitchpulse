# PitchPulse — a live copilot for your fantasy team

Once a Dream11 team locks, the fan becomes a spectator of a number. Their rank
jumps thousands of places every over and nothing tells them **why**, whether they
can **still recover**, or what happened **while they were away**. Notifications
ping on every wicket, relevant or not.

PitchPulse watches the match ball by ball, recomputes the fan's fantasy points and
rank against a 20,000-team mega contest, and explains every swing in plain language.

| Fan problem | What PitchPulse does |
|---|---|
| "Why did my rank just crash?" | After every over: *"Down 771 ranks to #1,608. Dhoni (+12) hurt: he's in 71% of teams and you don't have him."* Expand to see the numbers. |
| "Can I still win this?" | **How do I climb?** shows points to the top-10% cutoff, your live differentials, and the biggest threat you don't own. |
| "I missed 5 overs" | Leave the tab and come back: a *While you were away* card summarises the match situation and what it did to your rank. |
| Notification noise | Every ball goes to the stream; only moments that move *your* team become alerts (≈89% of balls filtered). |

## Architecture

```
ball feed (replay or live API)
   │  XADD match:{session}
   ▼
Redis Streams  ──────────── in-memory fallback (zero-config local dev)
   │  XREAD
   ▼
worker ─ MatchEngine
   │       ├─ Dream11 T20 scoring per player           (app/scoring.py)
   │       ├─ rank vs 20k teams: field_points = M @ p   (app/field.py, <1 ms/ball)
   │       └─ rank-swing attribution vs teams NEAR you  (app/engine.py)
   ├─ relevance filter → alerts                         (app/alerts.py)
   ├─ over end → LLM narrator (async, never blocks the feed)  (app/llm.py)
   │      Gemini → Groq → Claude → rule-based template
   │      number-grounding guard rejects any unseen number
   ▼
WebSocket fan-out → dashboard (static/, served by the same FastAPI app)
```

### Decisions worth defending

- **The engine computes, the LLM narrates.** Every rank, point and ownership figure comes
  from deterministic code. The model only turns a facts JSON into a sentence, and
  `grounded()` rejects output containing any number not present in the facts. With no API
  key at all, rule-based templates keep the product fully working.
- **Attribution uses your rank neighbourhood, not the field average.** Your rank moves
  because of the ~1,000 teams closest to you. For each player:
  `leverage = (your multiplier − neighbours' avg multiplier) × Δpoints`. This is why the app
  can say a player *in* your team hurt you (the teams around you captained him).
- **Vectorised contest.** 20k teams as an (N × 22) multiplier matrix. Field generation went
  from 8.6 s (Python loop) to 0.1 s using Gumbel-top-k sampling, so cold starts stay fast.
- **Secrets never leave the server.** Keys go in request headers, not URLs, and every logged or
  displayed error passes through `redact()`.
- **Replay mode by default.** Reviewers can open the link any time. Each viewer gets their
  own replay from ball 1, with 1x/2x/5x/20x speed, pause and restart.
- **Redis Streams over Kafka.** Same producer/consumer contract, free to host (Upstash).
  `app/bus.py` is a two-method interface, so Kafka drops in.

### Honest limitations

- The bundled match is **simulated** (realistic ball-by-ball, Cricsheet format). Any real T20
  match from [cricsheet.org](https://cricsheet.org/matches/) works via `MATCH_FILE`.
- The contest field is **simulated** with realistic ownership and captaincy bias; in
  production it would be the platform's real contest snapshot.
- Scoring follows Dream11's published T20 table as of writing; values live in `RULES` in
  `app/scoring.py` for easy checking.
- Sessions are in-process, so the service runs one worker. Scaling out means moving session
  state to Redis and pinning WebSockets.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional: add GEMINI_API_KEY
uvicorn app.main:app --reload --env-file .env
# open http://localhost:8000 → "Use demo team & start"
pytest -q                       # core logic tests, no keys needed
```

Regenerate the simulated match: `python scripts/generate_demo_match.py`.

## Deploy (Render, free)

1. Push this folder to GitHub.
2. Render → New → Blueprint → pick the repo (uses `render.yaml`).
3. Add `GEMINI_API_KEY` (aistudio.google.com) and/or `GROQ_API_KEY` (console.groq.com, free
   tier). The narrator tries them in order and pauses any provider that returns 402/429
   (no credits / rate limit) for a few minutes. Optional:
   `REDIS_URL` from an Upstash Redis database (use the `rediss://` URL).
4. Open the service URL. Free instances sleep after inactivity; the first load can take
   ~50 s. Open it yourself a minute before your demo.

WebSockets work on Render; Vercel serverless can't hold them, which is why the API and
dashboard ship as one Render service.

## Demo-round walkthrough (5 minutes)

1. **The problem (30 s):** show the team builder. "Once this locks, fans watch a rank they
   can't explain."
2. **Live (90 s):** demo team at 5x. Point at the rank chart turning red, the alert that fires,
   and the AI explanation. Expand *Show the numbers*.
3. **Away (45 s):** switch tabs for ~10 seconds at 20x, come back to the catch-up card.
4. **Recovery (30 s):** click *How do I climb?* during the chase.
5. **Engineering (90 s):** Observability tab: noise filtered %, guard rejections, LLM
   latency, event bus. Walk the pipeline diagram and the neighbourhood-attribution idea.
