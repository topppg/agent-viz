"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const terminal = new Set(["completed", "failed", "cancelled"]);
  // Any two differ by OKLab ΔE ≥ 15, and by ≥ 8 with simulated red-green colour blindness; each reaches 3:1
  // on every surface and stays ≥ 15 from the status colours. Bars draw them at full opacity. Re-check any edit.
  const PALETTE = ["#2795d8", "#93551e", "#018676", "#b36ca8", "#4460a2", "#85a149"];
  const QUIET_WINDOW = 30 * 60 * 1000;
  // A turn left unanswered in a still-open session stays yours this long.
  const OPEN_TURN_WINDOW = 12 * 3600 * 1000;
  const OPEN_IDLE = "open-idle";
  const MAX_ROWS = 300;
  const REST_KEY = "spiritboard.rested";
  const WIDTH_KEY = "spiritboard.columns";
  const KIND_NAMES = { codex: "Codex", claude: "Claude", opencode: "opencode", pi: "Pi", dsh: "DeepSeek Harness", grokbot: "Grok Bot" };
  const state = { snapshot: null, selected: null, view: "active", sort: null, descending: false, order: [], frozen: new Map(), split: 0, colors: new Map(), events: [], nextCursor: 0, more: false, eventBusy: false, request: 0, fetching: false, live: false, lastSnapshotAt: 0, fingerprint: null, historyOpen: false, tableKey: "", target: null, targetNote: "", targetFallback: false, latest: null, past: null, pastRequest: 0, zoom: 600, chart: null, chartAt: 0, rested: loadRested(), editing: null, draft: "", folds: new Map() };
  // Chart windows in seconds: heading, then the left and middle axis labels.
  const ZOOMS = { 600: ["last 10 minutes", "-10m", "-5m"], 3600: ["last hour", "-1h", "-30m"], 86400: ["last 24 hours", "-24h", "-12h"],
    604800: ["last 7 days", "-7d", "-3.5d"], 2592000: ["last 30 days", "-30d", "-15d"] };
  const node = (tag, text, className) => {
    const el = document.createElement(tag);
    if (text !== undefined && text !== null) el.textContent = String(text);
    if (className) el.className = className;
    return el;
  };
  const label = (value) => String(value ?? "unknown").replaceAll("_", " ");
  const time = (value) => {
    if (typeof value === "number") return value < 1e12 ? value * 1000 : value;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const liveNow = () => (time(state.latest?.server_time) || state.lastSnapshotAt || Date.now()) + Math.max(0, Date.now() - state.lastSnapshotAt);
  // While viewing the past, every age and status is judged at that moment.
  const serverNow = () => state.past ? state.past.at : liveNow();
  const span = (seconds) => seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${seconds / 60}m` : `${seconds / 3600}h`;
  const age = (value, now = serverNow()) => {
    const stamp = time(value);
    if (stamp === null) return "unknown";
    const seconds = Math.max(0, Math.floor((now - stamp) / 1000));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86400)}d`;
  };
  // Under a minute reads "just now", so open panels don't tick while you read them.
  const ago = (value) => time(value) !== null && serverNow() - time(value) < 60000 ? "just now" : `${age(value)} ago`;
  function showError(message) { $("error").hidden = !message; $("error").textContent = message || ""; }
  // Modes: "connecting" and "connected" stay quiet; only "down" asks for attention.
  let connection = ["Connecting…", "connecting"];
  function setConnection(text, mode) { connection = [text, mode]; showConnection(); }
  function showConnection() {
    if (state.past) { $("connection").textContent = "past · not live"; $("connection").className = "past"; return; }
    const [text, mode] = connection;
    $("connection").textContent = text; $("connection").className = mode;
  }
  function accountOf(run) { return typeof run.account === "object" ? (run.account?.label || run.account?.id || "unknown") : (run.account || "unknown"); }
  function sourceOf(run) { return typeof run.source === "object" ? (run.source?.kind || run.source?.label || "unknown") : (run.source || "unknown"); }

  function harnessOf(run) {
    const profiles = state.snapshot?.profiles || [];
    const profile = profiles.find((p) => run.session_id === `${p.id}:observed`);
    if (profile) return { key: `profile:${profile.id}`, label: profileLabel(profile) };
    return { key: `${sourceOf(run)}:${accountOf(run)}`, label: `${sourceOf(run)} · ${accountOf(run)}` };
  }
  function profileLabel(profile) {
    const same = (state.snapshot?.profiles || []).filter((p) => p.kind === profile.kind && p.label === profile.label).length > 1;
    // Discovered labels often already name the tool ("Claude · work"); do not repeat it.
    const tool = KIND_NAMES[profile.kind] || profile.kind;
    const named = [profile.kind, tool].some((x) => profile.label.toLowerCase().startsWith(x.toLowerCase()));
    const name = named ? profile.label : `${tool} · ${profile.label}`;
    return `${name}${same ? ` · ${profile.id}` : ""}`;
  }
  function healthOf(run) {
    const health = typeof run.health === "object" ? run.health?.state : run.health;
    if (health === "observed" || terminal.has(run.state)) return health || "unknown";
    const last = time(run.last_seen);
    if (health === "fresh" && last !== null && serverNow() - last > (state.snapshot?.stale_after_seconds || 30) * 1000) return "stale";
    return health || "unknown";
  }
  // A rest mark holds the turn's last_seen, so any later activity clears it.
  function loadRested() {
    try { return new Map(Object.entries(JSON.parse(localStorage.getItem(REST_KEY)) || {})); } catch (_) { return new Map(); }
  }
  function toggleRest(run) {
    if (state.rested.get(run.run_id) === run.last_seen) state.rested.delete(run.run_id); else state.rested.set(run.run_id, run.last_seen);
    const current = new Map(state.latest.runs.map((r) => [r.run_id, r.last_seen]));
    [...state.rested].filter(([id, seen]) => current.get(id) !== seen).forEach(([id]) => state.rested.delete(id));
    try { localStorage.setItem(REST_KEY, JSON.stringify(Object.fromEntries(state.rested))); } catch (_) { /* rest marks are a convenience */ }
    render();
  }
  function statusOf(run) {
    if (run.state === "completed") return { key: "done", glyph: "✓", text: "done" };
    if (run.state === "failed") return { key: "failed", glyph: "✗", text: "failed" };
    if (run.state === "cancelled") return { key: "done", glyph: "–", text: "cancelled" };
    if (run.archived) return { key: "closed", glyph: "■", text: "archived" };
    // No live process owns it (checked, not guessed): the session has ended.
    if (run.live === false) return { key: "closed", glyph: "■", text: "closed" };
    // An unanswered approval stops a transcript from changing; it still needs you.
    if (run.state === "waiting_user") return { key: "needs", glyph: "◐", text: "needs you" };
    // A top-level session that recently finished its turn is waiting for your next direction.
    if (run.state === "idle" && !run.parent_run_id && serverNow() - (time(run.last_seen) || 0) <= (run.live === true ? OPEN_TURN_WINDOW : QUIET_WINDOW)) {
      if (!state.past && state.rested.get(run.run_id) === run.last_seen) return { key: "rest", turn: true, glyph: "☾", text: "at rest" };
      return { key: "needs", turn: true, glyph: "◐", text: "your turn" };
    }
    if (run.state === "idle" && run.live === true && !run.parent_run_id) return { key: "idle", glyph: "◌", text: "open · idle" };
    if (!["fresh", "observed"].includes(healthOf(run))) return { key: "quiet", glyph: "○", text: "quiet" };
    // Waiting on a tool or a child is ordinary work, not something for you.
    if (String(run.state).startsWith("waiting")) return { key: "running", glyph: "●", text: "running" };
    if (run.state === "idle") return { key: "idle", glyph: "◌", text: "idle" };
    if (run.state === "unknown") return { key: "unknown", glyph: "?", text: "unknown" };
    return { key: "running", glyph: "●", text: "running" };
  }
  function doingOf(run, status) {
    if (terminal.has(run.state)) return { text: run.summary || run.activity || run.task || "finished", cls: "" };
    const summary = run.ai_summary;
    if (summary?.text) return { ai: true, text: summary.text, cls: status.key === "needs" ? "needs" : summary.stale ? "stale-summary" : "" };
    // The last reply says most about a finished turn; the request is the fallback.
    const replied = run.activity && !run.activity.startsWith("Turn ended");
    const raw = status.turn && !replied ? run.task || run.activity : run.activity || run.task;
    if (!raw) return { text: run.inferred ? "text hidden · enable include_text" : "no activity reported", cls: "dim" };
    if (status.key === "quiet" || status.key === "closed") return { text: raw, cls: "dim" };
    return { text: raw, cls: status.key === "needs" ? "needs" : "" };
  }
  // A name the user set (/rename, session name) always beats the AI title.
  const aiTitleOf = (run) => (run.title_source !== "user" && run.ai_summary?.title) || "";
  const autoNameOf = (run) => aiTitleOf(run) || run.name || "Unnamed run";
  // A name given here in Spiritboard beats every automatic one.
  const nameOf = (run) => run.local_name || autoNameOf(run);
  function titleNote(run) {
    if (run.local_name) return `Your name · automatic: ${autoNameOf(run)}`;
    if (!aiTitleOf(run)) return "";
    const was = run.ai_summary?.previous_title;
    return `AI title${run.name ? ` · native: ${run.name}` : ""}${was ? ` · Renamed from “${was}”` : ""}`;
  }
  function inView(run) {
    const status = statusOf(run);
    if (state.view === "waiting") return status.key === "needs";
    if (state.view === "active") {
      if (terminal.has(run.state) || run.archived) return false;
      // An open terminal is never history, however long it idles.
      if (status.key === "idle" && run.live === true) return true;
      // Long-idle open sessions, quiet and closed ones stay under "all" after 30 minutes.
      return !["quiet", "closed", "idle"].includes(status.key) || serverNow() - (time(run.last_seen) || 0) <= QUIET_WINDOW;
    }
    const window = Number($("window-filter").value);
    return !window || serverNow() - (time(run.last_seen) || 0) <= window;
  }
  function matches(run) {
    const query = $("search").value.toLowerCase().trim();
    const harness = $("harness-filter").value;
    return inView(run) && (!harness || harnessOf(run).key === harness) &&
      (!query || [run.local_name, run.name, run.ai_summary?.title, run.ai_summary?.previous_title, run.task, run.activity, run.ai_summary?.text, run.project, run.app, harnessOf(run).label].some((v) => String(v || "").toLowerCase().includes(query)));
  }

  function activity() { return state.snapshot?.activity || { bucket_seconds: 10, buckets: 60, runs: {} }; }
  function rateOf(runId) {
    const data = activity(); const series = data.runs?.[runId];
    if (!Array.isArray(series)) return 0;
    const perMinute = Math.max(1, Math.round(60 / data.bucket_seconds));
    return series.slice(-perMinute).reduce((a, b) => a + b, 0);
  }
  function harnesses(runs, paint = true) {
    const map = new Map();
    (state.snapshot.profiles || []).forEach((p) => map.set(`profile:${p.id}`, { key: `profile:${p.id}`, label: profileLabel(p), runs: [] }));
    runs.forEach((run) => {
      const h = harnessOf(run);
      if (!map.has(h.key)) map.set(h.key, { ...h, runs: [] });
      map.get(h.key).runs.push(run);
    });
    const list = [...map.values()].sort((a, b) => a.key.localeCompare(b.key));
    if (paint) state.colors = new Map(list.map((h, i) => [h.key, PALETTE[i % PALETTE.length]]));
    list.forEach((h) => { h.rate = h.runs.reduce((sum, run) => sum + rateOf(run.run_id), 0); h.latest = h.runs.reduce((m, run) => Math.max(m, time(run.last_seen) || 0), 0); });
    return list;
  }

  function applySnapshot(snapshot, reset = false) {
    if (!snapshot || !Array.isArray(snapshot.runs)) throw new Error("Invalid dashboard snapshot.");
    state.latest = snapshot;
    state.lastSnapshotAt = Date.now();
    if (state.zoom !== 600 && Date.now() - state.chartAt > 10000) loadChart();
    // The past board stays still; only the chart follows live updates.
    if (state.past) { render(); return; }
    const previousCursor = state.snapshot?.cursor;
    const replacedJournal = Boolean(state.snapshot?.stream_id && snapshot.stream_id && state.snapshot.stream_id !== snapshot.stream_id);
    const historyReset = reset || replacedJournal || (typeof previousCursor === "number" && snapshot.cursor < previousCursor);
    if (historyReset) {
      state.events = []; state.nextCursor = 0; state.more = false; state.eventBusy = false;
      state.request += 1; // Ignore any timeline request from the previous journal.
    }
    state.snapshot = snapshot;
    const fingerprint = JSON.stringify([snapshot.runs, snapshot.profiles || [], snapshot.ai || {}]);
    const changed = fingerprint !== state.fingerprint;
    state.fingerprint = fingerprint;
    $("demo-banner").hidden = !(snapshot.mode === "demo" || snapshot.runs.some((r) => r.demo));
    showError(null);
    render();
    if (state.selected) {
      // Do not reset an older paginated history while the user is reading it.
      if ((previousCursor !== snapshot.cursor || historyReset) && !state.eventBusy && !state.more) loadEvents(false);
    }
  }
  function render() {
    if (!state.snapshot) return;
    // The chart always shows the live journal; colours follow its harnesses.
    const current = harnesses(state.latest.runs);
    const list = state.past ? harnesses(state.snapshot.runs, false) : current;
    renderHarnessFilter(list); renderCounts(); renderAi(); renderWarnings(); renderHarnesses(list, renderFlux(current)); renderPast();
    // Rebuild the table only when something visible changed, so clicks and focus survive live ticks.
    if (tableKey() !== state.tableKey) renderTable(); else updateSeen();
  }
  function activeStatuses(runs) {
    // Same scope as the active view: long-quiet history is not counted.
    return runs.filter((run) => !terminal.has(run.state) && !run.archived && (statusOf(run).key !== "quiet" || serverNow() - (time(run.last_seen) || 0) <= QUIET_WINDOW)).map(statusOf);
  }
  const BUSY = new Set(["running", "waiting", "unknown"]);
  function renderCounts() {
    const statuses = activeStatuses(state.snapshot.runs);
    const needs = statuses.filter((s) => s.key === "needs").length;
    const counts = [["running", "●", statuses.filter((s) => BUSY.has(s.key)).length, "running"],
      ["needs", "◐", needs, needs === 1 ? "needs you" : "need you"],
      ["rest", "☾", statuses.filter((s) => s.key === "rest").length, "at rest"],
      ["idle", "◌", statuses.filter((s) => s.key === "idle").length, "idle"],
      ["quiet", "○", statuses.filter((s) => s.key === "quiet").length, "quiet"]];
    // Zero counts say nothing; only what is there takes space.
    $("counts").replaceChildren(...counts.filter(([, , value]) => value).map(([key, glyph, value, text]) => {
      const item = node("span", null, `count c-${key}`); item.append(node("span", glyph, `g-${key}`), " ", node("strong", value), node("span", ` ${text}`, "count-word")); return item;
    }));
    if (state.past) $("counts").append(node("span", `at ${new Date(state.past.at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`, "counts-at"));
  }
  function renderAi() {
    const ai = state.snapshot.ai || {};
    const host = $("ai-status"); host.replaceChildren();
    // Usage stays one hover away on the button; the header only says whether summaries work.
    const button = $("summary-settings-open");
    if (!ai.enabled) { host.append(node("span", "Summaries off", "muted")); button.title = "Open AI summaries to connect a provider or resume summaries"; return; }
    const known = ai.known_cost_usd ?? ai.actual_cost_usd ?? 0;
    const spent = ai.unknown_cost_calls && known === 0 ? "cost unavailable" : `$${known.toFixed(4)}${ai.unknown_cost_calls ? "+ known spend" : " spent"}`;
    const usage = [];
    if (ai.api_calls !== 0) usage.push(`${spent} · ${ai.api_calls ?? ai.calls ?? 0} API calls`);
    if (ai.subscription_calls) usage.push(`${ai.subscription_calls} ChatGPT plan calls`);
    if (!usage.length) usage.push("no provider calls yet");
    const provider = ({ codex: "ChatGPT", openrouter: "OpenRouter", anthropic: "Anthropic", openai: "OpenAI API" })[ai.provider] || "";
    host.append(node("span", ai.status === "error" ? "Summaries need attention" : `Summaries${provider ? ` · ${provider}` : ""}`, ai.status === "error" ? "bad" : ""));
    button.title = [ai.error, ...usage, "opt-in: bounded redacted context goes only to connected providers"].filter(Boolean).join(" · ");
  }
  function renderWarnings() {
    const raw = state.snapshot.source_errors || [];
    const warnings = (Array.isArray(raw) ? raw : Object.entries(raw).map(([profile, message]) => ({ profile, message })))
      .map((w) => typeof w === "string" ? w : `${w.label || w.profile || "Source"}: ${w.message || "Source unavailable"}`);
    (state.snapshot.source_stats || []).filter((s) => s.truncated || s.discovery_limited)
      .forEach((s) => warnings.push(`${s.label}: scanned ${s.selected_files} of ${s.files_seen} files; older sessions may be missing.`));
    (state.snapshot.source_stats || []).filter((s) => s.unsupported_schema)
      .forEach((s) => warnings.push(`${s.label}: storage format not recognized; some sessions were skipped.`));
    if (state.snapshot.ai?.error) warnings.push(`Summaries: ${state.snapshot.ai.error}`);
    const host = $("source-errors");
    host.hidden = !warnings.length;
    host.replaceChildren(...warnings.map((text) => node("p", text)));
  }
  function renderHarnessFilter(list) {
    const select = $("harness-filter"); const current = select.value;
    const wanted = ["", ...list.map((h) => h.key)].join("\u0000");
    if ([...select.options].map((o) => o.value).join("\u0000") !== wanted) {
      select.replaceChildren(new Option("all harnesses", ""), ...list.map((h) => new Option(h.label, h.key)));
      if ([...select.options].some((o) => o.value === current)) select.value = current;
    }
  }
  function renderHarnesses(list, drawn) {
    const focused = document.activeElement?.dataset?.focusKey;
    const host = $("harnesses");
    // Harnesses with something running or drawn in the chart (or the one filtered on); idle ones stay in the filter.
    const running = (h) => h.rate > 0 || drawn.has(h.key) || activeStatuses(h.runs).some((s) => BUSY.has(s.key) || s.key === "needs" || s.key === "idle");
    const shown = list.filter((h) => running(h) || $("harness-filter").value === h.key);
    host.replaceChildren(...shown.map((h) => {
      const button = node("button", null, `harness${$("harness-filter").value === h.key ? " is-selected" : ""}`);
      button.type = "button"; button.dataset.focusKey = `harness:${h.key}`;
      button.setAttribute("aria-pressed", String($("harness-filter").value === h.key));
      const swatch = node("span", null, "swatch"); swatch.style.background = state.colors.get(h.key);
      const statuses = activeStatuses(h.runs);
      const needs = statuses.filter((s) => s.key === "needs").length;
      const parts = [[(s) => BUSY.has(s.key), "running"], [(s) => s.key === "needs", needs === 1 ? "needs you" : "need you"]]
        .map(([test, text]) => [statuses.filter(test).length, text]).filter(([n]) => n).map(([n, text]) => `${n} ${text}`);
      // With nothing running or waiting, the idle age shows once events stop, in whole minutes so it never ticks.
      const idleFor = h.latest && serverNow() - h.latest >= 60000 ? `idle ${age(h.latest)}` : "idle";
      const sub = parts.join(" · ") || (!h.runs.length ? "no observations yet" : h.rate ? "" : idleFor);
      const name = node("span", null, "who"); name.title = sub ? `${h.label} · ${sub}` : h.label; name.append(node("span", h.label, "name"), node("span", sub, "sub"));
      const rate = node("span", h.rate || "", "rate");
      if (h.rate) rate.title = `${h.rate} events in the last minute`;
      button.append(swatch, name, rate);
      button.addEventListener("click", () => { $("harness-filter").value = $("harness-filter").value === h.key ? "" : h.key; render(); });
      return button;
    }));
    if (!list.length) host.append(node("p", "No harnesses yet. Start the demo or configure a source profile.", "empty"));
    else if (!shown.length) host.append(node("p", state.past ? "Nothing running at this moment." : "Nothing running right now.", "empty"));
    const idle = list.length - shown.length;
    if (idle) host.append(node("p", `+ ${idle} idle, in the harness filter`, "harness-idle"));
    if (focused) [...host.children].find((el) => el.dataset.focusKey === focused)?.focus({ preventScroll: true });
  }
  function chartData() {
    if (state.zoom === 600) return state.latest?.activity || activity();
    return state.chart || { bucket_seconds: state.zoom / 60, buckets: 60, runs: {} };
  }
  function renderZoom() {
    const [heading, left, middle] = ZOOMS[state.zoom];
    [...$("zoom").children].forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.zoom) === state.zoom)));
    $("flux-window").textContent = heading.replace("minutes", "min");
    $("axis").replaceChildren(node("span", left), node("span", middle), node("span", "now", "now"));
  }
  function setZoom(zoom) {
    state.zoom = zoom; state.chart = null; renderZoom();
    if (zoom !== 600) loadChart();
    if (state.snapshot) render();
  }
  async function loadChart() {
    const zoom = state.zoom; state.chartAt = Date.now();
    try {
      const response = await fetch(`/api/activity?window=${zoom}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Chart unavailable (HTTP ${response.status}).`);
      const data = await response.json();
      if (zoom === state.zoom) { state.chart = data; if (state.snapshot) render(); }
    } catch (error) { showError(error.message || "Unable to load the chart."); }
  }
  // A bar stands for the board at the end of its bucket; the newest bar is now.
  function travel(i, data) {
    if (i >= data.buckets - 1) { backToLive(); return; }
    const end = time(data.end) || liveNow();
    loadPast(end - (data.buckets - 1 - i) * data.bucket_seconds * 1000);
  }
  async function loadPast(at) {
    const ticket = ++state.pastRequest;
    try {
      const response = await fetch(`/api/snapshot?at=${(at / 1000).toFixed(3)}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`That moment is not in the kept history (HTTP ${response.status}).`);
      const snapshot = await response.json();
      if (ticket !== state.pastRequest) return;
      if (!snapshot || !Array.isArray(snapshot.runs)) throw new Error("Invalid dashboard snapshot.");
      state.past = { at: time(snapshot.at) ?? at };
      state.snapshot = snapshot; state.fingerprint = `past:${snapshot.at}`;
      showError(null); render();
    } catch (error) { if (ticket === state.pastRequest) showError(error.message || "Unable to load that moment."); }
  }
  function backToLive() {
    state.pastRequest += 1;
    if (!state.past) return;
    state.past = null; state.snapshot = state.latest; state.fingerprint = null;
    render();
  }
  const pastText = node("span");
  const backButton = node("button", "⟲ back to live"); backButton.type = "button";
  backButton.addEventListener("click", backToLive);
  $("past-bar").replaceChildren(pastText, backButton);
  function renderPast() {
    showConnection();
    $("past-bar").hidden = !state.past;
    if (!state.past) return;
    const when = new Date(state.past.at).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    pastText.textContent = `Viewing ${when} · ${age(state.past.at, liveNow())} ago · `;
  }
  function renderFlux(list) {
    const host = $("flux"); host.replaceChildren();
    const data = chartData(); const count = data.buckets || 60; const bucket = data.bucket_seconds || 10; const scale = 60 / bucket;
    const windowText = ZOOMS[state.zoom][0];
    $("flux-note").textContent = bucket <= 60 ? "events/min" : `events per ${span(bucket)}`;
    const byHarness = list.map((h) => {
      const series = new Array(count).fill(0);
      h.runs.forEach((run) => (data.runs?.[run.run_id] || []).forEach((v, i) => { series[i] += v; }));
      return { key: h.key, label: h.label, series };
    }).filter((h) => h.series.some(Boolean));
    // The harnesses drawn here, so the list can show each of them.
    const drawn = new Set(byHarness.map((h) => h.key));
    const totals = new Array(count).fill(0).map((_, i) => byHarness.reduce((sum, h) => sum + h.series[i], 0));
    const peak = Math.max(...totals);
    if (!peak) { host.append(node("span", `No events in the ${windowText}`, "empty")); host.setAttribute("aria-label", `No events in the ${windowText}`); return drawn; }
    // Percent heights let the same bars fill the full chart or the compact sparkline.
    const unit = 100 / Math.max(peak, 3);
    totals.forEach((_, i) => {
      const col = node("div", null, "col");
      // Top-to-bottom matches the harness list order reversed, so the first harness sits on the baseline.
      [...byHarness].reverse().forEach((h) => {
        if (!h.series[i]) return;
        const seg = node("span"); seg.style.height = `${h.series[i] * unit}%`; seg.style.background = state.colors.get(h.key); col.append(seg);
      });
      col.addEventListener("click", () => travel(i, data));
      host.append(col);
    });
    if (state.past) {
      const start = (time(data.end) || liveNow()) - count * bucket * 1000;
      const x = (state.past.at - start) / (count * bucket * 1000);
      if (x >= 0 && x <= 1) { const marker = node("span", null, "marker"); marker.style.left = `${x * 100}%`; host.append(marker); }
    }
    host.append(node("span", bucket <= 60 ? `${peak * scale}/min` : `${peak}/${span(bucket)}`, "ymax"));
    const recent = Math.max(1, Math.round(scale));
    const busiest = byHarness.map((h) => [h.label, h.series.slice(-recent).reduce((a, b) => a + b, 0)]).sort((a, b) => b[1] - a[1])[0];
    host.setAttribute("aria-label", `Events over the ${windowText}, stacked by harness. Busiest in the latest ${bucket <= 60 ? "minute" : span(bucket)}: ${busiest[0]} with ${busiest[1]}.`);
    return drawn;
  }

  function workRank(run) {
    const key = statusOf(run).key;
    return { needs: 0, running: 1, waiting: 2, rest: 3, idle: 3, unknown: 4, quiet: 5, failed: 6, done: 7 }[key] ?? 8;
  }
  function sortValue(run) {
    if (state.sort === "agent") return nameOf(run).toLowerCase();
    if (state.sort === "doing") return doingOf(run, statusOf(run)).text.toLowerCase();
    if (state.sort === "rate") return rateOf(run.run_id);
    return time(run.last_seen) || 0;
  }
  function renderSort() {
    document.querySelectorAll("[data-sort]").forEach((button) => {
      const active = button.dataset.sort === state.sort;
      button.setAttribute("aria-pressed", String(active));
      button.dataset.arrow = active ? state.descending ? "▼" : "▲" : "";
      button.setAttribute("aria-label", `${button.textContent.trim()}${active ? `, sorted ${state.descending ? "descending" : "ascending"}` : ", sort"}`);
    });
  }
  function tableKey() {
    return JSON.stringify([state.fingerprint, activity().end && Math.floor(time(activity().end) / 10000), state.view,
      $("harness-filter").value, $("window-filter").value, $("search").value, state.sort, state.descending, state.selected, state.historyOpen,
      state.events.length, state.eventBusy, state.more, state.target, state.targetNote, state.targetFallback, [...state.rested], Math.floor(serverNow() / 60000)]);
  }
  function seenText(value) {
    const seen = time(value);
    // Anything within the minute reads "now", so live rows stop ticking every second.
    return seen !== null && serverNow() - seen < 60000 ? "now" : age(value);
  }
  function updateSeen() { [...$("runs").querySelectorAll("[data-seen]")].forEach((el) => { el.textContent = seenText(el.dataset.seen); }); }
  function renderTable(force = false) {
    // An open rename keeps the table still so its draft, focus and caret survive live ticks.
    if (state.editing && !force) { state.tableKey = ""; updateSeen(); return; }
    state.tableKey = tableKey();
    const focused = document.activeElement?.dataset?.focusKey;
    const host = $("runs"); host.replaceChildren(); host.setAttribute("aria-busy", "false");
    const runs = state.snapshot.runs;
    const byId = new Map(runs.map((r) => [r.run_id, r]));
    const children = new Map();
    runs.forEach((r) => { if (!children.has(r.parent_run_id)) children.set(r.parent_run_id, []); children.get(r.parent_run_id).push(r); });
    const direct = new Set(runs.filter(matches).map((r) => r.run_id));
    const visible = new Set(direct);
    // Keep ancestors of matching runs as dimmed context so the tree stays readable.
    direct.forEach((id) => { const seen = new Set(); let r = byId.get(id); while (r?.parent_run_id && byId.has(r.parent_run_id) && !seen.has(r.parent_run_id)) { seen.add(r.parent_run_id); visible.add(r.parent_run_id); r = byId.get(r.parent_run_id); } });
    const ranks = new Map();
    const rank = (run, path = new Set()) => {
      if (ranks.has(run.run_id)) return ranks.get(run.run_id);
      const value = { rank: workRank(run), seen: time(run.last_seen) || 0 };
      if (path.has(run.run_id)) return value;
      const next = new Set(path); next.add(run.run_id);
      (children.get(run.run_id) || []).filter((c) => visible.has(c.run_id)).forEach((c) => { const b = rank(c, next); value.rank = Math.min(value.rank, b.rank); value.seen = Math.max(value.seen, b.seen); });
      ranks.set(run.run_id, value); return value;
    };
    const live = (a, b) => rank(a).rank - rank(b).rank || rank(b).seen - rank(a).seen || a.run_id.localeCompare(b.run_id);
    // While a run is open, keep the order from before it opened so rows don't jump; unseen runs go after.
    const frozen = (run) => state.frozen.get(run.run_id) ?? Infinity;
    // Column sorts order within tiers: busy or needing you, then at rest, then history; open idle sessions follow.
    const tier = (run) => { const r = rank(run).rank; return r < 3 ? 0 : r === 3 ? 1 : 2; };
    const sorted = (a, b) => {
      if (state.sort !== "status" && tier(a) !== tier(b)) return tier(a) - tier(b);
      const left = state.sort === "status" ? rank(a).rank : sortValue(a);
      const right = state.sort === "status" ? rank(b).rank : sortValue(b);
      const order = typeof left === "number" ? left - right : left.localeCompare(right);
      return (state.descending ? -order : order) || a.run_id.localeCompare(b.run_id);
    };
    const compare = state.sort ? sorted : state.selected ? (a, b) => frozen(a) - frozen(b) || live(a, b) : live;
    renderHiddenNote(runs, direct);
    state.order = [];
    if (!runs.length) { host.append(node("p", "No agents yet. Start the synthetic demo or configure a local source profile. This dashboard never starts agents.", "empty")); return; }
    if (!direct.size) { host.append(node("p", state.view === "active" ? `Nothing active ${state.past ? "at this moment" : "right now"}. Press e to show all history.` : "No agents match these filters.", "empty")); return; }
    const maxRate = Math.max(1, ...[...visible].map(rateOf));
    const visited = new Set();
    const revealed = new Set();
    const openPath = new Set();
    for (let r = byId.get(byId.get(state.selected)?.parent_run_id); r && !openPath.has(r.run_id); r = byId.get(r.parent_run_id)) openPath.add(r.run_id);
    let cap = MAX_ROWS, cut = false;
    const renderRun = (run, depth, orphan) => {
      if (visited.has(run.run_id) || !visible.has(run.run_id)) return;
      if (state.order.length >= cap) { cut = true; return; }
      visited.add(run.run_id);
      state.order.push(run.run_id);
      const all = children.get(run.run_id) || [];
      const inView = all.filter((c) => visible.has(c.run_id));
      // An idle or quiet session folds its settled subagents unless it is only context for them.
      const settled = ["idle", "quiet", "rest"].includes(statusOf(run).key) && inView.every((c) => rank(c).rank >= 3)
        && direct.has(run.run_id);
      // Your choice wins: true folds them all, false shows them all, even ones outside this view.
      const choice = state.folds.get(run.run_id);
      const folded = (choice ?? settled) && !openPath.has(run.run_id);
      const kids = (folded ? [] : choice === false ? all : inView).sort(compare);
      kids.forEach((c) => { if (!visible.has(c.run_id)) { visible.add(c.run_id); revealed.add(c.run_id); } });
      host.append(row(run, depth, !direct.has(run.run_id) && !revealed.has(run.run_id), orphan, maxRate, all.length, kids.length));
      if (state.selected === run.run_id) host.append(expansion(run, byId));
      kids.forEach((child) => renderRun(child, depth + 1, false));
    };
    const roots = runs.filter((r) => !r.parent_run_id || !byId.has(r.parent_run_id)).sort(compare);
    const renderRoot = (r) => renderRun(r, r.parent_run_id ? 1 : 0, Boolean(r.parent_run_id));
    // Open idle sessions with nothing busy below them gather under one line at the bottom, folded unless one is opened.
    // While a row is open, sessions listed above that line stay there, so the open row never jumps.
    const openIdle = roots.filter((r) => visible.has(r.run_id) && statusOf(r).key === "idle" && r.live === true
      && rank(r).rank >= 3 && !(state.selected && frozen(r) < state.split));
    const groupFolded = (state.folds.get(OPEN_IDLE) ?? true) && !openIdle.some((r) => r.run_id === state.selected || openPath.has(r.run_id));
    roots.filter((r) => !openIdle.includes(r)).forEach(renderRoot);
    const split = state.order.length;
    if (cut) host.append(node("p", `Showing the first ${MAX_ROWS} agents. Narrow the view or search to see more.`, "empty"));
    if (openIdle.length) {
      // The group has its own row budget, so a long history never hides it.
      cap = split + MAX_ROWS;
      host.append(groupRow(openIdle.length, groupFolded));
      if (!groupFolded) openIdle.forEach(renderRoot);
    }
    if (!state.selected) { state.frozen = new Map(state.order.map((id, i) => [id, i])); state.split = split; }
    if (focused) [...host.querySelectorAll("[data-focus-key]")].find((el) => el.dataset.focusKey === focused)?.focus({ preventScroll: true });
  }
  function groupRow(count, folded) {
    const button = node("button", `${folded ? "▸" : "▾"} ${count} open · idle`, "group-row");
    button.type = "button"; button.dataset.focusKey = `group:${OPEN_IDLE}`;
    button.setAttribute("aria-expanded", String(!folded));
    button.title = `${folded ? "Show" : "Hide"} sessions still open but idle`;
    button.addEventListener("click", () => toggleFold(OPEN_IDLE, !folded));
    return button;
  }
  function row(run, depth, context, orphan, maxRate, kids = 0, shown = 0) {
    const status = statusOf(run);
    const open = state.selected === run.run_id;
    const button = node("button", null, `row run-row ${status.key}${depth ? " sub" : ""}${open ? " selected" : ""}${context ? " context" : ""}`);
    // Subagents shift right as a block, so they never read as top-level processes.
    if (depth) button.style.setProperty("--depth", String(depth));
    button.type = "button"; button.dataset.focusKey = `select:${run.run_id}`; button.dataset.runId = run.run_id;
    button.setAttribute("aria-current", String(open));
    button.setAttribute("aria-expanded", String(open));
    const statusCell = node("span", null, `cell status g-${status.key}`);
    statusCell.append(node("span", status.glyph, "glyph"), ` ${status.text}`);
    if (run.inferred) statusCell.title = "Inferred from the transcript";
    const agent = node("span", null, "cell agent");
    const harness = harnessOf(run);
    // The harness lives in tooltips, the accessible name and the opened panel; rows carry status colour only.
    const where = run.app ? `${harness.label} · ${run.app}` : harness.label;
    agent.title = where;
    // Renaming happens in the opened session; a click on the row only opens it.
    const name = node("span", nameOf(run), "agent-name");
    name.title = titleNote(run) ? `${where} · ${titleNote(run)}` : where;
    agent.append(name, node("span", where, "sr-only"));
    if (run.local_name) {
      const mark = node("span", "✎", "renamed-mark"); mark.title = "Renamed by you in Spiritboard";
      mark.setAttribute("role", "img"); mark.setAttribute("aria-label", "renamed by you");
      agent.append(mark);
    }
    if (orphan) agent.append(node("span", "parent not yet available", "note"));
    if (context) agent.append(node("span", "context", "note"));
    if (kids) {
      const count = (n, more = "") => `${n} ${more}${n === 1 ? "subagent" : "subagents"}`;
      const text = shown === kids ? `▾ ${count(kids)}` : `▸ ${shown ? count(kids - shown, "more ") : count(kids)}`;
      const fold = node("span", text, "fold");
      fold.title = `${shown === kids ? "Hide" : "Show all"} subagents · s`;
      fold.addEventListener("click", (event) => { event?.stopPropagation?.(); toggleFold(run.run_id, shown === kids); });
      agent.append(fold);
      button.dataset.folds = String(shown === kids);
    }
    // Where the session runs, when it is an app rather than a terminal (Claude desktop).
    if (run.app) agent.append(node("span", run.app, "note app-note"));
    const doing = doingOf(run, status);
    const doingCell = node("span", null, `cell doing ${doing.cls}`);
    doingCell.append(doing.text);
    if (doing.ai) doingCell.title = `${run.ai_summary?.origin === "claude-recap" ? "Claude recap" : "AI summary"}${run.ai_summary?.stale ? " · goal changed, refresh due" : ""}`;
    const rate = rateOf(run.run_id);
    const rateCell = node("span", null, "rate-cell");
    // A zero rate is left blank, so only busy agents carry a number.
    if (rate) {
      const bar = node("span", null, "bar"); bar.style.width = `${Math.max(2, Math.round(rate / maxRate * 48))}px`;
      rateCell.append(node("span", rate, "num"), bar);
    }
    const last = node("span", seenText(run.last_seen), "cell seen");
    last.dataset.seen = run.last_seen || "";
    button.append(statusCell, agent, doingCell, rateCell, last);
    button.addEventListener("click", () => selectRun(state.selected === run.run_id ? null : run.run_id));
    return button;
  }
  function toggleFold(id, fold) {
    // With its panel open, the toggle closes the panel and the list together.
    if (state.selected === id) { selectRun(null); return; }
    state.folds.set(id, fold); renderTable();
  }
  // The focused row, else the opened one, folds or unfolds its subagents.
  function foldSelected() {
    const id = document.activeElement?.dataset?.runId || state.selected;
    const button = id && [...$("runs").querySelectorAll("[data-run-id]")].find((el) => el.dataset.runId === id && el.dataset.folds);
    if (button) toggleFold(id, button.dataset.folds === "true");
  }
  const byFocusKey = (key) => [...$("runs").querySelectorAll("[data-focus-key]")].find((el) => el.dataset.focusKey === key);
  // The opened session's heading is a button that turns into an input while renaming.
  function nameControl(run) {
    if (state.editing === run.run_id) {
      const input = node("input", null, "name-edit");
      input.type = "text"; input.value = state.draft; input.maxLength = 120; input.spellcheck = false; input.autocomplete = "off";
      input.setAttribute("aria-label", "Agent name"); input.dataset.focusKey = "name-edit";
      input.addEventListener("input", () => { state.draft = input.value; });
      input.addEventListener("keydown", (event) => {
        if (event.isComposing) return;
        if (event.key === "Enter") { event.preventDefault(); endEdit(input.value, true); }
        else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); endEdit(null, true); }
      });
      input.addEventListener("blur", () => endEdit(input.value, false));
      return input;
    }
    const note = titleNote(run);
    if (state.past) { const text = node("span", nameOf(run), "agent-name"); if (note) text.title = note; return text; }
    const button = node("button", null, "agent-name rename"); button.type = "button"; button.dataset.focusKey = "name";
    button.append(node("span", nameOf(run), "name-text"));
    button.title = run.local_name ? note : [note, "Click to rename"].filter(Boolean).join(" · ");
    button.setAttribute("aria-label", `Rename ${nameOf(run)}`);
    button.addEventListener("click", (event) => { event?.stopPropagation?.(); startEdit(run.run_id); });
    return button;
  }
  function startEdit(id) {
    const run = state.snapshot?.runs.find((r) => r.run_id === id);
    if (!run || state.past) return;
    state.editing = id; state.draft = nameOf(run);
    renderTable(true);
    const input = byFocusKey("name-edit");
    if (!input) { state.editing = null; renderTable(); return; }
    input.focus(); input.select?.();
  }
  function endEdit(value, refocus) {
    const id = state.editing;
    if (!id) return;
    state.editing = null;
    if (value !== null) storeName(id, value.trim());
    if (!refocus) { settle(); return; }
    renderTable();
    byFocusKey("name")?.focus();
  }
  // Rebuilding between press and release would swallow the click that ended a rename by blur.
  let pressed = false, unsettled = false;
  function settle() { if (pressed) unsettled = true; else renderDetail(); }
  document.addEventListener("pointerdown", () => { pressed = true; }, true);
  ["pointerup", "pointercancel"].forEach((name) => document.addEventListener(name, () => {
    pressed = false;
    if (unsettled) { unsettled = false; setTimeout(renderDetail, 0); }
  }, true));
  function setLocalName(id, value) {
    [state.latest, state.snapshot].forEach((snapshot) => snapshot?.runs.forEach((run) => {
      if (run.run_id !== id) return;
      if (value) run.local_name = value; else delete run.local_name;
    }));
  }
  const RENAME_ERRORS = { 400: "that name is not allowed", 403: "the request was refused", 404: "this agent is no longer listed" };
  // Saving the empty or the automatic name clears the local one; the next snapshot confirms it.
  async function storeName(id, text) {
    const run = state.snapshot?.runs.find((r) => r.run_id === id);
    if (!run || state.past) return;
    const wanted = !text || text === autoNameOf(run) ? null : text;
    const previous = run.local_name || null;
    if (wanted === previous) return;
    setLocalName(id, wanted);
    let saved = wanted, problem = "";
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(id)}/name`, { method: "POST", cache: "no-store",
        headers: { "X-Spiritboard": "rename", "Content-Type": "application/json" }, body: JSON.stringify({ name: wanted ?? "" }) });
      if (response.ok) saved = (await response.json())?.local_name ?? null;
      else problem = RENAME_ERRORS[response.status] || `HTTP ${response.status}`;
    } catch (_) { problem = "the local collector is unreachable"; }
    if (!problem) refresh();
    if (problem) showError(`Could not rename “${previous || autoNameOf(run)}”: ${problem}.`);
    // A newer snapshot or rename already replaced the optimistic name.
    if ((state.snapshot?.runs.find((r) => r.run_id === id)?.local_name || null) !== wanted) return;
    const final = problem ? previous : saved;
    if (final === wanted) return;
    setLocalName(id, final);
    settle();
  }
  function renderHiddenNote(runs, direct) {
    const note = $("hidden-note");
    // In the active view the note opens everything it hides; elsewhere it only counts.
    // aria-disabled rather than disabled, so the focus stays on it after the click.
    note.setAttribute("aria-disabled", String(state.view !== "active"));
    note.title = state.view === "active" ? "Show all (e)" : "";
    const hidden = state.view === "active" ? runs.filter((r) => !inView(r)).length : 0;
    note.textContent = state.view === "all" ? `${direct.size} of ${runs.length} agents in window`
      : state.view === "waiting" ? `${direct.size} waiting` : hidden ? `${hidden} finished or older · show all` : "";
    note.hidden = !note.textContent;
  }
  function renderViews() {
    [...$("views").children].forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === state.view)));
    $("window-label").hidden = state.view !== "all";
  }

  // Consecutive identical events collapse into one line, so tool bursts stay readable.
  function compressEvents(events) {
    const groups = [];
    events.forEach((event) => {
      const payload = event.payload || {};
      // Only display safe selected labels, never raw arguments, results, paths or headers.
      const named = payload.text || payload.summary || payload.detail || payload.reason || payload.task || payload.label || payload.name || "";
      const detail = named || payload.state || payload.outcome || "";
      const key = `${event.type}\u0000${detail}`;
      const last = groups[groups.length - 1];
      if (last && last.key === key) { last.count += 1; last.stamp = event.received_at || event.occurred_at; }
      // A bare state or outcome, or no detail at all, needs the event type to make sense.
      else groups.push({ key, type: event.type, detail, plain: !named, count: 1, stamp: event.received_at || event.occurred_at, late: event.applied === false });
    });
    return groups;
  }
  function expansion(run, byId) {
    const host = node("div", null, "expansion"); host.id = "detail";
    host.setAttribute("role", "region"); host.setAttribute("aria-label", `Details for ${nameOf(run)}`);
    const status = statusOf(run);
    const parent = byId.get(run.parent_run_id);
    const harness = harnessOf(run);
    const main = node("div", null, "detail-main");
    const head = node("div", null, "detail-head");
    const title = node("div", null, "detail-title");
    const heading = node("h3"); heading.append(nameControl(run));
    title.append(heading);
    if (run.local_name) {
      const renamed = node("span", `Renamed by you in Spiritboard · automatic title: ${autoNameOf(run)}`, "was renamed-note");
      if (!state.past) {
        const reset = node("button", "Use automatic title", "link"); reset.type = "button"; reset.dataset.focusKey = "auto-name";
        reset.addEventListener("click", () => { storeName(run.run_id, ""); settle(); byFocusKey("name")?.focus(); });
        renamed.append(" · ", reset);
      }
      title.append(renamed);
    } else if (aiTitleOf(run) && run.ai_summary?.previous_title) title.append(node("span", `was: ${run.ai_summary.previous_title}`, "was"));
    const actions = node("div", null, "detail-actions");
    actions.append(...targetControls(run));
    if (status.turn && !state.past) {
      const rest = node("button", status.key === "rest" ? "◐ back to your turn" : "☾ let it rest", "link rest-link"); rest.type = "button"; rest.dataset.focusKey = "rest";
      rest.addEventListener("click", () => toggleRest(run));
      actions.append(rest);
    }
    const links = node("div", null, "detail-links");
    const history = node("button", state.historyOpen ? "Hide history" : "History", "link"); history.type = "button"; history.dataset.focusKey = "history";
    history.addEventListener("click", () => { state.historyOpen = !state.historyOpen; if (state.historyOpen && !state.events.length) loadEvents(true); else renderTable(); });
    const close = node("button", "Close", "link"); close.type = "button"; close.title = "Esc"; close.addEventListener("click", () => selectRun(null));
    links.append(history, close);
    head.append(title, actions, links);
    const meta = node("p", null, "detail-meta"); meta.setAttribute("aria-label", "Session");
    const fact = (key, ...content) => { const item = node("span"); if (key) item.append(node("span", key, "k")); item.append(...content); meta.append(item); return item; };
    const swatch = node("span", null, "swatch"); swatch.style.background = state.colors.get(harness.key);
    fact("", swatch, harness.label);
    if (run.app) fact("in", run.app);
    if (run.project) fact("project", run.project);
    if (run.started_at && (terminal.has(run.state) || status.key === "closed")) fact("ran for", age(run.started_at, time(run.ended_at) || serverNow()));
    else if (run.started_at) fact("started", ago(run.started_at));
    const seen = node("span", seenText(run.last_seen)); seen.dataset.seen = run.last_seen || "";
    fact("last seen", seen);
    main.append(head, meta);
    // One status line under the title: what state it is in and where to act.
    const line = (cls, glyph, text, where) => { const p = node("p", null, `state-line ${cls}`); p.append(node("span", glyph, "glyph"), ` ${text}`); if (where) p.append(node("span", ` · ${where}`, "where")); main.append(p); };
    if (status.key === "rest") line("g-rest", "☾", "At rest · you checked this turn", "back when the agent acts again");
    else if (status.turn) line("g-needs", "◐", `Your turn · finished ${ago(run.last_seen)}`);
    else if (run.archived) line("g-closed", "■", `Archived in ${run.app || "its app"} · last active ${ago(run.last_seen)}`);
    else if (status.key === "closed") line("g-closed", "■", `Closed ${ago(run.last_seen)}`, "no running process owns this session");
    else if (status.key === "needs") line("g-needs", "◐", `Needs you · asked ${ago(run.last_seen)}`, parent ? `answer in “${nameOf(parent)}”` : "");
    else if (status.key !== "running") line(`g-${status.key}`, status.glyph, status.text[0].toUpperCase() + status.text.slice(1));
    // A subagent's goal is the task its parent gave it, unless a summary exists.
    const goal = run.ai_summary?.text || (run.parent_run_id ? run.task : "");
    if (goal) {
      const summary = node("div", null, "summary");
      summary.append(node("span", run.ai_summary?.text ? "summary" : "task", "label"), node("p", goal, "goal"));
      main.append(summary);
    }
    const fields = node("dl", null, "fields");
    const field = (title, content, className) => {
      if (!content) return;
      const item = node("div", null, className);
      const dd = node("dd"); dd.append(content);
      item.append(node("dt", title, "label"), dd);
      fields.append(item);
    };
    field("latest", terminal.has(run.state) ? run.summary || label(run.state) : run.activity || "no activity reported");
    if (!run.parent_run_id) field("you asked", run.task || (run.inferred ? "text hidden · set include_text for this profile" : ""), run.task ? "quote" : "");
    if (run.parent_run_id) {
      if (parent) {
        const link = node("button", nameOf(parent), "link"); link.type = "button"; link.dataset.focusKey = `parent:${parent.run_id}`;
        link.addEventListener("click", () => selectRun(parent.run_id));
        field("parent", link);
      } else field("parent", "not yet available");
    }
    main.append(fields);
    host.append(main);
    if (state.historyOpen) host.append(historyList(run));
    return host;
  }
  // The server only activates the owning window; a stopped session offers a command to copy.
  function targetControls(run) {
    const target = state.target;
    if (!target || target.runId !== run.run_id) return [];
    const items = [];
    if (target.kind === "focus") {
      const open = node("button", `↗ open in ${target.label}`, "link app-link"); open.type = "button"; open.dataset.focusKey = "open-app";
      open.addEventListener("click", () => openApp(run.run_id));
      items.push(open);
    } else if (target.kind === "resume" && target.command) {
      // An app session resumes with the CLI, so say where it will continue.
      const copy = node("button", run.app ? "⧉ copy terminal resume command" : "⧉ copy resume command", "link app-link"); copy.type = "button"; copy.dataset.focusKey = "copy-resume";
      copy.title = run.app ? `Resumes in a terminal, not in ${run.app}: ${target.command}` : target.command;
      copy.addEventListener("click", () => copyCommand(run.run_id));
      items.push(copy);
      if (state.targetFallback) {
        const field = node("input", null, "resume-command"); field.readOnly = true; field.value = target.command; field.dataset.focusKey = "resume-command";
        items.push(field);
      }
    } else return [];
    if (state.targetNote) items.push(node("span", state.targetNote, "muted"));
    // A past board never opens or resumes what is running now.
    if (state.past) items.forEach((item) => { if (item.tagName === "button" || item.tagName === "BUTTON") { item.disabled = true; item.title = "Back to live to open or resume"; } });
    return items;
  }
  async function fetchTarget(id) {
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(id)}/focus`, { cache: "no-store" });
      if (response.ok) { const data = await response.json(); if (data?.available) return { ...data, runId: id }; }
    } catch (_) { return null; }
    return null;
  }
  async function loadTarget(id) {
    const target = await fetchTarget(id);
    if (id !== state.selected) return;
    state.target = target; renderDetail();
  }
  async function openApp(id) {
    if (state.past) return;
    let ok = false;
    try { ok = (await fetch(`/api/runs/${encodeURIComponent(id)}/focus`, { method: "POST", cache: "no-store", headers: { "X-Spiritboard": "focus" } })).ok; } catch (_) { ok = false; }
    if (id !== state.selected) return;
    state.targetNote = ok ? "opened" : "could not open"; renderDetail();
  }
  async function copyCommand(id) {
    if (state.past) return;
    // Re-check first: a session that came back to life must not be resumed (forked).
    const target = await fetchTarget(id);
    if (id !== state.selected) return;
    if (target?.kind !== "resume") { state.target = target; state.targetNote = target ? "session is live" : ""; state.targetFallback = false; renderDetail(); return; }
    state.target = target;
    const command = target.command;
    try {
      await globalThis.navigator.clipboard.writeText(command);
      state.targetNote = "copied"; state.targetFallback = false; renderDetail();
    } catch (_) {
      state.targetNote = "select and copy"; state.targetFallback = true; renderDetail();
      const field = [...$("runs").querySelectorAll("[data-focus-key]")].find((el) => el.dataset.focusKey === "resume-command");
      field?.focus(); field?.select?.();
    }
  }
  function historyList(run) {
    const box = node("div", null, "history");
    const operations = run.operations || [];
    // Recent tools read as one line; running ones in the running colour, errors in red.
    if (operations.length) {
      const ops = node("p", null, "ops");
      ops.append(node("span", "tools", "t"));
      operations.slice(-10).forEach((op) => {
        const outcome = op.pending ? "running" : op.outcome || "unknown";
        ops.append(node("span", `${op.label || op.operation_id || "operation"}${op.pending || outcome === "error" ? ` ${outcome}` : ""}`, op.pending ? "op-running" : outcome === "error" ? "bad" : ""));
      });
      box.append(ops);
    }
    const timeline = node("ol", null, "timeline"); timeline.id = "timeline";
    compressEvents(state.events).reverse().forEach((group) => {
      const item = node("li"); const stamp = time(group.stamp);
      const type = label(group.type || "event");
      item.title = `${type} · ${stamp ? new Date(stamp).toLocaleTimeString() : "unknown time"}`;
      const text = group.plain ? [type.replaceAll(".", " "), group.detail].filter(Boolean).join(" · ") : group.detail;
      item.append(node("span", stamp ? ago(stamp) : "", "t"),
        node("span", `${text}${group.count > 1 ? ` ×${group.count}` : ""}${group.late ? " · late, not applied" : ""}`));
      timeline.append(item);
    });
    box.append(timeline);
    if (!state.events.length) box.append(node("p", state.eventBusy ? "Loading history…" : "No events in the available history.", "muted"));
    if (state.more) { const more = node("button", state.eventBusy ? "Loading…" : "Load older events", "link"); more.id = "load-more"; more.type = "button"; more.dataset.focusKey = "load-more"; more.disabled = state.eventBusy; more.addEventListener("click", () => loadEvents(false)); box.append(more); }
    return box;
  }
  function renderDetail() { if (state.snapshot) renderTable(); }
  function restSelected() {
    const run = state.selected && state.snapshot.runs.find((r) => r.run_id === state.selected);
    if (run && statusOf(run).turn && !state.past) toggleRest(run);
  }
  // Opening a session lists all its subagents; closing it folds back to the default (only busy or
  // needs-you subagents), unless you moved into one of them.
  function within(id, ancestor) {
    const byId = new Map(state.snapshot.runs.map((r) => [r.run_id, r]));
    for (let r = byId.get(id), seen = 0; r && seen < 64; r = byId.get(r.parent_run_id), seen++) if (r.run_id === ancestor) return true;
    return false;
  }
  async function selectRun(id) {
    const previous = state.selected;
    if (previous && previous !== id && !(id && within(id, previous))) state.folds.delete(previous);
    if (id && id !== previous) state.folds.set(id, false);
    state.selected = id; state.events = []; state.nextCursor = 0; state.more = false; state.historyOpen = false;
    state.target = null; state.targetNote = ""; state.targetFallback = false;
    renderDetail();
    if (id) { loadTarget(id); await loadEvents(true); }
  }
  async function loadEvents(reset) {
    if (!state.selected) return;
    const request = ++state.request;
    const id = state.selected;
    state.eventBusy = true;
    if (reset) { state.events = []; state.nextCursor = 0; }
    renderDetail();
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(id)}/events?after=${state.nextCursor}&limit=100`, { cache: "no-store" });
      if (!response.ok) throw new Error(`History unavailable (HTTP ${response.status}).`);
      const data = await response.json();
      if (request !== state.request || id !== state.selected) return;
      state.events = state.events.concat(data.events || []); state.nextCursor = data.next_cursor || state.nextCursor; state.more = Boolean(data.has_more);
    } catch (error) { if (request === state.request) showError(error.message || "Unable to load event history."); }
    finally { if (request === state.request) { state.eventBusy = false; renderDetail(); } }
  }
  async function refresh() {
    if (state.fetching) return;
    state.fetching = true; $("refresh").disabled = true;
    try {
      const response = await fetch("/api/snapshot", { cache: "no-store" });
      if (!response.ok) throw new Error(`Snapshot unavailable (HTTP ${response.status}).`);
      applySnapshot(await response.json());
      if (!state.live) setConnection("snapshot loaded · reconnecting live stream", "connecting");
    } catch (error) { showError(error.message || "Cannot reach the local collector."); setConnection("disconnected · retrying", "down"); }
    finally { state.fetching = false; $("refresh").disabled = false; }
  }
  function setView(view) { state.view = view; renderViews(); if (state.snapshot) renderTable(); }
  // j/k step between top-level agents; from a subagent they step from its top-level agent.
  function move(step) {
    const byId = new Map(state.snapshot.runs.map((r) => [r.run_id, r]));
    const tops = state.order.filter((id) => !byId.has(byId.get(id)?.parent_run_id));
    if (!tops.length) return;
    const top = tops.find((id) => state.selected && within(state.selected, id));
    const index = tops.indexOf(top);
    const next = tops[Math.min(tops.length - 1, Math.max(0, index === -1 ? 0 : index + step))];
    selectRun(next);
    [...$("runs").querySelectorAll("[data-run-id]")].find((el) => el.dataset.runId === next)?.focus({ preventScroll: false });
  }

  // Status and agent columns take a dragged width; WORKING ON keeps what is left.
  const widths = (() => { try { return JSON.parse(localStorage.getItem(WIDTH_KEY)) || {}; } catch (_) { return {}; } })();
  function setWidth(handle, width) {
    const key = handle.dataset.resize;
    const table = handle.closest(".table");
    if (width === null) { delete widths[key]; table.style.removeProperty(`--${key}-w`); } else {
      const room = handle.parentElement.getBoundingClientRect().width + handle.parentElement.nextElementSibling.getBoundingClientRect().width - 120;
      widths[key] = Math.round(Math.max(key === "status" ? 64 : 100, Math.min(room, width)));
      table.style.setProperty(`--${key}-w`, `${widths[key]}px`);
    }
    try { localStorage.setItem(WIDTH_KEY, JSON.stringify(widths)); } catch (_) { /* widths are a convenience */ }
  }
  document.querySelectorAll("[data-resize]").forEach((handle) => {
    const key = handle.dataset.resize;
    if (widths[key]) handle.closest(".table").style.setProperty(`--${key}-w`, `${widths[key]}px`);
    handle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      const start = event.clientX - handle.parentElement.getBoundingClientRect().width;
      handle.setPointerCapture(event.pointerId);
      handle.classList.toggle("dragging", true);
      const drag = (e) => setWidth(handle, e.clientX - start);
      const stop = () => { handle.classList.toggle("dragging", false); handle.removeEventListener("pointermove", drag); handle.removeEventListener("lostpointercapture", stop); };
      handle.addEventListener("pointermove", drag);
      handle.addEventListener("lostpointercapture", stop);
    });
    handle.addEventListener("dblclick", () => setWidth(handle, null));
    handle.addEventListener("keydown", (event) => {
      const step = { ArrowLeft: -16, ArrowRight: 16 }[event.key];
      if (!step) return;
      event.preventDefault();
      setWidth(handle, handle.parentElement.getBoundingClientRect().width + step);
    });
  });
  [...$("views").children].forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  document.querySelectorAll("[data-sort]").forEach((button) => button.addEventListener("click", () => {
    const key = button.dataset.sort;
    if (state.sort === key && state.descending !== ["rate", "seen"].includes(key)) {
      state.sort = null;
      state.descending = false;
    } else {
      state.descending = state.sort === key ? !state.descending : ["rate", "seen"].includes(key);
      state.sort = key;
    }
    renderSort();
    if (state.snapshot) renderTable();
  }));
  renderSort();
  [...$("zoom").children].forEach((b) => b.addEventListener("click", () => setZoom(Number(b.dataset.zoom))));
  $("harness-filter").addEventListener("change", render);
  $("window-filter").addEventListener("change", () => { if (state.snapshot) renderTable(); });
  $("search").addEventListener("input", () => { if (state.snapshot) renderTable(); });
  $("refresh").addEventListener("click", refresh);
  $("hidden-note").addEventListener("click", () => { if (state.view === "active") setView("all"); });
  // On narrow screens the header actions and the shortcut list fold behind a button each.
  const fold = (button, host, open) => { button.setAttribute("aria-expanded", String(open)); host.classList.toggle("open", open); };
  const foldMenu = (open) => fold($("more"), $("top-actions"), open);
  const foldKeys = (open) => fold($("keys-toggle"), $("keys"), open);
  $("more").addEventListener("click", () => foldMenu($("more").getAttribute("aria-expanded") !== "true"));
  const closeMenu = () => { foldMenu(false); if (!document.querySelector?.("dialog[open]")) $("more").focus(); };
  $("top-actions").addEventListener("click", (event) => { if (event.target?.closest?.("button")) closeMenu(); });
  $("keys-toggle").addEventListener("click", () => foldKeys($("keys-toggle").getAttribute("aria-expanded") !== "true"));
  document.addEventListener("click", (event) => { if (!event.target?.closest?.("#more, #top-actions")) foldMenu(false); });
  document.addEventListener("keydown", (event) => {
    if (document.querySelector?.("dialog[open]")) return;
    const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(String(event.target?.tagName).toUpperCase());
    if (event.key === "Escape") {
      if ($("more").getAttribute("aria-expanded") === "true") closeMenu(); else if (typing) event.target.blur(); else selectRun(null);
      return;
    }
    if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
    const actions = { "/": () => $("search").focus(), j: () => move(1), ArrowDown: () => move(1), k: () => move(-1), ArrowUp: () => move(-1),
      a: () => setView("active"), w: () => setView("waiting"), e: () => setView("all"), v: () => $("overview").classList.toggle("collapsed"), l: backToLive, L: backToLive, r: restSelected, s: foldSelected,
      "?": () => foldKeys($("keys-toggle").getAttribute("aria-expanded") !== "true") };
    if (actions[event.key]) { event.preventDefault(); actions[event.key](); }
  });
  renderViews();
  refresh();
  const stream = new EventSource("/api/events?after=0");
  stream.onopen = () => { state.live = true; setConnection("live", "connected"); };
  stream.onerror = () => { state.live = false; setConnection("live stream disconnected · retrying", "down"); };
  ["snapshot", "reset"].forEach((name) => stream.addEventListener(name, (event) => {
    try { applySnapshot(JSON.parse(event.data), name === "reset"); state.live = true; setConnection(name === "reset" ? "live · history reset" : "live", "connected"); }
    catch (_) { showError("Received an invalid update. Refresh to recover."); }
  }));
  setInterval(() => { if (!state.live || Date.now() - state.lastSnapshotAt > 5000) refresh(); }, 2000);
})();
