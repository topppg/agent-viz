"""Dependency-free browser asset checks; optional Node exercises DOM behavior."""

import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "agent_viz" / "static"


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.references = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "script":
            self.references.append(attrs.get("src"))
        if tag == "link":
            self.references.append(attrs.get("href"))


class StaticTests(unittest.TestCase):
    def test_assets_are_local_and_required_controls_exist(self):
        parser = Assets()
        parser.feed((STATIC / "index.html").read_text())
        self.assertEqual(set(parser.references), {"/style.css", "/app.js"})
        self.assertTrue(
            {
                "runs",
                "harnesses",
                "flux",
                "counts",
                "ai-status",
                "views",
                "harness-filter",
                "search",
                "connection",
                "demo-banner",
                "refresh",
                "source-errors",
                "error",
                "hidden-note",
                "overview",
                "usage",
                "heatmap",
                "usage-summary",
                "usage-legend",
            }
            <= parser.ids
        )
        script = (STATIC / "app.js").read_text()
        for forbidden in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write(",
            "eval(",
        ):
            self.assertNotIn(forbidden, script)

    @unittest.skipUnless(shutil.which("node"), "Node is optional; browser syntax/DOM check skipped")
    def test_browser_renders_table_harnesses_and_private_text_safely(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let document;
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.value = ''; this.hidden = false; this._text = ''; this.options = []; this.style = { setProperty(k, v) { this[k] = v; } }; this.attrs = {};
    this.classList = { toggle: (c) => { const on = !(this.className || '').includes(c); this.className = on ? c : ''; return on; } }; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => typeof c === 'string' ? c : c.textContent).join(''); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this._text = ''; if (this.tagName === 'select') this.options = [...items]; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  focus() { document.activeElement = this; }
  blur() {}
  querySelectorAll(selector) { const key = (selector.match(/^\[data-([a-z-]+)\]$/) || [])[1]; const prop = key ? key.replace(/-([a-z])/g, (_, c) => c.toUpperCase()) : 'focusKey'; return flatten(this).filter(x => x.dataset[prop] !== undefined); }
}
function flatten(el) { return el.children.filter(c => typeof c !== 'string').flatMap(c => [c, ...flatten(c)]); }
const ids = Object.fromEntries(['connection','refresh','demo-banner','error','source-errors','counts','ai-status','harnesses','flux','overview','usage','heatmap','usage-summary','usage-legend','views','harness-filter','window-filter','window-label','search','hidden-note','runs'].map(id => [id, new Element(id.endsWith('filter') ? 'select' : 'div')]));
ids['window-filter'].value = '604800000';
['active','waiting','all'].forEach(v => { const b = new Element('button'); b.dataset.view = v; ids.views.children.push(b); });
const docListeners = {};
document = { getElementById: id => ids[id], createElement: tag => new Element(tag), activeElement: null, addEventListener: (k, fn) => { docListeners[k] = fn; } };
const now = new Date().toISOString();
const old = '2020-01-01T00:00:00Z';
const pad2 = (n) => String(n).padStart(2, '0');
const ago = (n) => { const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - n); return d; };
const dk = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const spoken = (d) => `${['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.getDay()]} ${d.getDate()} ${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()]} ${d.getFullYear()}`;
const usage = { window_days: 365, limited: false, days: [
  { date: dk(ago(400)), requests: 99, sessions: 9, by_harness: { 'profile:codex-work': 99 }, sessions_by_harness: { 'profile:codex-work': 9 } },
  { date: dk(ago(6)), requests: 23, sessions: 4, by_harness: { 'profile:codex-work': 20, 'profile:claude-personal': 2, '<img src=x onerror=alert(1)>:demo': 1 }, sessions_by_harness: { 'profile:codex-work': 2, 'profile:claude-personal': 1, '<img src=x onerror=alert(1)>:demo': 1 } },
  { date: dk(ago(1)), requests: 0, sessions: 1, by_harness: {}, sessions_by_harness: { 'profile:codex-work': 1 } },
  { date: dk(ago(0)), requests: 2, sessions: 1, by_harness: { 'profile:codex-work': 2 }, sessions_by_harness: { 'profile:codex-work': 1 } }] };
const run = (id, parent, name, extra = {}) => ({run_id:id,parent_run_id:parent,name,session_id:'codex-work:observed',source:'codex',account:'work',state:'working',health:'observed',inferred:true,task:'Original task',activity:'<img src=x onerror=alert(1)>',last_seen:now,started_at:now,demo:true,operations:[],...extra});
const runs = [run('child','root','Child'), run('root',null,'Root'), run('ask','root','Asker',{state:'waiting_user',ai_summary:{text:'Tuning retry limits',title:'Retry tuning',origin:'claude-recap',stale:false}}),
  run('orphan','missing','Orphan',{session_id:'claude-personal:observed',source:'claude',account:'personal'}),
  run('done',null,'Finished',{state:'completed',summary:'All good',health:'unknown'}), run('ancient',null,'Ancient',{last_seen:old,health:'stale'}),
  {...run('sdk-run',null,'SDK worker'),session_id:'s1',source:'sdk',account:'demo',inferred:false,health:'fresh'}];
const snapshot = { stream_id:'journal-a', cursor:1, runs, server_time:now, stale_after_seconds:15,
  profiles:[{id:'codex-work',kind:'codex',label:'work'},{id:'claude-personal',kind:'claude',label:'personal'},{id:'codex-empty',kind:'codex',label:'Empty profile'}],
  source_errors:{unavailable:'Source unavailable'}, ai:{enabled:true,status:'ready',calls:2,actual_cost_usd:.0021,known_cost_usd:.0021,unknown_cost_calls:0,summaries:[]},
  activity:{bucket_seconds:10,buckets:60,end:now,runs:{root:[...new Array(59).fill(0),40],'sdk-run':[...new Array(54).fill(0),1,1,1,1,1,2]}}, usage };
let stream;
class EventSource { constructor() { this.listeners = {}; stream = this; } addEventListener(name, fn) { this.listeners[name] = fn; } }
class Option extends Element { constructor(text,value) { super('option'); this.textContent = text; this.value = value; } }
const requests = [];
const context = {document,Option,EventSource,setInterval:() => {},console,fetch:async url => { requests.push(url); return {ok:true,json:async()=>url.startsWith('/api/runs/') ? {events:[{type:'run.activity',payload:{text:'<script>not executable</script>'},received_at:now}],next_cursor:1,has_more:false} : snapshot}; }};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const tick = () => new Promise(resolve => setImmediate(resolve));
const rows = () => ids.runs.children.filter(x => typeof x !== 'string' && x.dataset.runId);
(async()=>{
 await tick();
 assert.equal(ids['demo-banner'].hidden,false);
 // Active view: finished and long-quiet runs are hidden; needs-you sorts first.
 assert.deepEqual(rows().map(r => r.dataset.runId), ['root','ask','child','orphan','sdk-run']);
 assert.ok(ids['hidden-note'].textContent.includes('1 finished · 1 older hidden'));
 assert.ok(ids.runs.textContent.includes('<img src=x onerror=alert(1)>'));
 assert.ok(!flatten(ids.runs).some(x => x.tagName === 'img'));
 assert.ok(ids.runs.textContent.includes('parent not yet available'));
 assert.ok(ids.runs.textContent.includes('├─ '));
 const asker = rows().find(r => r.dataset.runId === 'ask');
 assert.ok(asker.textContent.includes('needs you~'));
 assert.ok(asker.textContent.includes('Retry tuning'));
 assert.ok(asker.textContent.includes('✦ Tuning retry limits'));
 assert.ok(flatten(asker).some(x => (x.className || '').includes('doing needs')));
 assert.ok(rows().find(r => r.dataset.runId === 'root').textContent.includes('40'));
 // Harnesses: every profile listed, the dominant one flagged noisy.
 const harnessButtons = ids.harnesses.children.filter(x => x.tagName === 'button');
 assert.equal(harnessButtons.length, 4);
 assert.ok(ids.harnesses.textContent.includes('Empty profile'));
 assert.ok(ids.harnesses.textContent.includes('no observations yet'));
 assert.ok(harnessButtons.find(b => b.textContent.includes('codex · work')).textContent.includes('▲ noisy'));
 assert.ok(ids.flux.children.some(x => x.className === 'col'));
 assert.ok(ids.flux.attrs['aria-label'].includes('codex · work'));
 assert.ok(ids['ai-status'].textContent.includes('$0.0021 spent · 2 calls'));
 assert.ok(ids.counts.textContent.includes('1 needs you'));
 // Usage heatmap: one labelled cell per day in the 365-day window, text-only labels.
 const cells = () => flatten(ids.heatmap).filter(x => x.dataset.date);
 const cell = (n) => cells().find(x => x.dataset.date === dk(ago(n)));
 assert.equal(cells().length, 365);
 assert.equal(cell(400), undefined);
 assert.equal(cell(6).attrs['aria-label'], `${spoken(ago(6))} · 23 requests · 4 sessions`);
 assert.ok(cell(6).title.includes('codex · work: 20 requests'));
 assert.ok(cell(6).title.includes('<img src=x onerror=alert(1)>:demo: 1 request'));
 assert.ok(cell(6).className.includes('l4'));
 assert.ok(cell(1).className.includes('l0') && cell(1).className.includes('seen'));
 assert.equal(cell(2).attrs['aria-label'], `${spoken(ago(2))} · no activity`);
 assert.ok(!cell(2).className.includes('seen'));
 assert.ok(!flatten(ids.heatmap).some(x => ['img', 'script'].includes(x.tagName)));
 assert.ok(ids['usage-summary'].textContent.startsWith('25 requests · 2 active days'));
 assert.ok(flatten(ids.heatmap).some(x => x.className === 'wd' && x.textContent === 'Wed'));
 assert.ok(ids['usage-legend'].textContent.includes('less') && ids['usage-legend'].textContent.includes('more'));
 // Unchanged usage does not rebuild the grid on a live tick.
 const stableCell = cell(6);
 stream.listeners.snapshot({data:JSON.stringify(snapshot)});
 assert.equal(cell(6), stableCell);
 // Harness filter, including the heatmap counts.
 harnessButtons.find(b => b.textContent.includes('claude · personal')).listeners.click();
 assert.deepEqual(rows().map(r => r.dataset.runId), ['orphan']);
 assert.equal(cell(6).attrs['aria-label'], `${spoken(ago(6))} · 2 requests · 1 session`);
 assert.equal(cell(0).attrs['aria-label'], `${spoken(ago(0))} · no activity`);
 assert.ok(ids['usage-summary'].textContent.includes('claude · personal'));
 ids['harness-filter'].value = ''; ids['harness-filter'].listeners.change();
 assert.equal(cell(6).attrs['aria-label'], `${spoken(ago(6))} · 23 requests · 4 sessions`);
 // v hides both charts and brings them back.
 docListeners.keydown({key:'v', target:{tagName:'BODY'}, preventDefault(){}});
 assert.equal(ids.usage.hidden, true); assert.ok(ids.overview.className.includes('collapsed'));
 docListeners.keydown({key:'v', target:{tagName:'BODY'}, preventDefault(){}});
 assert.equal(ids.usage.hidden, false);
 // Views.
 ids.views.children[1].listeners.click();
 assert.deepEqual(rows().map(r => r.dataset.runId), ['root','ask']);
 assert.ok(rows()[0].className.includes('context'));
 ids.views.children[2].listeners.click();
 assert.equal(ids['window-label'].hidden, false);
 assert.equal(rows().length, 6);
 ids['window-filter'].value = ''; ids['window-filter'].listeners.change();
 assert.equal(rows().length, 7);
 ids['window-filter'].value = '604800000';
 ids.views.children[0].listeners.click();
 // Search keeps ancestors as context.
 ids.search.value = 'child'; ids.search.listeners.input();
 assert.deepEqual(rows().map(r => r.dataset.runId), ['root','child']);
 ids.search.value = ''; ids.search.listeners.input();
 // Re-render keeps focus on the same row.
 rows()[1].focus();
 stream.listeners.snapshot({data:JSON.stringify(snapshot)});
 assert.equal(document.activeElement.dataset.runId, rows()[1].dataset.runId);
 // An identical snapshot keeps the same row elements, so clicks survive live ticks.
 const stableRow = rows()[0];
 stream.listeners.snapshot({data:JSON.stringify(snapshot)});
 assert.equal(rows()[0], stableRow);
 // Clicking a row opens its details inline, directly under it.
 const detail = () => flatten(ids.runs).find(x => x.id === 'detail');
 const expanded = () => rows().find(r => r.attrs['aria-expanded'] === 'true')?.dataset.runId;
 rows().find(r => r.dataset.runId === 'orphan').listeners.click(); await tick();
 assert.equal(expanded(), 'orphan');
 const after = ids.runs.children.indexOf(rows().find(r => r.dataset.runId === 'orphan')) + 1;
 assert.equal(ids.runs.children[after], detail());
 assert.ok(detail().textContent.includes('Original task'));
 assert.ok(detail().textContent.includes('claude · personal'));
 assert.ok(detail().textContent.includes('not yet available'));
 assert.ok(requests.includes('/api/runs/orphan/events?after=0&limit=100'));
 // History is collapsed until asked for; it renders event text safely.
 assert.ok(!detail().textContent.includes('<script>not executable</script>'));
 flatten(detail()).find(x => x.dataset.focusKey === 'history').listeners.click(); await tick();
 assert.ok(detail().textContent.includes('<script>not executable</script>'));
 assert.ok(!flatten(detail()).some(x => x.tagName === 'script'));
 // A subagent opens from its own row; the parent link moves the open row back up.
 rows().find(r => r.dataset.runId === 'ask').listeners.click(); await tick();
 assert.equal(expanded(), 'ask');
 assert.ok(rows().find(r => r.dataset.runId === 'ask').className.includes(' sub'));
 assert.ok(!detail().textContent.includes('goal from'));
 rows().find(r => r.dataset.runId === 'child').listeners.click(); await tick();
 assert.ok(detail().textContent.includes('Original task'));
 assert.ok(!detail().textContent.includes('you asked'));
 rows().find(r => r.dataset.runId === 'ask').listeners.click(); await tick();
 assert.ok(detail().textContent.includes('waiting on you'));
 assert.ok(detail().textContent.includes('Tuning retry limits'));
 flatten(detail()).find(x => x.dataset.focusKey === 'parent:root').listeners.click(); await tick();
 assert.equal(expanded(), 'root');
 // Clicking the open row again closes it; keyboard navigation and Escape.
 rows().find(r => r.dataset.runId === 'root').listeners.click(); await tick();
 assert.equal(detail(), undefined);
 docListeners.keydown({key:'j', target:{tagName:'BODY'}, preventDefault(){}}); await tick();
 assert.equal(expanded(), 'root');
 docListeners.keydown({key:'j', target:{tagName:'BODY'}, preventDefault(){}}); await tick();
 assert.equal(expanded(), 'ask');
 docListeners.keydown({key:'Escape', target:{tagName:'BODY'}, preventDefault(){}});
 assert.equal(detail(), undefined);
 // Connection status and history reset.
 rows()[0].listeners.click(); await tick();
 const rootLoads = () => requests.filter(x => x === '/api/runs/root/events?after=0&limit=100').length;
 const before = rootLoads();
 stream.onerror(); assert.ok(ids.connection.textContent.includes('disconnected'));
 stream.listeners.snapshot({data:JSON.stringify(snapshot)}); assert.equal(ids.connection.textContent,'live');
 stream.listeners.reset({data:JSON.stringify({...snapshot,cursor:0})}); await tick();
 assert.ok(ids.connection.textContent.includes('history reset'));
 assert.equal(rootLoads(), before + 1);
 // A top-level session that finished its turn counts as needing you.
 const turn = run('turn', null, 'Turn ended', {state:'idle', activity:'Turn ended · session may resume'});
 stream.listeners.snapshot({data:JSON.stringify({...snapshot, runs:[...snapshot.runs, turn]})}); await tick();
 const turnRow = rows().find(r => r.dataset.runId === 'turn');
 assert.ok(turnRow.textContent.includes('↩ your turn~'));
 assert.ok(ids.counts.textContent.includes('2 needs you'));
 turnRow.listeners.click(); await tick();
 assert.ok(detail().textContent.includes('waiting for your next direction'));
 // Summaries off and empty chart.
 stream.listeners.snapshot({data:JSON.stringify({...snapshot,ai:{enabled:false},activity:{bucket_seconds:10,buckets:60,end:now,runs:{}}})});
 assert.ok(ids['ai-status'].textContent.includes('summaries off'));
 assert.ok(ids.flux.textContent.includes('No events in the last 10 minutes'));
})().catch(error => { console.error(error);process.exitCode=1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(STATIC / "app.js")],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
