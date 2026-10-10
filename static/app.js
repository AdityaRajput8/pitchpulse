/* PitchPulse dashboard client — plain JS, no build step. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n) => Number(n).toLocaleString("en-IN", { maximumFractionDigits: 1 });
  const short = (team) => team.split(" ").filter((w) => /^[A-Z]/.test(w)).map((w) => w[0]).join("");
  const initials = (name) => name.split(" ").map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* storage unavailable */ } },
  };
  const ROLES = ["WK", "BAT", "AR", "BOWL"];
  const ROLE_NAME = { WK: "Wicket-keeper", BAT: "Batter", AR: "All-rounder", BOWL: "Bowler" };

  const S = {
    info: null, sel: new Set(), cap: null, vc: null, roleFilter: "ALL",
    sid: null, ws: null, retries: 0, retryTimer: null,
    snap: null, board: new Map(), history: [], team: null, score: null, spark: new Map(),
    chart: null, chartMode: "rank", marketFilter: "mine", rankMoved: false,
    lastSeq: -1, ackSeq: null, hiddenSeq: null, flashes: new Set(), feedN: 0,
  };
  const teamIdx = (team) => (S.info && S.info.teams.indexOf(team) === 1 ? 1 : 0);

  // ======================= views =======================
  function show(view) {
    for (const v of ["builder", "live", "obs"]) $(v).hidden = v !== view;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active",
      t.dataset.view === (view === "builder" ? "live" : view)));
    if (view === "obs") pollMetrics();
  }
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    if (t.dataset.view === "live") show(S.sid ? "live" : "builder"); else show("obs");
  }));

  // ======================= team builder =======================
  async function loadMatch() {
    const r = await fetch("/api/match");
    S.info = await r.json();
    const [a, b] = S.info.teams;
    $("crest0").textContent = short(a);
    $("crest1").textContent = short(b);
    $("bMatchTitle").textContent = `${a} vs ${b}`;
    $("heroMeta").textContent = `${S.info.venue}${S.info.simulated ? " · simulated replay" : ""} · ${S.info.total_balls} deliveries`;
    $("bContest").textContent = fmt(S.info.contest_size);
    renderBuilder();
  }

  function teamCheck() {
    const ps = [...S.sel].map((id) => S.info.players[id]);
    const credits = ps.reduce((a, p) => a + p.credits, 0);
    const roles = Object.fromEntries(ROLES.map((r) => [r, ps.filter((p) => p.role === r).length]));
    const sides = {};
    ps.forEach((p) => { sides[p.team] = (sides[p.team] || 0) + 1; });
    let why = "";
    if (S.sel.size < 11) why = `Pick ${11 - S.sel.size} more player${11 - S.sel.size > 1 ? "s" : ""}`;
    else if (credits > 100) why = `Over budget by ${fmt(credits - 100)} credits`;
    else if (ROLES.some((r) => roles[r] < 1)) why = `Add at least one ${ROLE_NAME[ROLES.find((r) => roles[r] < 1)].toLowerCase()}`;
    else if (ROLES.some((r) => roles[r] > 8)) why = `Max 8 ${ROLE_NAME[ROLES.find((r) => roles[r] > 8)].toLowerCase()}s`;
    else if (Math.max(0, ...Object.values(sides)) > 10) why = "Max 10 players from one side";
    else if (S.cap == null) why = "Choose a captain (C)";
    else if (S.vc == null) why = "Choose a vice-captain (VC)";
    return { credits, roles, why };
  }

  function renderBuilder() {
    const { credits, roles, why } = teamCheck();
    // role tabs
    $("roleTabs").innerHTML = ["ALL", ...ROLES].map((r) =>
      `<button data-role="${r}" class="${S.roleFilter === r ? "on" : ""}">${r === "ALL" ? "All" : r} ${r === "ALL" ? `${S.sel.size}/11` : `(${roles[r]})`}</button>`).join("");
    // squads
    const byTeam = {};
    for (const p of S.info.players) (byTeam[p.team] ||= []).push(p);
    $("squads").innerHTML = Object.entries(byTeam).map(([team, ps]) => {
      const t = teamIdx(team);
      const shown = ps.filter((p) => S.roleFilter === "ALL" || p.role === S.roleFilter);
      const picked = ps.filter((p) => S.sel.has(p.id)).length;
      return `<div class="squad"><div class="squad-head"><span class="bar" style="background:var(--t${t})"></span><h2>${esc(team)}</h2><span class="cnt">${picked} picked</span></div>
      ${shown.map((p) => {
        const on = S.sel.has(p.id);
        return `<div class="prow ${on ? "sel" : ""}">
          <span class="avatar t${t}">${initials(p.name)}<small>${p.role}</small></span>
          <div><div class="pname">${esc(p.name)}</div>
            <div class="pmeta"><span class="own-mini"><span style="width:${p.selected_by}%"></span></span>Sel by ${p.selected_by}% · C ${p.captained_by}%</div></div>
          <span class="pcr">${p.credits} cr</span>
          <div class="cv">
            <button data-c="${p.id}" class="${S.cap === p.id ? "on" : ""}" ${on ? "" : "disabled"} aria-label="Make ${esc(p.name)} captain">C</button>
            <button data-v="${p.id}" class="${S.vc === p.id ? "on" : ""}" ${on ? "" : "disabled"} aria-label="Make ${esc(p.name)} vice-captain">VC</button>
          </div>
          <button class="add" data-add="${p.id}" aria-label="${on ? "Remove" : "Add"} ${esc(p.name)}">${on ? "−" : "+"}</button>
        </div>`;
      }).join("")}</div>`;
    }).join("");
    // team bar
    $("slots").innerHTML = Array.from({ length: 11 }, (_, i) => `<i class="${i < S.sel.size ? "on" : ""}"></i>`).join("");
    $("bCredits").textContent = fmt(100 - credits);
    $("bCredits").classList.toggle("down", credits > 100);
    $("bCap").textContent = S.cap != null ? S.info.players[S.cap].name : "—";
    $("bVc").textContent = S.vc != null ? S.info.players[S.vc].name : "—";
    $("checks").innerHTML = ROLES.map((r) => `<span class="check ${roles[r] >= 1 && roles[r] <= 8 ? "ok" : S.sel.size ? "bad" : ""}">${r} ${roles[r]}</span>`).join("");
    $("startBtn").disabled = Boolean(why);
    $("lockWhy").textContent = why || "Ready — your team locks at the first ball";
  }

  $("roleTabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    S.roleFilter = b.dataset.role; renderBuilder();
  });
  $("squads").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    $("bErr").textContent = "";
    if (b.dataset.add != null) {
      const id = +b.dataset.add;
      if (S.sel.has(id)) { S.sel.delete(id); if (S.cap === id) S.cap = null; if (S.vc === id) S.vc = null; }
      else if (S.sel.size < 11) S.sel.add(id);
      else $("bErr").textContent = "You already have 11. Remove someone first.";
    } else if (b.dataset.c != null) {
      const id = +b.dataset.c; S.cap = id; if (S.vc === id) S.vc = null;
    } else if (b.dataset.v != null) {
      const id = +b.dataset.v; S.vc = id; if (S.cap === id) S.cap = null;
    }
    renderBuilder();
  });
  $("clearBtn").addEventListener("click", () => { S.sel.clear(); S.cap = S.vc = null; renderBuilder(); });
  $("loadDemoBtn").addEventListener("click", () => {
    const d = S.info.demo_team; if (!d) return;
    S.sel = new Set(d.player_ids); S.cap = d.captain; S.vc = d.vice_captain; renderBuilder();
  });

  async function startSession(team) {
    $("bErr").textContent = "";
    $("startBtn").disabled = true; $("demoBtn").disabled = true;
    try {
      const r = await fetch("/api/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(team) });
      const j = await r.json();
      if (!r.ok) {
        const d = j.detail;
        $("bErr").textContent = Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d || "Couldn't lock that team.";
        return;
      }
      await endCurrentSession();
      S.sid = j.session_id; store.set("pp_sid", S.sid);
      connect();
    } catch {
      $("bErr").textContent = "Server unreachable. Is uvicorn running?";
    } finally {
      $("demoBtn").disabled = false; renderBuilder();
    }
  }
  $("startBtn").addEventListener("click", () => startSession({ player_ids: [...S.sel], captain: S.cap, vice_captain: S.vc }));
  $("demoBtn").addEventListener("click", () => {
    const d = S.info.demo_team;
    if (!d) { $("bErr").textContent = "No demo team for this match. Pick your 11."; return; }
    startSession(d);
  });
  $("newTeamBtn").addEventListener("click", async () => {
    // pre-fill the builder with the current team so it's easy to tweak
    if (S.team) { S.sel = new Set(S.team.player_ids); S.cap = S.team.captain; S.vc = S.team.vice_captain; }
    await endCurrentSession();
    $("controls").hidden = true; $("liveChip").hidden = true;
    renderBuilder(); show("builder");
  });

  // ======================= websocket (exactly one live socket) =======================
  function disconnect() {
    clearTimeout(S.retryTimer);
    if (S.ws) { S.ws.onclose = null; S.ws.onmessage = null; try { S.ws.close(); } catch { /* already closed */ } }
    S.ws = null;
  }
  async function endCurrentSession() {
    const old = S.sid;
    disconnect();
    S.sid = null; store.del("pp_sid");
    if (old) fetch(`/api/session/${old}`, { method: "DELETE" }).catch(() => {});
  }
  function connect() {
    disconnect();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/${S.sid}`);
    S.ws = ws;
    ws.onopen = () => { S.retries = 0; };
    ws.onmessage = (ev) => { if (ws === S.ws) handle(JSON.parse(ev.data)); };
    ws.onclose = () => {
      if (ws !== S.ws || !S.sid || S.retries > 8) return;
      S.retries++;
      S.retryTimer = setTimeout(connect, Math.min(1000 * S.retries, 5000));
    };
  }
  const send = (o) => S.ws && S.ws.readyState === 1 && S.ws.send(JSON.stringify(o));

  function handle(m) {
    if (m.type === "init") return onInit(m);
    if (m.type === "tick") return onTick(m);
    if (m.type === "alert") { pushFeed(alertItem(m.alert)); if (m.alert.relevance >= 0.75) toast(m.alert); return; }
    if (m.type === "insight") return onInsight(m.insight);
    if (m.type === "status") return onStatus(m);
    if (m.type === "end") { $("pulseText").textContent = `${m.result}. Hit Restart to replay, or change your team.`; $("liveChipText").textContent = "Full time"; return; }
    if (m.type === "error") {
      disconnect(); S.sid = null; store.del("pp_sid");
      $("controls").hidden = true; $("liveChip").hidden = true; show("builder");
      $("bErr").textContent = m.message;
    }
  }

  function onInit(m) {
    S.snap = m; S.team = m.team; S.history = m.history.slice(); S.score = m.score;
    S.board = new Map(m.board.map((p) => [p.id, { ...p, lastDelta: 0 }]));
    S.spark = new Map(m.board.map((p) => [p.id, [p.points]]));
    S.lastSeq = m.cursor - 1; S.ackSeq = null; S.rankMoved = m.cursor > 6; S.feedN = 0;
    $("kSize").textContent = fmt(m.contest_size);
    $("controls").hidden = false; $("liveChip").hidden = false;
    $("streamName").textContent = "match:" + m.session_id;
    $("feed").innerHTML = ""; $("stream").innerHTML = ""; $("answer").hidden = true;
    [...m.alerts.map(alertItem), ...m.insights.map(insightItem)].sort((a, b) => a.seq - b.seq).forEach(pushFeed);
    onStatus(m);
    show("live");
    renderKpis(null);
    renderMarket();
    buildChart();
    $("filteredCount").textContent = m.filtered;
    refreshAiPill();
    if (!m.cursor) { $("pulseText").textContent = "Toss done, teams locked. First ball coming up."; $("liveChipText").textContent = "Starting"; }
  }

  function onStatus(m) {
    document.querySelectorAll(".speed button").forEach((b) => b.classList.toggle("on", +b.dataset.speed === m.speed));
    $("pauseBtn").textContent = m.paused ? "Resume" : "Pause";
    $("pauseBtn").dataset.paused = m.paused ? "1" : "";
  }
  document.querySelectorAll(".speed button").forEach((b) => b.addEventListener("click", () => send({ cmd: "speed", value: +b.dataset.speed })));
  $("pauseBtn").addEventListener("click", () => send({ cmd: $("pauseBtn").dataset.paused ? "resume" : "pause" }));
  $("restartBtn").addEventListener("click", () => send({ cmd: "restart" }));

  // ======================= ticks =======================
  function onTick(t) {
    S.lastSeq = t.ball.seq; S.score = t.score;
    S.history.push(t.chart);
    if (t.rank_delta) S.rankMoved = true;
    S.flashes = new Set();
    for (const p of S.board.values()) p.lastDelta = 0;
    for (const b of t.board) {
      const p = S.board.get(b.id); if (!p) continue;
      Object.assign(p, { points: b.points, line: b.line, breakdown: b.breakdown, lastDelta: b.delta });
      if (b.delta) S.flashes.add(b.id);
    }
    for (const p of S.board.values()) {
      const arr = S.spark.get(p.id) || [];
      if (arr[arr.length - 1] !== p.points || t.over_end) arr.push(p.points);
      if (arr.length > 60) arr.shift();
      S.spark.set(p.id, arr);
    }
    renderKpis(t);
    renderMarket();
    updateChart();
    pushStream(t);
    $("filteredCount").textContent = t.filtered;
    $("pulseText").textContent = pulse(t);
    const inn = t.score.lines[t.score.innings - 1];
    $("liveChipText").textContent = t.match_end ? "Full time" : `${short(inn.team)} ${inn.runs}/${inn.wickets} · ${inn.overs} ov`;
  }

  const counters = new WeakMap();
  function countTo(el, to, { prefix = "" } = {}) {
    const from = counters.get(el) ?? to;
    counters.set(el, to);
    if (reduceMotion || from === to) { el.textContent = prefix + fmt(to); return; }
    const t0 = performance.now(), dur = 450;
    const step = (now) => {
      const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      el.textContent = prefix + fmt(Math.round(from + (to - from) * e));
      if (k < 1 && counters.get(el) === to) requestAnimationFrame(step);
      else if (k >= 1) el.textContent = prefix + fmt(to);
    };
    requestAnimationFrame(step);
  }

  function renderKpis(t) {
    const h = S.history[S.history.length - 1];
    if (!h) return;
    countTo($("kPoints"), h.user_points);
    if (t && t.user_delta) {
      $("kPointsD").innerHTML = `<span class="up">+${fmt(t.user_delta)}</span> on that ball`;
      flash($("kPoints").parentElement, true);
    }
    const card = $("rankCard");
    if (!S.rankMoved) { $("kRank").textContent = "Level"; }
    else countTo($("kRank"), h.rank, { prefix: "#" });
    if (t && t.rank_delta) {
      const up = t.rank_delta > 0;
      $("kRankD").innerHTML = `<span class="${up ? "up" : "down"}">${up ? "▲" : "▼"} ${fmt(Math.abs(t.rank_delta))}</span> · of ${fmt(S.snap.contest_size)}`;
      card.classList.toggle("down", !up); card.classList.toggle("up", up);
    }
    const gap = h.top10_cutoff - h.user_points;
    countTo($("kGap"), h.top10_cutoff);
    $("kGapD").innerHTML = !S.rankMoved ? "Everyone starts level"
      : Math.abs(gap) < 0.5 ? "You're right on the cutoff"
      : gap > 0 ? `<span class="down">${fmt(gap)} pts</span> behind it` : `<span class="up">Inside by ${fmt(-gap)} pts</span>`;
    const sc = S.score, inn = sc.lines[sc.innings - 1];
    $("kMatchK").textContent = `${short(inn.team)} batting`;
    $("kScore").textContent = `${inn.runs}/${inn.wickets}`;
    $("kScoreD").textContent = sc.result ? sc.result
      : sc.innings === 2 ? `${inn.overs} ov · need ${sc.need} off ${sc.balls_left}` : `${inn.overs} ov · CRR ${sc.crr}`;
  }
  function flash(el, up) {
    el.classList.remove("flash-up", "flash-down"); void el.offsetWidth;
    el.classList.add(up ? "flash-up" : "flash-down");
  }
  function pulse(t) {
    const sc = t.score, inn = sc.lines[sc.innings - 1];
    if (sc.result) return sc.result + ".";
    let s = `${short(inn.team)} ${inn.runs}/${inn.wickets} (${inn.overs}). `;
    s += sc.innings === 2 ? `Need ${sc.need} off ${sc.balls_left}, required rate ${sc.rrr}. ` : `Run rate ${sc.crr}. `;
    return s + t.commentary + ".";
  }

  // ======================= market =======================
  function sparkline(vals, color) {
    if (vals.length < 2) return `<svg viewBox="0 0 100 28" preserveAspectRatio="none"></svg>`;
    const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
    const pts = vals.map((v, i) => `${(i / (vals.length - 1)) * 100},${26 - ((v - min) / span) * 24}`);
    return `<svg viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true">
      <polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
  }
  function renderMarket() {
    const mine = new Set(S.team.player_ids);
    let ps = [...S.board.values()];
    if (S.marketFilter === "mine") ps = ps.filter((p) => mine.has(p.id));
    ps.sort((a, b) => b.points - a.points);
    const turf = css("--turf"), haze = css("--dim");
    $("market").innerHTML = ps.map((p) => {
      const d = p.lastDelta || 0;
      const badge = p.id === S.team.captain ? "C 2x" : p.id === S.team.vice_captain ? "VC 1.5x" : "";
      const cls = S.flashes.has(p.id) ? (d >= 0 ? "flash-up" : "flash-down") : "";
      const vals = S.spark.get(p.id) || [];
      return `<button class="tick t${teamIdx(p.team)} ${mine.has(p.id) ? "mine" : ""} ${cls}" data-pid="${p.id}">
        <div class="row"><span class="nm">${esc(p.name)}</span>${badge ? `<span class="badge">${badge}</span>` : ""}</div>
        <div class="row"><span class="px">${fmt(p.points)}</span>
          <span class="chg ${d > 0 ? "up" : d < 0 ? "down" : ""}">${d ? (d > 0 ? "▲ +" : "▼ ") + fmt(d) : short(p.team) + " · " + p.role}</span></div>
        ${sparkline(vals, vals.length > 1 && vals.at(-1) > vals[0] ? turf : haze)}
        <div class="ln">${esc(p.line)}</div>
        <div class="ownt">In ${p.own}% of teams${p.cap >= 5 ? ` · ${p.cap}% captain` : ""}</div>
      </button>`;
    }).join("");
  }
  $("marketTabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    S.marketFilter = b.dataset.filter;
    $("marketTabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    renderMarket();
  });
  $("market").addEventListener("click", (e) => {
    const b = e.target.closest(".tick"); if (!b) return;
    const p = S.board.get(+b.dataset.pid);
    const rows = Object.entries(p.breakdown || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v > 0 ? "+" : ""}${v}</td></tr>`).join("");
    const mult = p.id === S.team.captain ? 2 : p.id === S.team.vice_captain ? 1.5 : S.team.player_ids.includes(p.id) ? 1 : 0;
    $("sheetBody").innerHTML = `<h2>${esc(p.name)}</h2>
      <p class="note">${esc(p.team)} · ${p.role} · ${esc(p.line)}</p>
      <table>${rows}<tr><td><b>Total</b></td><td><b>${fmt(p.points)}</b></td></tr>
      <tr><td>Worth to you (${mult ? mult + "x" : "not in your team"})</td><td>${fmt(p.points * mult)}</td></tr></table>
      <p class="note">In ${p.own}% of contest teams, captained in ${p.cap}%.</p>`;
    $("sheet").hidden = false;
  });
  const closeSheet = () => { $("sheet").hidden = true; };
  $("sheetX").addEventListener("click", closeSheet);
  $("sheet").addEventListener("click", (e) => { if (e.target.id === "sheet") closeSheet(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });

  // ======================= stream =======================
  function pushStream(t) {
    const b = t.ball, el = $("stream");
    const run = b.wicket ? "W" : b.extras.wides ? "wd" : b.extras.noballs ? "nb" : String(b.runs_total);
    const rc = b.wicket ? "rW" : b.runs_batter === 6 ? "r6" : b.runs_batter === 4 ? "r4" : "";
    const yd = t.user_delta ? `<span class="yd up">you +${fmt(t.user_delta)}</span>` : "";
    const li = document.createElement("li");
    li.className = b.wicket ? "hl-W" : b.runs_batter === 6 ? "hl-6" : "";
    li.innerHTML = `<span class="ov">${b.innings === 2 ? "② " : ""}${b.label}</span><span>${esc(t.commentary)}${yd}</span><span class="run ${rc}">${run}</span>`;
    el.prepend(li);
    if (t.over_end) {
      const h = S.history, now = h[h.length - 1];
      const before = h.slice(0, -1).reverse().find((x) => x.label.endsWith(".6") || x.label === "start" || x.innings !== now.innings) || h[0];
      const inn = t.score.lines[b.innings - 1];
      const moved = before.rank - now.rank;
      const br = document.createElement("li");
      br.className = "over-break";
      br.innerHTML = `End of over ${b.over + 1}: ${short(inn.team)} ${inn.runs}/${inn.wickets} · your rank <span class="${moved >= 0 ? "up" : "down"}">${moved >= 0 ? "▲" : "▼"} ${fmt(Math.abs(moved))}</span>`;
      el.prepend(br);
    }
    while (el.children.length > 200) el.lastChild.remove();
  }

  // ======================= copilot =======================
  function alertItem(a) {
    return { seq: a.seq ?? 0, html: `<li class="alert-${a.tone === "up" ? "up" : a.tone === "down" ? "down" : "info"}">
      <div class="meta"><span>${esc(a.at || "")}</span><span>Alert</span></div>
      <div class="t">${esc(a.title)}</div><div>${esc(a.body)}</div></li>` };
  }
  function factsTable(f) {
    if (!f || !f.drivers || !f.drivers.length) return "";
    return `<details><summary>Show the numbers</summary><table>
      ${f.drivers.map((d) => `<tr><td>${esc(d.player)}</td><td>+${d.fantasy_points_gained}</td><td>${esc(d.your_exposure)}</td><td>in ${d.field_ownership_pct}% · C ${d.field_captaincy_pct}%</td></tr>`).join("")}
      </table></details>`;
  }
  function srcBadge(src) {
    const t = src === "template" ? "rule-based" : src === "cache" ? "cached" : esc(src) + " · verified";
    return `<span class="src ${src === "template" ? "template" : ""}">${t}</span>`;
  }
  function insightItem(i) {
    const label = { over: "Why your rank moved", recovery: "Where you stand" }[i.kind] || "Copilot";
    return { seq: i.seq ?? S.lastSeq, html: `<li class="ai">
      <div class="meta"><span>${esc(i.at || "")}</span><span>${label}</span>${srcBadge(i.source)}</div>
      <div>${esc(i.text)}</div>${factsTable(i.facts)}</li>` };
  }
  function pushFeed(item) {
    $("feed").insertAdjacentHTML("afterbegin", item.html);
    const f = $("feed"); while (f.children.length > 80) f.lastChild.remove();
    S.feedN++; $("feedCount").textContent = `${S.feedN} so far`;
  }
  function onInsight(i) { pushFeed(insightItem(i)); }

  async function refreshAiPill() {
    try {
      const m = await (await fetch("/api/metrics")).json();
      const on = m.llm_providers && m.llm_providers.length;
      $("aiSource").textContent = on ? `AI: ${m.llm_providers.join(" → ")}` : "Rule-based (no API key)";
      $("aiSource").classList.toggle("ok", Boolean(on));
    } catch { /* leave as is */ }
  }

  // Pinned answer card: user questions never get pushed away by the live feed.
  let answerToken = 0;
  async function ask(label, path, body) {
    if (!S.sid) return;
    const token = ++answerToken;
    $("answer").hidden = false;
    $("answerQ").textContent = label;
    $("answerA").innerHTML = `<span class="thinking"><i></i><i></i><i></i></span>`;
    try {
      const r = await fetch(`/api/session/${S.sid}/${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      const j = await r.json();
      if (token !== answerToken) return; // a newer question replaced this one
      if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : "Request failed");
      $("answerA").innerHTML = `${esc(j.text)} ${srcBadge(j.source)}${factsTable(j.facts)}`;
    } catch (e) {
      if (token === answerToken) $("answerA").textContent = e.message;
    }
  }
  $("answerX").addEventListener("click", () => { answerToken++; $("answer").hidden = true; });
  $("askForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("askInput").value.trim(); if (q.length < 2) return;
    $("askInput").value = "";
    ask(`You asked: ${q}`, "ask", { question: q });
  });
  document.querySelector(".quick").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const q = b.dataset.q;
    if (q === "catchup") {
      const first = S.ackSeq === null;
      const since = first ? Math.max(-1, S.lastSeq - 12) : S.ackSeq;
      S.ackSeq = S.lastSeq;
      ask(first ? "Catch me up on the last 2 overs" : "Catch me up since I last asked", "catchup", { since_seq: since });
    } else if (q === "climb") ask("How do I climb?", "recovery");
    else ask(q, "ask", { question: q });
  });

  // "While you were away": auto catch-up when the fan returns to the tab
  document.addEventListener("visibilitychange", () => {
    if (!S.sid) return;
    if (document.hidden) { S.hiddenSeq = S.lastSeq; return; }
    if (S.hiddenSeq != null && S.lastSeq - S.hiddenSeq >= 6) ask("While you were away", "catchup", { since_seq: S.hiddenSeq });
    S.hiddenSeq = null;
  });

  // ======================= toasts =======================
  function toast(a) {
    const el = document.createElement("div");
    el.className = `toast ${a.tone === "up" ? "up" : a.tone === "down" ? "down" : ""}`;
    el.innerHTML = `<b>${esc(a.title)}</b>${esc(a.body)}`;
    $("toasts").prepend(el);
    while ($("toasts").children.length > 3) $("toasts").lastChild.remove();
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 300); }, 4500);
  }

  // ======================= charts =======================
  const CHARTS = {
    rank: { title: "Your rank, ball by ball", note: "Lower is better. Green while you're climbing, red while you're sliding." },
    race: { title: "Points race", note: "Your points against the top-10% cutoff and the median team." },
    worm: { title: "Match worm", note: "Cumulative runs by over for both sides." },
  };
  const glow = { id: "glow", beforeDatasetDraw(chart, args) {
    const c = chart.ctx; c.save(); c.shadowColor = args.meta.dataset?.options?.borderColor || "transparent";
    c.shadowBlur = args.index === 0 ? 12 : 0;
  }, afterDatasetDraw(chart) { chart.ctx.restore(); } };
  function gradient(chart, color) {
    const { ctx, chartArea } = chart; if (!chartArea) return "transparent";
    const g = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
    g.addColorStop(0, color + "55"); g.addColorStop(1, color + "00"); return g;
  }
  function chartData() {
    const turf = css("--turf"), red = css("--red"), haze = css("--haze"), amber = css("--amber"), flood = css("--flood");
    const h = S.history;
    if (S.chartMode === "rank") {
      return { labels: h.map((x) => x.label), datasets: [{
        label: "Rank", data: h.map((x) => x.rank), borderWidth: 2.5, pointRadius: 0, tension: .3, borderColor: turf,
        segment: { borderColor: (c) => (c.p1.parsed.y <= c.p0.parsed.y ? turf : red) },
      }] };
    }
    if (S.chartMode === "race") {
      return { labels: h.map((x) => x.label), datasets: [
        { label: "You", data: h.map((x) => x.user_points), borderColor: turf, borderWidth: 2.5, pointRadius: 0, tension: .3, fill: "origin",
          backgroundColor: (c) => gradient(c.chart, turf) },
        { label: "Top 10% cutoff", data: h.map((x) => x.top10_cutoff), borderColor: amber, borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, tension: .3 },
        { label: "Median team", data: h.map((x) => x.median), borderColor: haze, borderWidth: 1.5, pointRadius: 0, tension: .3 },
      ] };
    }
    const worm = S.score.worm, cum = (a) => a.reduce((acc, v) => (acc.push((acc.at(-1) || 0) + v), acc), []);
    return { labels: Array.from({ length: 20 }, (_, i) => String(i + 1)), datasets: S.score.lines.map((l, i) => {
      const col = i ? css("--t1") : css("--t0");
      return { label: l.team, data: cum(worm[i] || []), borderColor: col, borderWidth: 2.5, pointRadius: 2.5, pointBackgroundColor: col, tension: .25 };
    }) };
  }
  function buildChart() {
    if (!window.Chart) return;
    const grid = "rgba(150,182,232,.08)", haze = css("--haze");
    if (S.chart) S.chart.destroy();
    S.chart = new Chart($("chart"), {
      type: "line", data: chartData(), plugins: [glow],
      options: {
        animation: false, responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: S.chartMode !== "rank", labels: { color: haze, boxWidth: 12, usePointStyle: true } },
          tooltip: { backgroundColor: "rgba(10,18,34,.92)", borderColor: "rgba(150,182,232,.2)", borderWidth: 1, padding: 10,
            callbacks: { label: (c) => `${c.dataset.label}: ${S.chartMode === "rank" ? "#" : ""}${fmt(c.parsed.y)}` } } },
        scales: {
          x: { ticks: { color: haze, maxTicksLimit: 10, autoSkip: true }, grid: { color: grid }, border: { display: false } },
          y: { reverse: S.chartMode === "rank", ticks: { color: haze, callback: (v) => fmt(v) }, grid: { color: grid }, border: { display: false },
               title: { display: true, text: S.chartMode === "rank" ? "rank" : S.chartMode === "race" ? "fantasy points" : "runs", color: haze } },
        },
      },
    });
    $("chartTitle").textContent = CHARTS[S.chartMode].title;
    $("chartNote").textContent = CHARTS[S.chartMode].note;
  }
  function updateChart() {
    if (!S.chart) return buildChart();
    S.chart.data = chartData();
    S.chart.update("none");
  }
  $("chartTabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    S.chartMode = b.dataset.chart;
    $("chartTabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    buildChart();
  });

  // ======================= observability =======================
  let metricsTimer = null;
  async function pollMetrics() {
    clearTimeout(metricsTimer);
    if ($("obs").hidden) return;
    try {
      const m = await (await fetch("/api/metrics")).json();
      const cards = [
        ["Balls processed", fmt(m.balls_processed), "through the event bus"],
        ["Noise filtered", m.noise_filtered_pct + "%", "balls that didn't ping anyone"],
        ["Alerts sent", fmt(m.alerts_sent), "relevance-scored"],
        ["LLM providers", m.llm_providers?.length ? m.llm_providers.join(" → ") : "none", m.llm_providers?.length ? "keys found in env" : "add GEMINI_API_KEY to .env"],
        ["LLM narrations", `${fmt(m.llm_ok)} / ${fmt(m.llm_calls)}`, "verified / attempted"],
        ["Guard rejections", fmt(m.guard_rejections), "outputs with ungrounded numbers"],
        ["Rule-based fallbacks", fmt(m.template_fallbacks), "no key, error or rejected"],
        ["Cache hits", fmt(m.cache_hits), "identical facts reused"],
        ["Avg LLM latency", m.avg_llm_latency_ms + " ms", m.p95_llm_latency_ms ? `p95 ${m.p95_llm_latency_ms} ms` : "not enough calls yet"],
        ["Event bus", m.event_bus, "Redis Streams when REDIS_URL is set"],
        ["Active viewers", fmt(m.active_sessions), `${fmt(m.sessions_created)} sessions since boot`],
      ];
      $("obsGrid").innerHTML = cards.map(([k, v, d]) => `<article class="kpi glass"><span class="k">${k}</span><div class="v mono" style="font-size:26px">${esc(v)}</div><span class="d">${esc(d)}</span></article>`).join("");
      const paused = Object.entries(m.llm_cooldowns || {}).map(([p, s]) => `${p} paused ${s}s`).join(", ");
      $("obsErr").textContent = m.last_llm_error ? `Last LLM error — ${m.last_llm_error}${paused ? ` (${paused}; using fallback)` : ""}` : "";
    } catch { $("obsErr").textContent = "Metrics unavailable. Is the server running?"; }
    metricsTimer = setTimeout(pollMetrics, 3000);
  }

  // ======================= boot =======================
  loadMatch().then(() => {
    const sid = store.get("pp_sid");
    if (sid) { S.sid = sid; connect(); }
  }).catch(() => { $("bMatchTitle").textContent = "Server unreachable. It may be waking up — refresh in 30 seconds."; });
})();
