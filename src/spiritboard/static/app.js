"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const terminal = new Set(["completed", "failed", "cancelled"]);
  const PALETTE = ["#8fa8ff", "#c4b5fd", "#f0abfc", "#fda4af", "#7dd3fc", "#fdba74", "#a7f3d0", "#e9d5ff"];
  const QUIET_WINDOW = 30 * 60 * 1000;
  const MAX_ROWS = 300;
  const state = { snapshot: null, selected: null, view: "active", sort: null, descending: false, order: [], frozen: new Map(), colors: new Map(), events: [], nextCursor: 0, more: false, eventBusy: false, request: 0, fetching: false, live: false, lastSnapshotAt: 0, fingerprint: null, historyOpen: false, tableKey: "", target: null, targetNote: "", targetFallback: false, latest: null, past: null, pastRequest: 0, zoom: 600, chart: null, chartAt: 0 };
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
  function showError(message) { $("error").hidden = !message; $("error").textContent = message || ""; }
  function setConnection(text, connected) { $("connection").textContent = text; $("connection").className = connected ? "connected" : "muted"; }
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
    const name = profile.label.toLowerCase().startsWith(profile.kind.toLowerCase()) ? profile.label : `${profile.kind} · ${profile.label}`;
    return `${name}${same ? ` · ${profile.id}` : ""}`;
  }
  function healthOf(run) {
    const health = typeof run.health === "object" ? run.health?.state : run.health;
    if (health === "observed" || terminal.has(run.state)) return health || "unknown";
    const last = time(run.last_seen);
    if (health === "fresh" && last !== null && serverNow() - last > (state.snapshot?.stale_after_seconds || 30) * 1000) return "stale";
    return health || "unknown";
  }
  function statusOf(run) {
    const inferred = run.inferred ? "~" : "";
    if (run.state === "completed") return { key: "done", glyph: "✓", text: `done${inferred}` };
    if (run.state === "failed") return { key: "failed", glyph: "✗", text: `failed${inferred}` };
    if (run.state === "cancelled") return { key: "done", glyph: "–", text: `cancelled${inferred}` };
    if (run.archived) return { key: "closed", glyph: "■", text: `archived ${age(run.last_seen)}` };
    // No live process owns it (checked, not guessed): the session has ended.
    if (run.live === false) return { key: "closed", glyph: "■", text: `closed ${age(run.last_seen)}` };
    // An unanswered approval stops a transcript from changing; it still needs you.
    if (run.state === "waiting_user") return { key: "needs", glyph: "◐", text: `needs you${inferred}` };
    // A top-level session that recently finished its turn is waiting for your next direction.
    if (run.state === "idle" && !run.parent_run_id && serverNow() - (time(run.last_seen) || 0) <= QUIET_WINDOW)
      return { key: "needs", turn: true, glyph: "↩", text: `your turn${inferred}` };
    if (run.state === "idle" && run.live === true && !run.parent_run_id) return { key: "idle", glyph: "◌", text: `open · idle ${age(run.last_seen)}` };
    if (!["fresh", "observed"].includes(healthOf(run))) return { key: "quiet", glyph: "○", text: `quiet ${age(run.last_seen)}` };
    // Waiting on a tool or a child is ordinary work, not something for you.
    if (String(run.state).startsWith("waiting")) return { key: "running", glyph: "●", text: `running${inferred}` };
    if (run.state === "idle") return { key: "idle", glyph: "◌", text: `idle${inferred}` };
    if (run.state === "unknown") return { key: "unknown", glyph: "?", text: "unknown" };
    return { key: "running", glyph: "●", text: `running${inferred}` };
  }
  function doingOf(run, status) {
    if (terminal.has(run.state)) return { mark: "›", text: run.summary || run.activity || run.task || "finished", cls: "" };
    const summary = run.ai_summary;
    if (summary?.text) return { mark: "", ai: true, text: summary.text, cls: status.key === "needs" ? "needs" : summary.stale ? "stale-summary" : "" };
    // The last reply says most about a finished turn; the request is the fallback.
    const replied = run.activity && !run.activity.startsWith("Turn ended");
    const raw = status.turn && !replied ? run.task || run.activity : run.activity || run.task;
    if (!raw) return { mark: "›", text: run.inferred ? "text hidden · enable include_text" : "no activity reported", cls: "dim" };
    if (status.key === "quiet" || status.key === "closed") return { mark: "›", text: `last said: ${raw}`, cls: "dim" };
    return { mark: "›", text: raw, cls: status.key === "needs" ? "needs" : "" };
  }
  // Desktop metadata and /rename take priority, even over a cached summary title.
  const nameOf = (run) => (run.app === "Claude desktop" && run.name) || run.ai_summary?.title || run.name || "Unnamed run";
  function inView(run) {
    const status = statusOf(run);
    if (state.view === "waiting") return status.key === "needs";
    if (state.view === "active") {
      if (terminal.has(run.state) || run.archived) return false;
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
      (!query || [run.name, run.ai_summary?.title, run.task, run.activity, run.ai_summary?.text, run.project, run.app, harnessOf(run).label].some((v) => String(v || "").toLowerCase().includes(query)));
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
    renderHarnessFilter(list); renderCounts(); renderAi(); renderWarnings(); renderHarnesses(list); renderFlux(current); renderPast();
    // Rebuild the table only when something visible changed, so clicks and focus survive live ticks.
    if (tableKey() !== state.tableKey) renderTable(); else updateSeen();
  }
  function activeStatuses(runs) {
    // Same scope as the active view: long-quiet history is not counted.
    return runs.filter((run) => !terminal.has(run.state) && !run.archived && (statusOf(run).key !== "quiet" || serverNow() - (time(run.last_seen) || 0) <= QUIET_WINDOW)).map(statusOf);
  }
  const BUSY = new Set(["running", "waiting", "idle", "unknown"]);
  function renderCounts() {
    const statuses = activeStatuses(state.snapshot.runs);
    const counts = [["running", "●", statuses.filter((s) => BUSY.has(s.key)).length, "running"],
      ["needs", "◐", statuses.filter((s) => s.key === "needs").length, "needs you"],
      ["quiet", "○", statuses.filter((s) => s.key === "quiet").length, "quiet"]];
    $("counts").replaceChildren(...counts.map(([key, glyph, value, text]) => {
      const item = node("span"); item.append(node("span", glyph, `g-${key}`), " ", node("strong", value), ` ${text}`); return item;
    }));
  }
  function renderAi() {
    const ai = state.snapshot.ai || {};
    const host = $("ai-status"); host.replaceChildren();
    if (!ai.enabled) { host.append(node("span", "summaries off", "muted")); host.title = "Open AI summaries to connect a provider or resume summaries"; return; }
    const spent = `$${(ai.known_cost_usd ?? ai.actual_cost_usd ?? 0).toFixed(4)}${ai.unknown_cost_calls ? "+" : ""}`;
    const usage = [];
    if (ai.api_calls !== 0) usage.push(`${spent} spent · ${ai.api_calls ?? ai.calls ?? 0} API calls`);
    if (ai.subscription_calls) usage.push(`${ai.subscription_calls} ChatGPT plan calls`);
    if (!usage.length) usage.push("no provider calls yet");
    const provider = ai.provider === "codex" ? "ChatGPT" : ai.provider === "openrouter" ? "OpenRouter" : "";
    host.append(node("span", `summaries ${ai.status === "error" ? "error" : "on"}${provider ? ` · ${provider}` : ""}`, "ai-mark"),
      node("span", ` · ${usage.join(" · ")}`, "muted"));
    host.title = ai.error || "Opt-in: bounded redacted context goes only to connected providers; ChatGPT uses plan allowance, not a reported USD charge";
  }
  function renderWarnings() {
    const raw = state.snapshot.source_errors || [];
    const warnings = (Array.isArray(raw) ? raw : Object.entries(raw).map(([profile, message]) => ({ profile, message })))
      .map((w) => typeof w === "string" ? w : `${w.label || w.profile || "Source"}: ${w.message || "Source unavailable"}`);
    (state.snapshot.source_stats || []).filter((s) => s.truncated || s.discovery_limited)
      .forEach((s) => warnings.push(`${s.label}: scanned ${s.selected_files} of ${s.files_seen} files; older sessions may be missing.`));
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
  function renderHarnesses(list) {
    const total = list.reduce((sum, h) => sum + h.rate, 0);
    const busiest = Math.max(0, ...list.map((h) => h.rate));
    const active = list.filter((h) => h.rate > 0).length;
    const focused = document.activeElement?.dataset?.focusKey;
    const host = $("harnesses");
    // Only harnesses with something running (or the one filtered on); idle ones stay in the filter.
    const running = (h) => h.rate > 0 || activeStatuses(h.runs).some((s) => BUSY.has(s.key) || s.key === "needs");
    const shown = list.filter((h) => running(h) || $("harness-filter").value === h.key);
    host.replaceChildren(...shown.map((h) => {
      const button = node("button", null, `harness${$("harness-filter").value === h.key ? " is-selected" : ""}`);
      button.type = "button"; button.dataset.focusKey = `harness:${h.key}`;
      button.setAttribute("aria-pressed", String($("harness-filter").value === h.key));
      const swatch = node("span", null, "swatch"); swatch.style.background = state.colors.get(h.key);
      const statuses = activeStatuses(h.runs);
      const parts = [[(s) => BUSY.has(s.key), "running"], [(s) => s.key === "needs", "needs you"], [(s) => s.key === "quiet", "quiet"]]
        .map(([test, text]) => [statuses.filter(test).length, text]).filter(([n]) => n).map(([n, text]) => `${n} ${text}`);
      const sub = parts.join(" · ") || (h.runs.length ? "nothing active" : "no observations yet");
      const name = node("span", null, "who"); name.title = `${h.label} · ${sub}`; name.append(node("span", h.label, "name"), node("span", sub, "sub"));
      const rate = node("span", h.rate, "rate"); if (h.rate === busiest && h.rate) rate.style.color = state.colors.get(h.key);
      const noisy = active > 1 && h.rate === busiest && h.rate >= 30 && h.rate >= total * .5;
      const flag = node("span", noisy ? "▲ noisy" : h.rate ? "" : h.latest ? `idle ${age(h.latest)}` : "", `flag${noisy ? " noisy" : ""}`);
      button.append(swatch, name, rate, flag);
      button.addEventListener("click", () => { $("harness-filter").value = $("harness-filter").value === h.key ? "" : h.key; render(); });
      return button;
    }));
    if (!list.length) host.append(node("p", "No harnesses yet. Start the demo or configure a source profile.", "empty"));
    else if (!shown.length) host.append(node("p", state.past ? "Nothing running at this moment." : "Nothing running right now.", "empty"));
    const idle = list.length - shown.length;
    if (idle) host.append(node("p", `${idle} idle harness${idle === 1 ? "" : "es"} · pick one in the harness filter`, "harness-idle muted"));
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
    $("past-bar").hidden = !state.past;
    if (!state.past) return;
    const when = new Date(state.past.at).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    pastText.textContent = `Viewing ${when} · ${age(state.past.at, liveNow())} ago · `;
  }
  function renderFlux(list) {
    const host = $("flux"); host.replaceChildren();
    const data = chartData(); const count = data.buckets || 60; const bucket = data.bucket_seconds || 10; const scale = 60 / bucket;
    const windowText = ZOOMS[state.zoom][0];
    $("flux-note").textContent = `height = ${bucket <= 60 ? "events/min" : `events per ${span(bucket)}`} · dots = latest events`;
    const byHarness = list.map((h) => {
      const series = new Array(count).fill(0);
      h.runs.forEach((run) => (data.runs?.[run.run_id] || []).forEach((v, i) => { series[i] += v; }));
      return { key: h.key, label: h.label, series };
    }).filter((h) => h.series.some(Boolean));
    const totals = new Array(count).fill(0).map((_, i) => byHarness.reduce((sum, h) => sum + h.series[i], 0));
    const peak = Math.max(...totals);
    if (!peak) { host.append(node("span", `No events in the ${windowText}`, "empty")); host.setAttribute("aria-label", `No events in the ${windowText}`); return; }
    const height = 170; const unit = height / Math.max(peak, 3);
    totals.forEach((_, i) => {
      const col = node("div", null, "col");
      // Top-to-bottom matches the harness list order reversed, so the first harness sits on the baseline.
      [...byHarness].reverse().forEach((h) => {
        if (!h.series[i]) return;
        const seg = node("span"); seg.style.height = `${h.series[i] * unit}px`; seg.style.background = state.colors.get(h.key); col.append(seg);
      });
      col.addEventListener("click", () => travel(i, data));
      host.append(col);
    });
    // Deterministic spray per absolute bucket so dots stay put as time advances.
    const endBucket = Math.floor((time(data.end) || liveNow()) / 1000 / bucket);
    const noise = (n) => { const x = Math.sin(n * 12.9898) * 43758.5453; return x - Math.floor(x); };
    for (let i = Math.max(0, count - 12); i < count; i++) {
      const top = byHarness.reduce((best, h) => h.series[i] > (best?.series[i] || 0) ? h : best, null);
      const dots = Math.min(15, Math.ceil(totals[i] / 2));
      for (let j = 0; j < dots; j++) {
        const seed = (endBucket - (count - 1 - i)) * 31 + j;
        const dot = node("span", null, "dot"); const size = 1.5 + noise(seed + .3) * 3;
        dot.style.left = `${(i + noise(seed)) / count * 100}%`;
        dot.style.bottom = `${Math.max(0, totals[i] * unit + (noise(seed + .7) - .3) * 36)}px`;
        dot.style.width = dot.style.height = `${size}px`;
        dot.style.background = noise(seed + .9) > .3 && top ? state.colors.get(top.key) : "#e6e9ef";
        dot.style.opacity = String(.4 + noise(seed + .5) * .6);
        host.append(dot);
      }
    }
    if (state.past) {
      const start = (time(data.end) || liveNow()) - count * bucket * 1000;
      const x = (state.past.at - start) / (count * bucket * 1000);
      if (x >= 0 && x <= 1) { const marker = node("span", null, "marker"); marker.style.left = `${x * 100}%`; host.append(marker); }
    }
    host.append(node("span", bucket <= 60 ? `${peak * scale}/min` : `${peak}/${span(bucket)}`, "ymax"));
    const recent = Math.max(1, Math.round(scale));
    const busiest = byHarness.map((h) => [h.label, h.series.slice(-recent).reduce((a, b) => a + b, 0)]).sort((a, b) => b[1] - a[1])[0];
    host.setAttribute("aria-label", `Events over the ${windowText}, stacked by harness. Busiest in the latest ${bucket <= 60 ? "minute" : span(bucket)}: ${busiest[0]} with ${busiest[1]}.`);
  }

  function workRank(run) {
    const key = statusOf(run).key;
    return { needs: 0, running: 1, waiting: 2, idle: 3, unknown: 4, quiet: 5, failed: 6, done: 7 }[key] ?? 8;
  }
  function sortValue(run) {
    if (state.sort === "harness") return harnessOf(run).label.toLowerCase();
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
      state.events.length, state.eventBusy, state.more, state.target, state.targetNote, state.targetFallback, Math.floor(serverNow() / 60000)]);
  }
  function seenText(value) {
    const seen = time(value);
    return seen !== null && serverNow() - seen < 2000 ? "now" : age(value);
  }
  function updateSeen() { [...$("runs").querySelectorAll("[data-seen]")].forEach((el) => { el.textContent = seenText(el.dataset.seen); }); }
  function renderTable() {
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
    const sorted = (a, b) => {
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
    const renderRun = (run, prefix, pad, orphan) => {
      if (visited.has(run.run_id) || !visible.has(run.run_id) || state.order.length >= MAX_ROWS) return;
      visited.add(run.run_id);
      state.order.push(run.run_id);
      host.append(row(run, prefix, !direct.has(run.run_id), orphan, maxRate));
      if (state.selected === run.run_id) host.append(expansion(run, byId));
      const kids = (children.get(run.run_id) || []).filter((c) => visible.has(c.run_id)).sort(compare);
      kids.forEach((child, i) => {
        const last = i === kids.length - 1;
        renderRun(child, `${pad}${last ? "└─ " : "├─ "}`, `${pad}${last ? "   " : "│  "}`, false);
      });
    };
    runs.filter((r) => !r.parent_run_id || !byId.has(r.parent_run_id)).sort(compare)
      .forEach((r) => renderRun(r, r.parent_run_id ? "?─ " : "", r.parent_run_id ? "   " : "", Boolean(r.parent_run_id)));
    if (!state.selected) state.frozen = new Map(state.order.map((id, i) => [id, i]));
    if (visited.size < visible.size && state.order.length >= MAX_ROWS) host.append(node("p", `Showing the first ${MAX_ROWS} agents. Narrow the view or search to see more.`, "empty"));
    if (focused) [...host.querySelectorAll("[data-focus-key]")].find((el) => el.dataset.focusKey === focused)?.focus({ preventScroll: true });
  }
  function row(run, prefix, context, orphan, maxRate) {
    const status = statusOf(run);
    const depth = Math.round(prefix.length / 3);
    const button = node("button", null, `row run-row ${status.key}${depth ? " sub" : ""}${state.selected === run.run_id ? " selected" : ""}${context ? " context" : ""}`);
    // Subagents shift right as a block, so they never read as top-level processes.
    if (depth) button.style.setProperty("--depth", String(depth));
    button.type = "button"; button.dataset.focusKey = `select:${run.run_id}`; button.dataset.runId = run.run_id;
    button.setAttribute("aria-current", String(state.selected === run.run_id));
    button.setAttribute("aria-expanded", String(state.selected === run.run_id));
    const statusCell = node("span", `${state.selected === run.run_id ? "▾" : "▸"} ${status.glyph} ${status.text}`, `cell status g-${status.key}`);
    const agent = node("span", null, "cell");
    agent.append(node("span", prefix, "tree"), node("span", nameOf(run), "agent-name"));
    if (orphan) agent.append(node("span", "parent not yet available", "note"));
    if (context) agent.append(node("span", "context", "note"));
    // Where the session runs, when it is an app rather than a terminal (Claude desktop).
    if (run.app) agent.append(node("span", run.app, "note"));
    const harness = harnessOf(run);
    const harnessCell = node("span", null, "cell harness-cell");
    const swatch = node("span", null, "swatch"); swatch.style.background = state.colors.get(harness.key);
    const where = run.app ? `${harness.label} · ${run.app}` : harness.label;
    harnessCell.title = where; harnessCell.setAttribute("aria-label", where);
    harnessCell.append(swatch);
    const doing = doingOf(run, status);
    const doingCell = node("span", null, `cell doing ${doing.cls}`);
    if (doing.mark) doingCell.append(node("span", `${doing.mark} `, "mark"));
    doingCell.append(doing.text);
    if (doing.ai) doingCell.title = `${run.ai_summary?.origin === "claude-recap" ? "Claude recap" : "AI summary"}${run.ai_summary?.stale ? " · goal changed, refresh due" : ""}`;
    const rate = rateOf(run.run_id);
    const rateCell = node("span", null, "rate-cell");
    const bar = node("span", null, "bar"); bar.style.width = `${Math.round(rate / maxRate * 56)}px`; bar.style.background = state.colors.get(harness.key);
    rateCell.append(node("span", rate, "num"), bar);
    const last = node("span", seenText(run.last_seen), "cell seen");
    last.dataset.seen = run.last_seen || "";
    button.append(harnessCell, statusCell, agent, doingCell, rateCell, last);
    button.addEventListener("click", () => selectRun(state.selected === run.run_id ? null : run.run_id));
    return button;
  }
  function renderHiddenNote(runs, direct) {
    const note = $("hidden-note");
    if (state.view === "all") { note.textContent = `${direct.size} of ${runs.length} agents in window`; return; }
    const finished = runs.filter((r) => terminal.has(r.state)).length;
    const older = runs.filter((r) => !terminal.has(r.state) && !inView(r)).length;
    note.textContent = state.view === "active" ? `${finished} finished · ${older} older hidden · e to show` : `${direct.size} waiting`;
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
      const detail = payload.text || payload.summary || payload.detail || payload.reason || payload.task || payload.label || payload.name || payload.state || payload.outcome || "";
      const key = `${event.type}\u0000${detail}`;
      const last = groups[groups.length - 1];
      if (last && last.key === key) { last.count += 1; last.stamp = event.received_at || event.occurred_at; }
      else groups.push({ key, type: event.type, detail, count: 1, stamp: event.received_at || event.occurred_at, late: event.applied === false });
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
    title.append(node("h3", nameOf(run)), node("span", `${status.glyph} ${status.text}`, `pill g-${status.key}`));
    const actions = node("div", null, "detail-actions");
    actions.append(...targetControls(run));
    const links = node("div", null, "detail-links");
    const history = node("button", state.historyOpen ? "hide history" : "history ▸", "link"); history.type = "button"; history.dataset.focusKey = "history";
    history.addEventListener("click", () => { state.historyOpen = !state.historyOpen; if (state.historyOpen && !state.events.length) loadEvents(true); else renderTable(); });
    const close = node("button", "esc close", "link"); close.type = "button"; close.addEventListener("click", () => selectRun(null));
    links.append(history, close);
    head.append(title, actions, links);
    const meta = node("p", null, "detail-meta"); meta.setAttribute("aria-label", "Session");
    const fact = (key, ...content) => { const item = node("span"); if (key) item.append(node("span", key, "k")); item.append(...content); meta.append(item); return item; };
    const swatch = node("span", null, "swatch"); swatch.style.background = state.colors.get(harness.key);
    fact("", swatch, harness.label);
    if (run.app) fact("in", run.app);
    if (run.project) fact("project", run.project);
    fact(terminal.has(run.state) || status.key === "closed" ? "ran for" : "running for", age(run.started_at, time(run.ended_at) || serverNow()));
    const seen = node("span", seenText(run.last_seen)); seen.dataset.seen = run.last_seen || "";
    fact("last seen", seen);
    main.append(head, meta);
    if (status.turn) {
      const turn = node("p", `↩ finished its turn ${age(run.last_seen)} ago · waiting for your next direction`, "needs-line");
      turn.append(node("span", ` · continue it in ${harness.label}`, "needs-where"));
      main.append(turn);
    } else if (run.archived) {
      main.append(node("p", `■ archived in ${run.app || "its app"} · last active ${age(run.last_seen)} ago`, "closed-line"));
    } else if (status.key === "closed") {
      main.append(node("p", `■ closed ${age(run.last_seen)} ago · no running process owns this session any more`, "closed-line"));
    } else if (status.key === "needs") {
      const needs = node("p", `◐ waiting on you for ${age(run.last_seen)}${run.activity ? ` · ${run.activity}` : ""}`, "needs-line");
      needs.append(node("span", ` · answer it in “${parent ? nameOf(parent) : nameOf(run)}”`, "needs-where"));
      main.append(needs);
    }
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
    field("now", terminal.has(run.state) ? run.summary || label(run.state) : run.activity || "no activity reported");
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
    if (operations.length) {
      const ops = node("ul", null, "ops");
      operations.slice(-10).forEach((op) => {
        const outcome = op.pending ? "running" : op.outcome || "unknown";
        const item = node("li");
        item.append(node("span", label(op.kind || "tool"), "t"), node("span", outcome, op.pending ? "pending" : outcome === "error" ? "bad" : "ok"), node("span", `${op.label || op.operation_id || "operation"}${op.detail ? ` · ${op.detail}` : ""}`));
        ops.append(item);
      });
      box.append(ops);
    }
    const timeline = node("ol", null, "timeline"); timeline.id = "timeline";
    compressEvents(state.events).reverse().forEach((group) => {
      const item = node("li"); const stamp = time(group.stamp);
      item.append(node("span", stamp ? new Date(stamp).toLocaleTimeString() : "unknown time", "t"),
        node("span", `${label(group.type || "event")}${group.count > 1 ? ` ×${group.count}` : ""}`),
        node("span", `${group.detail}${group.late ? " · late, not applied" : ""}`));
      timeline.append(item);
    });
    box.append(timeline);
    if (!state.events.length) box.append(node("p", state.eventBusy ? "Loading history…" : "No events in the available history.", "muted"));
    if (state.more) { const more = node("button", state.eventBusy ? "Loading…" : "Load older events", "link"); more.id = "load-more"; more.type = "button"; more.dataset.focusKey = "load-more"; more.disabled = state.eventBusy; more.addEventListener("click", () => loadEvents(false)); box.append(more); }
    return box;
  }
  function renderDetail() { if (state.snapshot) renderTable(); }
  async function selectRun(id) {
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
      if (!state.live) setConnection("snapshot loaded · reconnecting live stream", false);
    } catch (error) { showError(error.message || "Cannot reach the local collector."); setConnection("disconnected · retrying", false); }
    finally { state.fetching = false; $("refresh").disabled = false; }
  }
  function setView(view) { state.view = view; renderViews(); if (state.snapshot) renderTable(); }
  function move(step) {
    if (!state.order.length) return;
    const index = state.order.indexOf(state.selected);
    const next = state.order[Math.min(state.order.length - 1, Math.max(0, index === -1 ? 0 : index + step))];
    selectRun(next);
    [...$("runs").querySelectorAll("[data-run-id]")].find((el) => el.dataset.runId === next)?.focus({ preventScroll: false });
  }

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
  document.addEventListener("keydown", (event) => {
    if (document.querySelector?.("dialog[open]")) return;
    const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(event.target?.tagName);
    if (event.key === "Escape") { if (typing) event.target.blur(); else selectRun(null); return; }
    if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
    const actions = { "/": () => $("search").focus(), j: () => move(1), ArrowDown: () => move(1), k: () => move(-1), ArrowUp: () => move(-1),
      a: () => setView("active"), w: () => setView("waiting"), e: () => setView("all"), v: () => $("overview").classList.toggle("collapsed"), l: backToLive, L: backToLive };
    if (actions[event.key]) { event.preventDefault(); actions[event.key](); }
  });
  renderViews();
  refresh();
  const stream = new EventSource("/api/events?after=0");
  stream.onopen = () => { state.live = true; setConnection("live", true); };
  stream.onerror = () => { state.live = false; setConnection("live stream disconnected · retrying", false); };
  ["snapshot", "reset"].forEach((name) => stream.addEventListener(name, (event) => {
    try { applySnapshot(JSON.parse(event.data), name === "reset"); state.live = true; setConnection(name === "reset" ? "live · history reset" : "live", true); }
    catch (_) { showError("Received an invalid update. Refresh to recover."); }
  }));
  setInterval(() => { if (!state.live || Date.now() - state.lastSnapshotAt > 5000) refresh(); }, 2000);
})();
