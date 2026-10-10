"""Narration layer.

Design rule: the engine computes every number; the LLM only turns a FACTS
JSON into one or two human sentences. A guard rejects any output that
contains a number not present in the facts, and we fall back to a
deterministic template. The demo therefore never shows a hallucinated
rank or points figure, and still works with no API key at all.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import OrderedDict

import httpx

from . import answers
from .metrics import metrics

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
TIMEOUT = float(os.getenv("LLM_TIMEOUT_S", "8"))
_sem = asyncio.Semaphore(int(os.getenv("LLM_CONCURRENCY", "4")))
_cache: OrderedDict[str, str] = OrderedDict()
_client: httpx.AsyncClient | None = None

SYSTEM = (
    "You are the live copilot inside a fantasy cricket app, talking to one fan about their team "
    "during a match. Write like a sharp friend watching alongside them: plain, specific, no hype, "
    "no emojis. Use ONLY numbers that appear in FACTS, written exactly as given (you may add "
    "thousands separators). Never invent stats, never predict outcomes with certainty, never give "
    "betting or money advice. Refer to players by surname after first mention."
)

TASKS = {
    "over": "In at most 2 sentences (max 45 words), explain why the fan's rank moved in this window, "
            "naming the player(s) that drove it and why ownership/captaincy made it matter.",
    "recovery": "In at most 3 sentences (max 60 words), tell the fan where they stand and which "
                "live players could still move them up, and the biggest threat they don't own.",
    "catchup": "In at most 3 sentences (max 60 words), catch the fan up on what they missed: match "
               "situation first, then what it did to their points and rank, and why.",
        "ask": "Answer exactly the fan's QUESTION, nothing else, in at most 3 sentences using only FACTS. "
           "If FACTS has a 'verdict', it is the engine's conclusion about that player: state it plainly "
           "and explain it using that player's ownership and captaincy numbers. Never contradict the "
           "verdict. If FACTS cannot answer, say so briefly.",
}


# ---------------- providers ----------------
def providers() -> list[str]:
    return _providers()


def _providers() -> list[str]:
    order = os.getenv("LLM_PROVIDERS", "gemini,groq,anthropic").split(",")
    keys = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
    return [p.strip() for p in order if p.strip() in keys and os.getenv(keys[p.strip()])]


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT)
    return _client


async def _call(provider: str, prompt: str) -> str:
    c = _http()
    if provider == "gemini":
        r = await c.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},  # header, never the URL
            json={"systemInstruction": {"parts": [{"text": SYSTEM}]},
                  "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.4, "maxOutputTokens": 400,
                                       "thinkingConfig": {"thinkingBudget": 0}}})
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    if provider == "groq":
        body = {
            "model": GROQ_MODEL,
            "temperature": 0.4,
            # reasoning models (gpt-oss, qwen3) spend tokens thinking before answering,
            # so give them room and keep the thinking short
            "max_tokens": 1200,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt}],
        }
        if "gpt-oss" in GROQ_MODEL:
            body["reasoning_effort"] = "low"
        r = await c.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"].get("content") or ""
    if provider == "anthropic":
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
            json={"model": ANTHROPIC_MODEL, "max_tokens": 200, "system": SYSTEM,
                  "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])
    raise ValueError(provider)


# ---------------- failure handling ----------------
_cooldown_until: dict[str, float] = {}
_KEY_ENV = ("GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY")
_KEY_PATTERN = re.compile(r"(key=|api[_-]?key[\"':= ]+|Bearer\s+)[A-Za-z0-9._\-]{8,}", re.I)


def redact(text: str) -> str:
    """Strip anything that looks like a credential before logging or displaying it."""
    for env in _KEY_ENV:
        val = os.getenv(env)
        if val and len(val) > 6:
            text = text.replace(val, "***")
    return _KEY_PATTERN.sub(lambda m: m.group(1) + "***", text)


def _describe_error(provider: str, e: Exception) -> tuple[str, float]:
    """Short, secret-free message and how long to stop calling this provider."""
    resp = getattr(e, "response", None)
    if resp is None:
        return f"{provider}: {type(e).__name__} (network/timeout)", 20
    status = resp.status_code
    msg = ""
    try:
        msg = resp.json().get("error", {}).get("message", "")
    except Exception:
        msg = resp.text[:200]
    if isinstance(msg, dict):
        msg = str(msg)
    cool = {401: 600, 402: 600, 403: 600, 404: 600, 429: 60}.get(status, 20)
    return redact(f"{provider}: HTTP {status} — {msg[:220]}"), cool


def cooldowns() -> dict[str, int]:
    now = time.time()
    return {p: round(t - now) for p, t in _cooldown_until.items() if t > now}


# ---------------- grounding guard ----------------
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _fact_numbers(obj, acc: set[float]) -> set[float]:
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        acc.update({float(obj), float(round(obj)), abs(float(obj)), float(round(abs(obj)))})
    elif isinstance(obj, str):
        for m in _NUM.findall(obj):
            acc.add(float(m.replace(",", "")))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _fact_numbers(k, acc)
            _fact_numbers(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _fact_numbers(v, acc)
    return acc


SAFE_NUMBERS = {0.0, 1.0, 2.0, 3.0, 10.0, 11.0, 20.0, 50.0, 100.0}  # "top 10%", "2x", "50" bonus…


def grounded(text: str, facts: dict) -> bool:
    allowed = _fact_numbers(facts, set(SAFE_NUMBERS))
    for m in _NUM.findall(text):
        v = float(m.replace(",", ""))
        if v not in allowed:
            return False
    return True


# ---------------- public API ----------------
async def narrate(kind: str, facts: dict, question: str | None = None) -> dict:
    """Returns {"text", "source", "latency_ms"}; never raises."""
    template = TEMPLATES[kind](facts, question)
    payload = json.dumps(facts, sort_keys=True, default=str)
    key = hashlib.sha1(f"{kind}|{question}|{payload}".encode()).hexdigest()
    if key in _cache:
        metrics.inc("cache_hits")
        _cache.move_to_end(key)
        return {"text": _cache[key], "source": "cache", "latency_ms": 0}

    prompt = f"TASK: {TASKS[kind]}\n\nFACTS:\n{payload}"
    if question:
        prompt += f"\n\nQUESTION: {question[:300]}"
    for provider in _providers():
        if _cooldown_until.get(provider, 0) > time.time():
            continue  # recently failed (no credits / rate limited): don't hammer it
        t0 = time.perf_counter()
        metrics.inc("llm_calls")
        try:
            async with _sem:
                text = (await _call(provider, prompt)).strip().strip('"')
        except Exception as e:
            msg, cool = _describe_error(provider, e)
            _cooldown_until[provider] = time.time() + cool
            metrics.last_llm_error = msg
            print(f"[llm] {msg} (pausing {provider} for {cool}s)")
            continue
        ms = (time.perf_counter() - t0) * 1000
        metrics.latency.append(ms)
        if not text or not grounded(text, facts):
            metrics.inc("guard_rejections")
            print(f"[llm] guard rejected {provider} output: {redact(text[:160])!r}")
            continue
        metrics.inc("llm_ok")
        metrics.provider_hits[provider] = metrics.provider_hits.get(provider, 0) + 1
        _cache[key] = text
        if len(_cache) > 2000:
            _cache.popitem(last=False)
        return {"text": text, "source": provider, "latency_ms": round(ms)}

    metrics.inc("template_fallbacks")
    return {"text": template, "source": "template", "latency_ms": 0}


# ---------------- deterministic templates ----------------
def _surname(name: str) -> str:
    return name.split()[-1]


def _driver_sentence(d: dict) -> str:
    n, pts = d["player"], d["fantasy_points_gained"]
    exp, own, cap = d["your_exposure"], d["field_ownership_pct"], d["field_captaincy_pct"]
    if d["effect_on_you"] == "helped":
        if exp == "captain":
            return f"{n} (+{pts:g}) carried you: he's your captain, but only {cap}% of teams captained him."
        if exp == "vice-captain":
            return f"{n} (+{pts:g}) paid off as your vice-captain."
        if exp == "in your team":
            if own <= 35:
                return f"{n} (+{pts:g}) is your edge: only {own}% of teams have him."
            return (f"{n} (+{pts:g}) helped: only {d.get('ownership_near_your_rank_pct', own)}% of "
                    f"teams near your rank have him.")
        return f"{n} gave up points that the field relied on, which helped you."
    if exp == "not in your team":
        return f"{n} (+{pts:g}) hurt: he's in {own}% of teams and you don't have him."
    return f"{n} scored {pts:g} for you, but {cap}% of teams captained him, so the field gained more."


def _over_template(f: dict, _q=None) -> str:
    if f["direction"] == "flat":
        head = f"Rank steady at #{f['rank_now']:,}."
    else:
        head = (f"{'Up' if f['direction'] == 'up' else 'Down'} {abs(f['ranks_moved']):,} ranks to "
                f"#{f['rank_now']:,}. You made {f['your_points_gained']:g} pts; teams around your rank made "
                f"{f['teams_near_you_gained_avg']:g}.")
    want = {"up": "helped", "down": "hurt"}.get(f["direction"])
    aligned = [d for d in f["drivers"] if d["effect_on_you"] == want] or f["drivers"]
    if aligned:
        head += " " + _driver_sentence(aligned[0])
    return head


def _recovery_template(f: dict, _q=None) -> str:
    if f["match_finished"]:
        return f"Match over. You finished #{f['rank_now']:,} of {f['contest_size']:,}."
    if f["in_top_10pct"]:
        s = f"You're in the top 10% at #{f['rank_now']:,}."
    else:
        s = f"You're #{f['rank_now']:,}, {f['points_to_top_10pct']:g} pts off the top 10%."
    if f["your_live_differentials"]:
        d = f["your_live_differentials"][0]
        s += f" Your best lever is {d['player']} ({d['status']}, in {d['field_ownership_pct']}% of teams)."
    if f["biggest_threats_you_dont_own"]:
        t = f["biggest_threats_you_dont_own"][0]
        s += f" Watch {t['player']}: {t['field_ownership_pct']}% of teams have him and he's {t['status']}."
    return s


def _catchup_template(f: dict, _q=None) -> str:
    lines = f.get("score_now", [])
    sc = " · ".join(f"{_short(l['team'])} {l['runs']}/{l['wickets']} ({l['overs']})" for l in lines if l["overs"] != "0.0")
    s = f"You missed {f['balls_missed']} balls. {sc}."
    if f.get("result"):
        s += f" {f['result']}."
    elif f.get("need") is not None:
        s += f" {f['need']} needed off {f['balls_left']}."
    s += " " + _over_template(f)
    return s


def _ask_template(f: dict, q=None) -> str:
    return answers.answer(q or "", f)


def _short(team: str) -> str:
    return "".join(w[0] for w in team.split() if w[0].isupper())


TEMPLATES = {"over": _over_template, "recovery": _recovery_template,
             "catchup": _catchup_template, "ask": _ask_template}