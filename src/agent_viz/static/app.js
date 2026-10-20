"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const terminal = new Set(["completed", "failed", "cancelled"]);
  const state = { snapshot: null, collapsed: new Set(), selected: null, events: [], nextCursor: 0, more: false, eventBusy: false, request: 0, fetching: false, live: false, lastSnapshotAt: 0, fingerprint: null, rowClocks: [], profileClocks: [], visibleKey: "", healthKey: "" };
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
  const age = (value, now = Date.now()) => {
    const stamp = time(value);
    if (stamp === null) return "unknown";
    const seconds = Math.max(0, Math.floor((now - stamp) / 1000));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
    return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`;
  };
  const badge = (text, kind = "") => node("span", text, `badge ${kind}`);
  function showError(message) { $("error").hidden = !message; $("error").textContent = message || ""; }
  function setConnection(text, connected) { $("connection").textContent = text; $("connection").className = connected ? "connected" : "muted"; }
  function optionValues(id, values, emptyLabel, display = label) {
    const select = $(id);
    const current = select.value;
    const nextValues = [...new Set(values.filter((v) => v !== null && v !== undefined && v !== "").map(String))].sort();
    if ([...select.options].map((option) => `${option.value}:${option.textContent}`).join("\u0000") === [":" + emptyLabel, ...nextValues.map((v) => `${v}:${display(v)}`)].join("\u0000")) return;
    select.replaceChildren(new Option(emptyLabel, ""));
    nextValues.forEach((v) => select.add(new Option(display(v), v)));
    if ([...select.options].some((o) => o.value === current)) select.value = current;
  }
  function accountOf(run) { return typeof run.account === "object" ? (run.account?.label || run.account?.id || "unknown") : (run.account || "unknown"); }
  function accountKey(run) {
    const profile = (state.snapshot?.profiles || []).find((p) => run.session_id === `${p.id}:observed`);
    return profile ? `profile:${profile.id}` : `account:${accountOf(run)}`;
  }
  function accountLabel(key) {
    const profiles = state.snapshot?.profiles || [];
    const profile = profiles.find((p) => key === `profile:${p.id}`);
    if (!profile) return key.slice("account:".length);
    return profiles.filter((p) => p.label === profile.label).length > 1 ? `${profile.label} · ${profile.kind} · ${profile.id}` : profile.label;
  }
  function sourceOf(run) { return typeof run.source === "object" ? (run.source?.kind || run.source?.label || "unknown") : (run.source || "unknown"); }
  function healthOf(run) {
    const health = typeof run.health === "object" ? run.health?.state : run.health;
    // Native file observations establish evidence, not an authoritative live connection.
    if (health === "observed") return "observed";
    if (terminal.has(run.state)) return health || "unknown";
    const last = time(run.last_seen);
    const serverNow = time(state.snapshot?.server_time) || state.lastSnapshotAt;
    const now = serverNow + Math.max(0, Date.now() - state.lastSnapshotAt);
    if (health === "fresh" && last !== null && now - last > (state.snapshot?.stale_after_seconds || 30) * 1000) return "stale";
    return health || "unknown";
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
    const runs = snapshot.runs;
    const fingerprint = JSON.stringify([runs, snapshot.profiles || [], snapshot.source_errors || [], snapshot.source_stats || []]);
    const changed = fingerprint !== state.fingerprint;
    state.fingerprint = fingerprint;
    optionValues("source-filter", runs.map(sourceOf), "All sources");
    optionValues("account-filter", runs.map(accountKey).concat((snapshot.profiles || []).map((p) => `profile:${p.id}`)), "All profiles", accountLabel);
    optionValues("state-filter", runs.map((r) => r.state), "All states");
    $("demo-banner").hidden = !(snapshot.mode === "demo" || runs.some((r) => r.demo));
    showError(null);
    renderOverview(); renderWarnings(); renderAiBrief();
    if (changed || historyReset) { renderProfiles(); renderTree(); }
    updateClocks(); renderFreshness();
    if (state.selected) {
      if (changed || historyReset) renderDetail();
      // Do not reset an older paginated history while the user is reading it.
      // Once caught up, append new events using the last accepted event cursor.
      if ((previousCursor !== snapshot.cursor || historyReset) && !state.eventBusy && !state.more) loadEvents(false);
    }
  }
  function renderWarnings() {
    const raw = state.snapshot?.source_errors || [];
    const warnings = Array.isArray(raw) ? [...raw] : Object.entries(raw).map(([profile, message]) => ({ profile, message }));
    (state.snapshot?.source_stats || []).filter((stat) => stat.truncated || stat.discovery_limited).forEach((stat) => warnings.push({label:stat.label, message:`Inventory limited: selected ${stat.selected_files} of ${stat.files_seen} files found. Some sessions may be outside the scan budget.`}));
    const host = $("source-errors");
    host.replaceChildren(); host.hidden = !warnings.length;
    warnings.forEach((w) => host.append(node("p", typeof w === "string" ? w : `${w.label || w.profile || w.source || "Source"}: ${w.message || w.error || "Source unavailable"}`, "warning")));
  }
  function renderOverview() {
    const runs = state.snapshot.runs;
    const recent = runs.filter((r) => ["fresh", "observed"].includes(healthOf(r)));
    const counts = [["Observed runs", runs.length], ["Recent evidence", recent.length], ["Reported working", runs.filter((r) => r.state === "working").length], ["Reported waiting", runs.filter((r) => String(r.state).startsWith("waiting")).length], ["Confirmed finished", runs.filter((r) => terminal.has(r.state)).length]];
    $("overview").replaceChildren(...counts.map(([title, value]) => { const box = node("div", null, "stat"); box.append(node("strong", value), node("span", title)); return box; }));
  }
  function renderAiBrief() {
    const host = $("ai-brief");
    if (!host) return;
    const ai = state.snapshot.ai || {};
    host.hidden = !ai.enabled;
    host.replaceChildren();
    if (!ai.enabled) return;
    const top = node("div", null, "brief-top");
    top.append(node("h2", "Live brief / DS4F"), badge(label(ai.status || "waiting"), "inferred"));
    host.append(top);
    const runs = new Map(state.snapshot.runs.map((run) => [run.run_id, run]));
    const summaries = (ai.summaries || []).filter((entry) => {
      const run = runs.get(entry.run_id);
      const seen = time(run?.last_seen);
      return run && !run.parent_run_id && (run.state === "working" || String(run.state).startsWith("waiting")) && seen !== null && Date.now() - seen <= 900000;
    }).sort((a, b) => workRank(runs.get(a.run_id)) - workRank(runs.get(b.run_id)) || (time(runs.get(b.run_id).last_seen) || 0) - (time(runs.get(a.run_id).last_seen) || 0));
    if (!summaries.length) host.append(node("p", "Preparing a short brief from recent activity. No model call is made on every refresh.", "muted"));
    summaries.filter((entry) => runs.has(entry.run_id)).forEach((entry) => {
      const run = runs.get(entry.run_id);
      const line = node("div", null, "brief-entry");
      const select = node("button", `${sourceOf(run)} / ${accountOf(run)} · ${run.name}`, "brief-run");
      select.type = "button"; select.addEventListener("click", () => selectRun(run.run_id));
      line.append(select, node("p", entry.text, "brief-text"));
      line.append(node("span", `${entry.stale ? "Cached · " : ""}generated ${age(entry.generated_at)} ago`, "run-meta"));
      host.append(line);
    });
    const cost = typeof ai.actual_cost_usd === "number" ? `$${ai.actual_cost_usd.toFixed(6)} reported` : "charge not confirmed";
    host.append(node("p", `${ai.calls || 0}/${ai.max_calls || "—"} requests · ${cost} · $${ai.budget_usd ?? "—"} budget. Opt-in: bounded redacted context is sent to OpenRouter.`, "brief-accounting"));
    if (ai.error) host.append(node("p", ai.error, "warning"));
  }
  function workRank(run) {
    const recent = ["fresh", "observed"].includes(healthOf(run));
    if (run.state === "working") return recent ? 0 : 3;
    if (String(run.state).startsWith("waiting")) return recent ? 1 : 4;
    if (recent) return 2;
    return terminal.has(run.state) ? 6 : run.state === "unknown" ? 7 : 5;
  }
  function renderProfiles() {
    const host = $("profiles");
    if (!host) return;
    const focusedKey = document.activeElement?.dataset?.focusKey;
    const runs = state.snapshot.runs;
    const configured = state.snapshot.profiles || [];
    const profiles = configured.length ? configured : [...new Set(runs.map((r) => `${sourceOf(r)}\u0000${accountOf(r)}`))].map((key) => { const [kind, profileLabel] = key.split("\u0000"); return {id:key, kind, label:profileLabel}; });
    host.replaceChildren(); state.profileClocks = [];
    profiles.forEach((profile) => {
      const members = runs.filter((run) => configured.length ? run.session_id === `${profile.id}:observed` : sourceOf(run) === profile.kind && accountOf(run) === profile.label);
      const latest = members.reduce((stamp, run) => Math.max(stamp, time(run.last_seen) || 0), 0);
      const recent = members.filter((run) => ["fresh", "observed"].includes(healthOf(run))).length;
      const profileKey = configured.length ? `profile:${profile.id}` : `account:${profile.label}`;
      const selected = $("account-filter").value === profileKey && (!$("source-filter").value || $("source-filter").value === profile.kind);
      const card = node("button", null, `profile-card${selected ? " is-selected" : ""}`);
      card.type = "button"; card.dataset.focusKey = `profile:${profile.id}`;
      card.setAttribute("aria-label", `Filter ${accountLabel(profileKey)}`);
      card.setAttribute("aria-pressed", String(selected));
      const heading = node("span", profile.kind.toUpperCase(), "profile-kind");
      heading.append(node("span", recent ? " RECENT EVIDENCE" : " NO RECENT EVIDENCE", `profile-indicator${recent ? " recent" : ""}`));
      const evidence = node("span", "", "profile-evidence");
      state.profileClocks.push({el:evidence, latest});
      card.append(heading, node("strong", profile.label, "profile-label"), node("span", `${members.length} retained runs · ${recent} with recent evidence`, "profile-count"), evidence);
      card.addEventListener("click", () => { $("account-filter").value = selected ? "" : profileKey; $("source-filter").value = selected ? "" : profile.kind; renderProfiles(); renderTree(); });
      host.append(card);
    });
    if (focusedKey) [...host.querySelectorAll("[data-focus-key]")].find((el) => el.dataset.focusKey === focusedKey)?.focus({preventScroll:true});
    updateClocks();
  }
  function updateClocks() {
    state.rowClocks.forEach(({el, run}) => { el.textContent = `Elapsed ${age(run.started_at, time(run.ended_at) || Date.now())} · last evidence ${age(run.last_seen || run.updated_at)} ago`; });
    state.profileClocks.forEach(({el, latest}) => { el.textContent = latest ? `Last evidence ${age(latest)} ago` : "Configured · no observations yet"; });
  }
  function matches(run) {
    const query = $("search").value.toLowerCase().trim();
    const window = {"5m":300000, "1h":3600000, "24h":86400000}[$("recency-filter")?.value];
    const evidence = time(run.last_seen);
    return (!$("source-filter").value || sourceOf(run) === $("source-filter").value) &&
      (!$("account-filter").value || accountKey(run) === $("account-filter").value) &&
      (!$("state-filter").value || run.state === $("state-filter").value) &&
      (!window || (evidence !== null && Date.now() - evidence <= window)) &&
      (!query || [run.name, run.task, run.activity, run.ai_summary?.text, run.project].some((v) => String(v || "").toLowerCase().includes(query)));
  }
  function renderTree() {
    const focusedKey = document.activeElement?.dataset?.focusKey;
    const host = $("runs"); const previousScroll = host.scrollTop; host.replaceChildren(); host.setAttribute("aria-busy", "false");
    state.rowClocks = [];
    const runs = state.snapshot.runs;
    const byId = new Map(runs.map((r) => [r.run_id, r]));
    const children = new Map();
    runs.forEach((r) => { if (!children.has(r.parent_run_id)) children.set(r.parent_run_id, []); children.get(r.parent_run_id).push(r); });
    const direct = new Set(runs.filter(matches).map((r) => r.run_id));
    state.visibleKey = JSON.stringify([...direct]);
    state.healthKey = JSON.stringify(runs.map(healthOf));
    const visible = new Set(direct);
    // Preserve ancestry during filtering; context rows are explicitly distinguished.
    direct.forEach((id) => { const seen = new Set(); let r = byId.get(id); while (r?.parent_run_id && byId.has(r.parent_run_id) && !seen.has(r.parent_run_id)) { seen.add(r.parent_run_id); visible.add(r.parent_run_id); r = byId.get(r.parent_run_id); } });
    const priorities = new Map();
    const priority = (run, seen = new Set()) => {
      if (priorities.has(run.run_id)) return priorities.get(run.run_id);
      const value = {rank:workRank(run), seen:time(run.last_seen) || 0};
      if (seen.has(run.run_id)) return value;
      const path = new Set(seen); path.add(run.run_id);
      (children.get(run.run_id) || []).filter((child) => visible.has(child.run_id)).forEach((child) => {
        const branch = priority(child, path); value.rank = Math.min(value.rank, branch.rank); value.seen = Math.max(value.seen, branch.seen);
      });
      priorities.set(run.run_id, value); return value;
    };
    const compare = (a,b) => priority(a).rank - priority(b).rank || priority(b).seen - priority(a).seen || a.run_id.localeCompare(b.run_id);
    $("run-count").textContent = `(${direct.size} / ${runs.length})`;
    if (!runs.length) { host.append(node("p", "No runs yet. Start the synthetic demo or configure a local source profile. Agents are never started by this dashboard.", "empty")); return; }
    if (!direct.size) { host.append(node("p", "No runs match these filters.", "empty")); return; }
    const visited = new Set();
    const renderRun = (run, parent, depth) => {
      if (visited.has(run.run_id) || !visible.has(run.run_id)) return;
      visited.add(run.run_id);
      const group = node("div", null, "run-group");
      const recent = ["fresh", "observed"].includes(healthOf(run));
      const kind = sourceOf(run);
      const row = node("article", null, `run-row source-${kind === "codex" ? "codex" : kind === "claude" ? "claude" : "other"}${state.selected === run.run_id ? " selected" : ""}${!direct.has(run.run_id) ? " context-row" : ""}${recent ? " recent-run" : ""}`);
      row.dataset.runId = run.run_id;
      const childRuns = (children.get(run.run_id) || []).filter((r) => visible.has(r.run_id)).sort(compare);
      const top = node("div", null, "run-top");
      if (childRuns.length) {
        const toggle = node("button", state.collapsed.has(run.run_id) ? "+" : "−", "tree-toggle");
        toggle.type = "button"; toggle.setAttribute("aria-label", `${state.collapsed.has(run.run_id) ? "Expand" : "Collapse"} children of ${run.name || "run"}`); toggle.setAttribute("aria-expanded", String(!state.collapsed.has(run.run_id)));
        toggle.dataset.focusKey = `toggle:${run.run_id}`;
        toggle.addEventListener("click", () => { if (state.collapsed.has(run.run_id)) state.collapsed.delete(run.run_id); else state.collapsed.add(run.run_id); renderTree(); });
        top.append(toggle);
      } else top.append(node("span", "·", "tree-dot"));
      const select = node("button", run.name || "Unnamed run", "run-title"); select.type = "button"; select.addEventListener("click", () => selectRun(run.run_id));
      select.dataset.focusKey = `select:${run.run_id}`;
      top.append(select, badge(label(run.state), `state-${run.state}`), badge(`health: ${healthOf(run)}`, `health-${healthOf(run)}`));
      if (run.inferred) top.append(badge("inferred", "inferred"));
      if (childRuns.length) top.append(node("span", `${childRuns.length} child${childRuns.length === 1 ? "" : "ren"}`, "muted"));
      if (!direct.has(run.run_id)) top.append(badge("parent context"));
      row.append(top, node("p", `${sourceOf(run)} · ${accountOf(run)}${run.project ? ` · ${run.project}` : ""}`, "run-meta"));
      row.append(node("p", run.ai_summary?.text || run.activity || run.task || "No activity text supplied.", run.ai_summary ? "activity ai-summary" : "activity"));
      if (run.ai_summary) row.append(badge(`DS4F brief${run.ai_summary.stale ? " · cached" : ""}`, "summary-label"));
      const meta = node("p", null, "run-meta");
      const durationEnd = time(run.ended_at) || Date.now();
      meta.textContent = `Duration ${age(run.started_at, durationEnd)} · last evidence ${age(run.last_seen || run.updated_at)} ago`;
      state.rowClocks.push({el:meta, run});
      row.append(meta);
      if (run.parent_run_id && !byId.has(run.parent_run_id)) row.append(node("p", "Parent not yet available · child remains visible", "warning"));
      group.append(row);
      if (!state.collapsed.has(run.run_id) && childRuns.length) {
        const nested = node("div", null, "children"); nested.setAttribute("aria-label", `Children of ${run.name || "run"}`);
        // The contract rejects cycles; visited also prevents malformed snapshots looping forever.
        childRuns.forEach((r) => renderRun(r, nested, depth + 1)); group.append(nested);
      }
      parent.append(group);
    };
    runs.filter((r) => !r.parent_run_id || !byId.has(r.parent_run_id)).sort(compare).forEach((r) => renderRun(r, host, 0));
    // Defensive fallback keeps disconnected/cyclic malformed records visible.
    runs.filter((r) => visible.has(r.run_id) && !visited.has(r.run_id) && !byId.has(r.parent_run_id)).forEach((r) => renderRun(r, host, 0));
    if (focusedKey) [...host.querySelectorAll("[data-focus-key]")].find((el) => el.dataset.focusKey === focusedKey)?.focus({ preventScroll: true });
    host.scrollTop = previousScroll;
  }
  function field(host, title, content) { const group = node("div", null, "detail-field"); group.append(node("h3", title), node("p", content || "Not supplied", "wrap")); host.append(group); }
  function renderDetail() {
    const focusedKey = document.activeElement?.dataset?.focusKey;
    const host = $("detail"); const previousScroll = host.scrollTop; host.replaceChildren();
    const heading = node("h2", "Run detail"); heading.id = "detail-heading"; host.append(heading);
    const run = state.snapshot?.runs.find((r) => r.run_id === state.selected);
    if (!run) { host.append(node("p", "Selected run is no longer in the available history.", "empty")); return; }
    host.append(node("h3", run.name || "Unnamed run", "detail-name"));
    field(host, "Source / account", `${sourceOf(run)} · ${accountOf(run)}`);
    const badges = node("p", null, "detail-badges"); badges.append(badge(label(run.state)), badge(`health: ${healthOf(run)}`)); if (run.inferred) badges.append(badge("Inferred from source evidence", "inferred")); host.append(badges);
    field(host, "Original task", run.task);
    field(host, "Current / last activity", run.activity);
    if (run.ai_summary) field(host, "DS4F activity brief (not execution status)", run.ai_summary.text);
    field(host, "Final summary", run.final_summary || run.summary);
    const evidence = run.evidence;
    field(host, "Evidence", typeof evidence === "string" ? evidence : evidence?.label || evidence?.kind || (run.inferred ? "Observed source activity; state is inferred, not a live process guarantee." : "Application-reported events."));
    if (Array.isArray(run.warnings) && run.warnings.length) field(host, "Delivery / ordering warnings", run.warnings.join(" · "));
    field(host, "Run identity", run.run_id);
    field(host, run.mode === "observation" ? "Observer collection" : "Session", run.session_id);
    const operations = run.operations || [];
    host.append(node("h3", `Operations (${operations.length})`));
    if (!operations.length) host.append(node("p", "No operation events reported.", "muted"));
    operations.forEach((op) => { const line = node("div", null, "operation"); line.append(node("strong", op.label || op.name || op.operation_id || "Operation"), node("span", ` ${label(op.kind || "tool")} · ${label(op.pending ? "in progress" : op.outcome || op.state || op.status || "unknown")}`)); if (op.detail) line.append(node("p", op.detail)); host.append(line); });
    const timelineHeader = node("div", null, "timeline-header"); timelineHeader.append(node("h3", "Event history"));
    const reload = node("button", "Reload history"); reload.type = "button"; reload.dataset.focusKey = "reload-history"; reload.addEventListener("click", () => loadEvents(true)); timelineHeader.append(reload); host.append(timelineHeader);
    const timeline = node("ol", null, "timeline"); timeline.id = "timeline";
    state.events.forEach((event) => {
      const item = node("li"); const stamp = time(event.received_at || event.occurred_at); item.append(node("span", stamp ? new Date(stamp).toLocaleTimeString() : "Unknown time", "event-time"), node("strong", label(event.type || "event")));
      const payload = event.payload || {};
      // Only display safe selected labels, never raw arguments, results, paths or headers.
      const detail = payload.text || payload.activity || payload.summary || payload.detail || payload.reason || payload.task || payload.label || payload.name || payload.state || payload.outcome;
      if (detail) item.append(node("p", detail));
      if (event.late || event.delayed || event.sequence_gap || event.applied === false) item.append(badge(event.sequence_gap ? "sequence gap" : "late / not applied", "inferred"));
      timeline.append(item);
    });
    host.append(timeline);
    if (!state.events.length) host.append(node("p", state.eventBusy ? "Loading event history…" : "No events in the available history.", "muted"));
    if (state.more) { const more = node("button", state.eventBusy ? "Loading…" : "Load more events"); more.id = "load-more"; more.dataset.focusKey = "load-more"; more.type = "button"; more.disabled = state.eventBusy; more.addEventListener("click", () => loadEvents(false)); host.append(more); }
    if (focusedKey) [...host.querySelectorAll("[data-focus-key]")].find((el) => el.dataset.focusKey === focusedKey)?.focus({ preventScroll: true });
    host.scrollTop = previousScroll;
  }
  async function selectRun(id) { state.selected = id; state.events = []; state.nextCursor = 0; state.more = false; renderTree(); await loadEvents(true); }
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
      if (!state.live) setConnection("Snapshot loaded · reconnecting live stream", false);
    } catch (error) { showError(error.message || "Cannot reach the local collector."); setConnection("Disconnected · retrying", false); }
    finally { state.fetching = false; $("refresh").disabled = false; }
  }
  function renderFreshness() {
    if (!state.snapshot) return;
    $("freshness").textContent = `Snapshot ${age(state.lastSnapshotAt)} ago${state.snapshot.truncated ? ` · Showing ${state.snapshot.runs.length} of ${state.snapshot.total_runs} retained runs` : ""} · Work state and telemetry health are separate. “Observed” means local file evidence, not a guaranteed live process.`;
  }
  ["source-filter", "account-filter", "state-filter", "recency-filter"].forEach((id) => $(id)?.addEventListener("change", () => { renderProfiles(); renderTree(); }));
  $("search").addEventListener("input", renderTree);
  $("refresh").addEventListener("click", refresh);
  $("expand-all").addEventListener("click", () => { state.collapsed.clear(); if (state.snapshot) renderTree(); });
  refresh();
  const stream = new EventSource("/api/events?after=0");
  stream.onopen = () => { state.live = true; setConnection("Live connection", true); };
  stream.onerror = () => { state.live = false; setConnection("Live stream disconnected · retrying", false); };
  ["snapshot", "reset"].forEach((name) => stream.addEventListener(name, (event) => {
    try { applySnapshot(JSON.parse(event.data), name === "reset"); state.live = true; setConnection(name === "reset" ? "Live connection · history reset" : "Live connection", true); }
    catch (_) { showError("Received an invalid update. Refresh to recover."); }
  }));
  setInterval(() => {
    renderFreshness(); updateClocks();
    if (state.snapshot) {
      const healthKey = JSON.stringify(state.snapshot.runs.map(healthOf));
      const visibleKey = JSON.stringify(state.snapshot.runs.filter(matches).map((run) => run.run_id));
      if (healthKey !== state.healthKey || visibleKey !== state.visibleKey) { renderOverview(); renderProfiles(); renderTree(); }
    }
    if (!state.live || Date.now() - state.lastSnapshotAt > 5000) refresh();
  }, 2000);
})();
