"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const terminal = new Set(["completed", "failed", "cancelled"]);
  const PALETTE = ["#7aa2f7", "#5eead4", "#c3a6ff", "#f0a07a", "#e6c07b", "#94e2d5", "#f5a3c7", "#a3be8c"];
  const QUIET_WINDOW = 30 * 60 * 1000;
  const MAX_ROWS = 300;
  const state = { snapshot: null, selected: null, view: "active", order: [], colors: new Map(), events: [], nextCursor: 0, more: false, eventBusy: false, request: 0, fetching: false, live: false, lastSnapshotAt: 0, fingerprint: null, historyOpen: false, tableKey: "", usageKey: "" };
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
  const serverNow = () => (time(state.snapshot?.server_time) || state.lastSnapshotAt || Date.now()) + Math.max(0, Date.now() - state.lastSnapshotAt);
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
    // An unanswered approval stops a transcript from changing; it still needs you.
    if (run.state === "waiting_user") return { key: "needs", glyph: "◐", text: `needs you${inferred}` };
    // A top-level session that recently finished its turn is waiting for your next direction.
    if (run.state === "idle" && !run.parent_run_id && serverNow() - (time(run.last_seen) || 0) <= QUIET_WINDOW)
      return { key: "needs", turn: true, glyph: "↩", text: `your turn${inferred}` };
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
    if (summary?.text) return { mark: "✦", ai: true, text: summary.text, cls: status.key === "needs" ? "needs" : summary.stale ? "stale-summary" : "" };
    // After a finished turn the latest activity is just "turn ended"; the request says more.
    const raw = status.turn ? run.task || run.activity : run.activity || run.task;
    if (!raw) return { mark: "›", text: run.inferred ? "text hidden · enable include_text" : "no activity reported", cls: "dim" };
    if (status.key === "quiet") return { mark: "›", text: `last said: ${raw}`, cls: "dim" };
    return { mark: "›", text: raw, cls: status.key === "needs" ? "needs" : "" };
  }
  // Session titles (Claude ai-title, Codex thread name) come with summaries.
  const nameOf = (run) => run.ai_summary?.title || run.name || "Unnamed run";
  function inView(run) {
    const status = statusOf(run);
    if (state.view === "waiting") return status.key === "needs";
    if (state.view === "active") {
      if (terminal.has(run.state)) return false;
      return status.key !== "quiet" || serverNow() - (time(run.last_seen) || 0) <= QUIET_WINDOW;
    }
    const window = Number($("window-filter").value);
    return !window || serverNow() - (time(run.last_seen) || 0) <= window;
  }
  function matches(run) {
    const query = $("search").value.toLowerCase().trim();
    const harness = $("harness-filter").value;
    return inView(run) && (!harness || harnessOf(run).key === harness) &&
      (!query || [run.name, run.ai_summary?.title, run.task, run.activity, run.ai_summary?.text, run.project, harnessOf(run).label].some((v) => String(v || "").toLowerCase().includes(query)));
  }

  function activity() { return state.snapshot?.activity || { bucket_seconds: 10, buckets: 60, runs: {} }; }
  function rateOf(runId) {
    const data = activity(); const series = data.runs?.[runId];
    if (!Array.isArray(series)) return 0;
    const perMinute = Math.max(1, Math.round(60 / data.bucket_seconds));
    return series.slice(-perMinute).reduce((a, b) => a + b, 0);
  }
  function harnesses() {
    const runs = state.snapshot.runs;
    const map = new Map();
    (state.snapshot.profiles || []).forEach((p) => map.set(`profile:${p.id}`, { key: `profile:${p.id}`, label: profileLabel(p), runs: [] }));
    runs.forEach((run) => {
      const h = harnessOf(run);
      if (!map.has(h.key)) map.set(h.key, { ...h, runs: [] });
      map.get(h.key).runs.push(run);
    });
    const list = [...map.values()].sort((a, b) => a.key.localeCompare(b.key));
    state.colors = new Map(list.map((h, i) => [h.key, PALETTE[i % PALETTE.length]]));
    list.forEach((h) => { h.rate = h.runs.reduce((sum, run) => sum + rateOf(run.run_id), 0); h.latest = h.runs.reduce((m, run) => Math.max(m, time(run.last_seen) || 0), 0); });
    return list;
  }

  function applySnapshot(snapshot, reset = false) {
    if (!snapshot || !Array.isArray(snapshot.runs)) throw new Error("Invalid dashboard snapshot.");
    const previousCursor = state.snapshot?.cursor;
    const replacedJournal = Boolean(state.snapshot?.stream_id && snapshot.stream_id && state.snapshot.stream_id !== snapshot.stream_id);
    const historyReset = reset || replacedJournal || (typeof previousCursor === "number" && snapshot.cursor < previousCursor);
    if (historyReset) {
      state.events = []; state.nextCursor = 0; state.more = false; state.eventBusy = false;
      state.request += 1; // Ignore any timeline request from the previous journal.
    }
    state.snapshot = snapshot;
    state.lastSnapshotAt = Date.now();
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
    const list = harnesses();
    renderHarnessFilter(list); renderCounts(); renderAi(); renderWarnings(); renderHarnesses(list); renderFlux(list); renderUsage(list);
    // Rebuild the table only when something visible changed, so clicks and focus survive live ticks.
    if (tableKey() !== state.tableKey) renderTable(); else updateSeen();
  }
  function activeStatuses(runs) {
    // Same scope as the active view: long-quiet history is not counted.
    return runs.filter((run) => !terminal.has(run.state) && (statusOf(run).key !== "quiet" || serverNow() - (time(run.last_seen) || 0) <= QUIET_WINDOW)).map(statusOf);
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
    if (!ai.enabled) { host.append(node("span", "summaries off", "muted")); host.title = "Start with --summaries to opt in to OpenRouter summaries"; return; }
    const spent = `$${(ai.known_cost_usd ?? ai.actual_cost_usd ?? 0).toFixed(4)}${ai.unknown_cost_calls ? "+" : ""}`;
    host.append(node("span", `✦ summaries ${ai.status === "error" ? "error" : "on"}`, "ai-mark"),
      node("span", ` · ${spent} spent · ${ai.calls || 0} calls`, "muted"));
    host.title = ai.error || "Opt-in: bounded redacted context is sent to OpenRouter";
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
    host.replaceChildren(...list.map((h) => {
      const button = node("button", null, `harness${$("harness-filter").value === h.key ? " is-selected" : ""}`);
      button.type = "button"; button.dataset.focusKey = `harness:${h.key}`;
      button.setAttribute("aria-pressed", String($("harness-filter").value === h.key));
      const swatch = node("span", null, "swatch"); swatch.style.background = state.colors.get(h.key);
      const statuses = activeStatuses(h.runs);
      const parts = [[(s) => BUSY.has(s.key), "running"], [(s) => s.key === "needs", "needs you"], [(s) => s.key === "quiet", "quiet"]]
        .map(([test, text]) => [statuses.filter(test).length, text]).filter(([n]) => n).map(([n, text]) => `${n} ${text}`);
      const name = node("span", null); name.append(node("span", h.label, "name"), node("span", parts.join(" · ") || (h.runs.length ? "nothing active" : "no observations yet"), "sub"));
      const rate = node("span", h.rate, "rate"); if (h.rate === busiest && h.rate) rate.style.color = state.colors.get(h.key);
      const noisy = active > 1 && h.rate === busiest && h.rate >= 30 && h.rate >= total * .5;
      const flag = node("span", noisy ? "▲ noisy" : h.rate ? "" : h.latest ? `idle ${age(h.latest)}` : "", `flag${noisy ? " noisy" : ""}`);
      button.append(swatch, name, rate, flag);
      button.addEventListener("click", () => { $("harness-filter").value = $("harness-filter").value === h.key ? "" : h.key; render(); });
      return button;
    }));
    if (!list.length) host.append(node("p", "No harnesses yet. Start the demo or configure a source profile.", "empty"));
    if (focused) [...host.children].find((el) => el.dataset.focusKey === focused)?.focus({ preventScroll: true });
  }
  function renderFlux(list) {
    const host = $("flux"); host.replaceChildren();
    const data = activity(); const count = data.buckets || 60; const scale = 60 / (data.bucket_seconds || 10);
    const byHarness = list.map((h) => {
      const series = new Array(count).fill(0);
      h.runs.forEach((run) => (data.runs?.[run.run_id] || []).forEach((v, i) => { series[i] += v; }));
      return { key: h.key, label: h.label, series };
    }).filter((h) => h.series.some(Boolean));
    const totals = new Array(count).fill(0).map((_, i) => byHarness.reduce((sum, h) => sum + h.series[i], 0));
    const peak = Math.max(...totals);
    if (!peak) { host.append(node("span", "No events in the last 10 minutes", "empty")); host.setAttribute("aria-label", "No events in the last 10 minutes"); return; }
    const height = 170; const unit = height / Math.max(peak, 3);
    totals.forEach((_, i) => {
      const col = node("div", null, "col");
      // Top-to-bottom matches the harness list order reversed, so the first harness sits on the baseline.
      [...byHarness].reverse().forEach((h) => {
        if (!h.series[i]) return;
        const seg = node("span"); seg.style.height = `${h.series[i] * unit}px`; seg.style.background = state.colors.get(h.key); col.append(seg);
      });
      host.append(col);
    });
    // Deterministic spray per absolute bucket so dots stay put as time advances.
    const endBucket = Math.floor((time(data.end) || serverNow()) / 1000 / (data.bucket_seconds || 10));
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
    host.append(node("span", `${peak * scale}/min`, "ymax"));
    const busiest = byHarness.map((h) => [h.label, h.series.slice(-scale).reduce((a, b) => a + b, 0)]).sort((a, b) => b[1] - a[1])[0];
    host.setAttribute("aria-label", `Events per minute over the last 10 minutes, stacked by harness. Busiest now: ${busiest[0]} at ${busiest[1]} per minute.`);
  }

  const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const WEEKS = 53;
  const pad2 = (n) => String(n).padStart(2, "0");
  const dayKey = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  const dayName = (d) => `${WEEKDAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  function renderUsageLegend() {
    const host = $("usage-legend");
    if (host.children.length) return;
    host.append(node("span", "less"), ...[0, 1, 2, 3, 4].map((level) => node("span", null, `day l${level}`)), node("span", "more"),
      node("span", null, "day l0 seen"), node("span", "activity, no requests"));
  }
  function renderUsage(list) {
    const usage = state.snapshot.usage || { days: [], window_days: 365 };
    const harness = $("harness-filter").value;
    const today = new Date(serverNow()); today.setHours(0, 0, 0, 0);
    const labels = new Map(list.map((h) => [h.key, h.label]));
    // Rebuild only when the data, filter, day or harness names change, not on every tick.
    const key = JSON.stringify([usage, harness, dayKey(today), [...labels]]);
    if (key === state.usageKey) return;
    state.usageKey = key;
    renderUsageLegend();
    const byDate = new Map((usage.days || []).map((d) => [d.date, d]));
    const valueOf = (d) => {
      if (!d) return null;
      if (!harness) return { requests: d.requests || 0, sessions: d.sessions || 0, by: d.by_harness || {} };
      const requests = d.by_harness?.[harness] || 0; const sessions = d.sessions_by_harness?.[harness] || 0;
      return requests || sessions ? { requests, sessions, by: {} } : null;
    };
    const first = new Date(today.getFullYear(), today.getMonth(), today.getDate() - (today.getDay() + 6) % 7 - (WEEKS - 1) * 7);
    const windowStart = new Date(today.getFullYear(), today.getMonth(), today.getDate() - ((usage.window_days || 365) - 1));
    const days = [];
    for (let i = 0; i < WEEKS * 7; i++) {
      const date = new Date(first.getFullYear(), first.getMonth(), first.getDate() + i);
      days.push({ date, col: Math.floor(i / 7) + 2, row: (i % 7) + 2, inside: date >= windowStart && date <= today, value: valueOf(byDate.get(dayKey(date))) });
    }
    const counts = days.filter((d) => d.inside && d.value?.requests).map((d) => d.value.requests).sort((a, b) => a - b);
    const quantile = (q) => counts[Math.floor(q * (counts.length - 1))];
    const cuts = counts.length ? [quantile(.25), quantile(.5), quantile(.75)] : [];
    const peak = counts[counts.length - 1] || 0;
    const level = (n) => !n ? 0 : n === peak ? 4 : 1 + cuts.filter((c) => n > c).length;
    const host = $("heatmap"); host.replaceChildren();
    const place = (el, col, row) => { el.style.gridColumn = String(col); el.style.gridRow = String(row); return el; };
    [["Mon", 2], ["Wed", 4], ["Fri", 6]].forEach(([text, row]) => host.append(place(node("span", text, "wd"), 1, row)));
    let lastMonth = -10;
    days.filter((d) => d.date.getDate() === 1).forEach((d) => {
      // A label needs about three columns; drop one that would overlap its neighbour.
      if (d.col - lastMonth < 3) return;
      lastMonth = d.col; host.append(place(node("span", MONTHS[d.date.getMonth()], "mo"), d.col, 1));
    });
    let total = 0, active = 0, busiest = null;
    days.forEach((d) => {
      const cell = place(node("span", null, "day"), d.col, d.row);
      if (!d.inside) { cell.className = "day out"; cell.setAttribute("aria-hidden", "true"); host.append(cell); return; }
      const v = d.value;
      const text = `${dayName(d.date)} · ${v ? `${plural(v.requests, "request")} · ${plural(v.sessions, "session")}` : "no activity"}`;
      const breakdown = Object.entries(v?.by || {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => `\n${labels.get(k) || k}: ${plural(n, "request")}`).join("");
      cell.className = `day l${level(v?.requests)}${v && !v.requests ? " seen" : ""}`;
      cell.dataset.date = dayKey(d.date);
      cell.setAttribute("role", "img"); cell.setAttribute("aria-label", text); cell.title = text + breakdown;
      if (v) { total += v.requests; if (v.requests) active += 1; if (v.requests && v.requests >= (busiest?.value.requests || 0)) busiest = d; }
      host.append(cell);
    });
    const scope = harness ? ` · ${labels.get(harness) || harness}` : "";
    const summary = total ? `${plural(total, "request")} · ${plural(active, "active day")} · busiest ${dayName(busiest.date)} (${busiest.value.requests})${scope}`
      : `no requests recorded in the last ${usage.window_days || 365} days${scope}`;
    $("usage-summary").textContent = summary + (usage.limited ? " · partial: some transcripts were too large or unreadable" : "");
    host.setAttribute("aria-label", `Daily requests heatmap, one cell per day: ${summary}`);
    // On narrow screens the newest weeks matter most.
    if (host.parentElement) host.parentElement.scrollLeft = host.parentElement.scrollWidth;
  }

  function workRank(run) {
    const key = statusOf(run).key;
    return { needs: 0, running: 1, waiting: 2, idle: 3, unknown: 4, quiet: 5, failed: 6, done: 7 }[key] ?? 8;
  }
  function tableKey() {
    return JSON.stringify([state.fingerprint, activity().end && Math.floor(time(activity().end) / 10000), state.view,
      $("harness-filter").value, $("window-filter").value, $("search").value, state.selected, state.historyOpen,
      state.events.length, state.eventBusy, state.more, Math.floor(serverNow() / 60000)]);
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
    const compare = (a, b) => rank(a).rank - rank(b).rank || rank(b).seen - rank(a).seen || a.run_id.localeCompare(b.run_id);
    renderHiddenNote(runs, direct);
    state.order = [];
    if (!runs.length) { host.append(node("p", "No agents yet. Start the synthetic demo or configure a local source profile. This dashboard never starts agents.", "empty")); return; }
    if (!direct.size) { host.append(node("p", state.view === "active" ? "Nothing active right now. Press e to show all history." : "No agents match these filters.", "empty")); return; }
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
    const harness = harnessOf(run);
    const harnessCell = node("span", null, "cell harness-cell");
    const swatch = node("span", null, "swatch"); swatch.style.background = state.colors.get(harness.key);
    harnessCell.append(swatch, node("span", harness.label, "cell"));
    const doing = doingOf(run, status);
    const doingCell = node("span", null, `cell doing ${doing.cls}`);
    doingCell.append(node("span", `${doing.mark} `, `mark${doing.ai ? " ai" : ""}`), doing.text);
    if (doing.ai) doingCell.title = `${run.ai_summary?.origin === "claude-recap" ? "Claude recap" : "AI summary"}${run.ai_summary?.stale ? " · goal changed, refresh due" : ""}`;
    const rate = rateOf(run.run_id);
    const rateCell = node("span", null, "rate-cell");
    const bar = node("span", null, "bar"); bar.style.width = `${Math.round(rate / maxRate * 56)}px`; bar.style.background = state.colors.get(harness.key);
    rateCell.append(node("span", rate, "num"), bar);
    const last = node("span", seenText(run.last_seen), "cell");
    last.dataset.seen = run.last_seen || "";
    button.append(statusCell, agent, harnessCell, doingCell, rateCell, last);
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
    if (status.turn) {
      const turn = node("p", `↩ finished its turn ${age(run.last_seen)} ago · waiting for your next direction`, "needs-line");
      turn.append(node("span", ` · continue it in ${harnessOf(run).label}`, "needs-where"));
      host.append(turn);
    } else if (status.key === "needs") {
      const needs = node("p", `◐ waiting on you for ${age(run.last_seen)}${run.activity ? ` · ${run.activity}` : ""}`, "needs-line");
      needs.append(node("span", ` · answer it in “${parent ? nameOf(parent) : nameOf(run)}”`, "needs-where"));
      host.append(needs);
    }
    // A subagent's goal is the task its parent gave it, unless a summary exists.
    const goal = run.ai_summary?.text || (run.parent_run_id ? run.task : "");
    if (goal) host.append(node("p", goal, "goal"));
    const fields = node("dl", null, "fields");
    const field = (title, content) => { if (content) fields.append(node("dt", title), node("dd", content)); };
    field("now", terminal.has(run.state) ? run.summary || label(run.state) : run.activity || "no activity reported");
    if (!run.parent_run_id) field("you asked", run.task || (run.inferred ? "text hidden · set include_text for this profile" : ""));
    if (run.parent_run_id) {
      const dd = node("dd");
      if (parent) {
        const link = node("button", nameOf(parent), "link"); link.type = "button"; link.dataset.focusKey = `parent:${parent.run_id}`;
        link.addEventListener("click", () => selectRun(parent.run_id));
        dd.append(link);
      } else dd.append("not yet available");
      fields.append(node("dt", "parent"), dd);
    }
    host.append(fields);
    const footer = node("div", null, "expansion-footer");
    footer.append(node("span", `${harnessOf(run).label}${run.project ? ` · ${run.project}` : ""} · ${age(run.started_at, time(run.ended_at) || serverNow())}`, "muted"), node("span", "", "spacer"));
    const history = node("button", state.historyOpen ? "hide history" : "history ▸", "link"); history.type = "button"; history.dataset.focusKey = "history";
    history.addEventListener("click", () => { state.historyOpen = !state.historyOpen; if (state.historyOpen && !state.events.length) loadEvents(true); else renderTable(); });
    const close = node("button", "esc close", "link"); close.type = "button"; close.addEventListener("click", () => selectRun(null));
    footer.append(history, close);
    host.append(footer);
    if (state.historyOpen) host.append(historyList(run));
    return host;
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
    renderDetail();
    if (id) await loadEvents(true);
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
  function toggleCharts() { $("usage").hidden = $("overview").classList.toggle("collapsed"); }
  function setView(view) { state.view = view; renderViews(); if (state.snapshot) renderTable(); }
  function move(step) {
    if (!state.order.length) return;
    const index = state.order.indexOf(state.selected);
    const next = state.order[Math.min(state.order.length - 1, Math.max(0, index === -1 ? 0 : index + step))];
    selectRun(next);
    [...$("runs").querySelectorAll("[data-run-id]")].find((el) => el.dataset.runId === next)?.focus({ preventScroll: false });
  }

  [...$("views").children].forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  $("harness-filter").addEventListener("change", render);
  $("window-filter").addEventListener("change", () => { if (state.snapshot) renderTable(); });
  $("search").addEventListener("input", () => { if (state.snapshot) renderTable(); });
  $("refresh").addEventListener("click", refresh);
  document.addEventListener("keydown", (event) => {
    const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(event.target?.tagName);
    if (event.key === "Escape") { if (typing) event.target.blur(); else selectRun(null); return; }
    if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
    const actions = { "/": () => $("search").focus(), j: () => move(1), ArrowDown: () => move(1), k: () => move(-1), ArrowUp: () => move(-1),
      a: () => setView("active"), w: () => setView("waiting"), e: () => setView("all"), v: toggleCharts };
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
