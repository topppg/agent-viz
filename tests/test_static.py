"""Dependency-free browser asset checks; optional Node exercises DOM behavior."""

import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "spiritboard" / "static"


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
        self.assertEqual(
            set(parser.references),
            {
                "/favicon.svg",
                "/style.css",
                "/app.js",
                "/summary-settings.css",
                "/summary-settings.js",
            },
        )
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
                "past-bar",
                "zoom",
                "axis",
                "more",
                "top-actions",
                "keys-toggle",
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
    this.classList = { toggle: (c, force) => { const on = force ?? !(this.className || '').includes(c); this.className = on ? c : ''; } }; }
  getAttribute(key) { return this.attrs[key]; }
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
const ids = Object.fromEntries(['connection','refresh','demo-banner','error','source-errors','counts','ai-status','harnesses','flux','overview','views','harness-filter','window-filter','window-label','search','hidden-note','runs','past-bar','zoom','flux-window','flux-note','axis','more','top-actions','keys-toggle','keys'].map(id => [id, new Element(id.endsWith('filter') ? 'select' : 'div')]));
const headers = Object.fromEntries(['harness','status','agent','doing','rate','seen'].map(key => { const button = new Element('button'); button.dataset.sort = key; button.textContent = key; return [key, button]; }));
ids['window-filter'].value = '604800000';
['active','waiting','all'].forEach(v => { const b = new Element('button'); b.dataset.view = v; ids.views.children.push(b); });
['600','3600'].forEach(v => { const b = new Element('button'); b.dataset.zoom = v; ids.zoom.children.push(b); });
const docListeners = {};
document = { getElementById: id => ids[id], createElement: tag => new Element(tag), querySelectorAll: selector => selector === '[data-sort]' ? Object.values(headers) : [], activeElement: null, addEventListener: (k, fn) => { docListeners[k] = fn; } };
const now = new Date().toISOString();
const old = '2020-01-01T00:00:00Z';
const run = (id, parent, name, extra = {}) => ({run_id:id,parent_run_id:parent,name,session_id:'codex-work:observed',source:'codex',account:'work',state:'working',health:'observed',inferred:true,task:'Original task',activity:'<img src=x onerror=alert(1)>',last_seen:now,started_at:now,demo:true,operations:[],...extra});
const runs = [run('child','root','Child'), run('root',null,'Root'), run('ask','root','Asker',{state:'waiting_user',ai_summary:{text:'Tuning retry limits',title:'Retry tuning',origin:'claude-recap',stale:false}}),
  run('orphan','missing','Orphan',{session_id:'claude-personal:observed',source:'claude',account:'personal'}),
  run('done',null,'Finished',{state:'completed',summary:'All good',health:'unknown'}), run('ancient',null,'Ancient',{last_seen:old,health:'stale'}),
  {...run('sdk-run',null,'SDK worker'),session_id:'s1',source:'sdk',account:'demo',inferred:false,health:'fresh'}];
const snapshot = { stream_id:'journal-a', cursor:1, runs, server_time:now, stale_after_seconds:15,
  profiles:[{id:'codex-work',kind:'codex',label:'work'},{id:'claude-personal',kind:'claude',label:'personal'},{id:'codex-empty',kind:'codex',label:'Empty profile'},{id:'grokbot-mac',kind:'grokbot',label:'Grok Bot'},{id:'dsh-home',kind:'dsh',label:'DeepSeek Harness'}],
  source_errors:{unavailable:'Source unavailable'}, ai:{enabled:true,status:'ready',calls:2,actual_cost_usd:.0021,known_cost_usd:.0021,unknown_cost_calls:0,summaries:[]},
  activity:{bucket_seconds:10,buckets:60,end:now,runs:{root:[...new Array(59).fill(0),40],'sdk-run':[...new Array(54).fill(0),1,1,1,1,1,2]}} };
const pastIso = new Date(Date.parse(now) - 290000).toISOString();
const past = {...snapshot, at:pastIso, server_time:pastIso, runs:[run('orphan','missing','Orphan',{session_id:'claude-personal:observed',source:'claude',account:'personal',last_seen:pastIso,live:null,activity:'Earlier step'})]};
let stream;
class EventSource { constructor() { this.listeners = {}; stream = this; } addEventListener(name, fn) { this.listeners[name] = fn; } }
class Option extends Element { constructor(text,value) { super('option'); this.textContent = text; this.value = value; } }
const requests = [], posts = [], copied = [];
let renameStatus = 200;
const targets = {orphan:{available:true,kind:'focus',label:'cmux'}, ask:{available:true,kind:'resume',label:'resume',command:'claude --resume invented-0000'}};
const navigator = {clipboard:{writeText:async text => { copied.push(text); }}};
const context = {document,Option,EventSource,navigator,setInterval:() => {},console,fetch:async (url, options = {}) => {
  if (options.method === 'POST') {
    posts.push(options.body ? {url, headers:options.headers, body:JSON.parse(options.body)} : {url, headers:options.headers});
    if (url.endsWith('/name')) return renameStatus === 200 ? {ok:true,json:async()=>({ok:true,local_name:JSON.parse(options.body).name.trim() || null})} : {ok:false,status:renameStatus};
    return {ok:true,json:async()=>({ok:true})};
  }
  requests.push(url);
  if (url.endsWith('/focus')) {
    return {ok:true,json:async()=>targets[decodeURIComponent(url.split('/')[3])] || {available:false,kind:'unsupported',label:''}};
  }
  if (url.startsWith('/api/snapshot?at=')) return {ok:true,json:async()=>past};
  if (url.startsWith('/api/activity?window=')) return {ok:true,json:async()=>({bucket_seconds:60,buckets:60,end:now,runs:{root:[...new Array(59).fill(0),5]}})};
  return {ok:true,json:async()=>url.startsWith('/api/runs/') ? {events:[{type:'run.activity',payload:{text:'<script>not executable</script>'},received_at:now}],next_cursor:1,has_more:false} : snapshot}; }};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const tick = () => new Promise(resolve => setImmediate(resolve));
const rows = () => ids.runs.children.filter(x => typeof x !== 'string' && x.dataset.runId);
const expandOf = (r) => flatten(r).find(x => (x.className || '').includes('expand'));
const nameButton = (r) => flatten(r).find(x => (x.className || '').includes('rename'));
const editor = () => flatten(ids.runs).find(x => x.className === 'name-edit');
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
 assert.ok(asker.textContent.includes('Tuning retry limits') && !asker.textContent.includes('✦'));
 assert.ok(flatten(asker).some(x => (x.className || '').includes('doing needs')));
 assert.ok(rows().find(r => r.dataset.runId === 'root').textContent.includes('40'));
 // Harnesses: only those with something running; idle ones are counted, the dominant one flagged noisy.
 const harnessButtons = ids.harnesses.children.filter(x => x.tagName === 'button');
 assert.equal(harnessButtons.length, 3);
 assert.ok(!ids.harnesses.textContent.includes('Empty profile'));
 // Labels that already name the tool are not prefixed with its kind.
 assert.ok([...ids['harness-filter'].children].some(o => o.textContent === 'Grok Bot'));
 assert.ok(![...ids['harness-filter'].children].some(o => /grokbot ·|dsh ·/.test(o.textContent)));
 assert.ok(ids.harnesses.textContent.includes('3 idle harnesses · pick one in the harness filter'));
 assert.ok(harnessButtons.find(b => b.textContent.includes('Codex · work')).textContent.includes('▲ noisy'));
 assert.ok(ids.flux.children.some(x => x.className === 'col'));
 assert.ok(ids.flux.attrs['aria-label'].includes('Codex · work'));
 assert.ok(ids['ai-status'].textContent.includes('$0.0021 spent · 2 API calls'));
 assert.ok(ids.counts.textContent.includes('1 needs you'));
 // Header sorting reverses on a second click and keeps children with their parent.
 headers.agent.listeners.click();
 assert.deepEqual(rows().map(r => r.dataset.runId), ['orphan','root','child','ask','sdk-run']);
 assert.equal(headers.agent.attrs['aria-pressed'], 'true');
 assert.equal(headers.agent.dataset.arrow, '▲');
 headers.agent.listeners.click();
 assert.deepEqual(rows().map(r => r.dataset.runId), ['sdk-run','root','ask','child','orphan']);
 assert.equal(headers.agent.dataset.arrow, '▼');
 headers.rate.listeners.click();
 assert.equal(rows()[0].dataset.runId, 'root');
 headers.seen.listeners.click();
 assert.equal(headers.seen.dataset.arrow, '▼');
 headers.doing.listeners.click();
 assert.equal(headers.doing.attrs['aria-pressed'], 'true');
 headers.status.listeners.click();
 assert.deepEqual(rows().map(r => r.dataset.runId), ['root','ask','child','orphan','sdk-run']);
 headers.harness.listeners.click();
 assert.equal(rows()[0].dataset.runId, 'orphan');
 headers.status.listeners.click();
 headers.status.listeners.click();
 headers.status.listeners.click();
 assert.equal(headers.status.attrs['aria-pressed'], 'false');
 assert.deepEqual(rows().map(r => r.dataset.runId), ['root','ask','child','orphan','sdk-run']);
 // Harness filter.
 harnessButtons.find(b => b.textContent.includes('Claude · personal')).listeners.click();
 assert.deepEqual(rows().map(r => r.dataset.runId), ['orphan']);
 ids['harness-filter'].value = ''; ids['harness-filter'].listeners.change();
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
 expandOf(rows()[1]).focus();
 stream.listeners.snapshot({data:JSON.stringify(snapshot)});
 assert.equal(document.activeElement, expandOf(rows()[1]));
 // An identical snapshot keeps the same row elements, so clicks survive live ticks.
 const stableRow = rows()[0];
 stream.listeners.snapshot({data:JSON.stringify(snapshot)});
 assert.equal(rows()[0], stableRow);
 // Clicking a row opens its details inline, directly under it.
 const detail = () => flatten(ids.runs).find(x => x.id === 'detail');
 const expanded = () => rows().find(r => expandOf(r).attrs['aria-expanded'] === 'true')?.dataset.runId;
 rows().find(r => r.dataset.runId === 'orphan').listeners.click(); await tick();
 assert.equal(expanded(), 'orphan');
 const after = ids.runs.children.indexOf(rows().find(r => r.dataset.runId === 'orphan')) + 1;
 assert.equal(ids.runs.children[after], detail());
 assert.ok(detail().textContent.includes('Original task'));
 assert.ok(detail().textContent.includes('Claude · personal'));
 assert.ok(detail().textContent.includes('not yet available'));
 assert.ok(requests.includes('/api/runs/orphan/events?after=0&limit=100'));
 // A live session offers to open its app; the POST carries the custom header.
 const control = (key) => flatten(detail()).find(x => x.dataset.focusKey === key);
 assert.equal(control('open-app').textContent, '↗ open in cmux');
 // The open button sits in the title row, beside the session name.
 const head = flatten(detail()).find(x => x.className === 'detail-head');
 assert.ok(flatten(head).includes(control('open-app')));
 assert.ok(flatten(head).some(x => x.tagName === 'h3'));
 control('open-app').listeners.click(); await tick();
 assert.deepEqual(posts, [{url:'/api/runs/orphan/focus', headers:{'X-Spiritboard':'focus'}}]);
 assert.ok(detail().textContent.includes('opened'));
 stream.listeners.snapshot({data:JSON.stringify(snapshot)}); await tick();
 assert.equal(requests.filter(x => x === '/api/runs/orphan/focus').length, 1);
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
 // A stopped session offers a resume command to copy, never to run.
 assert.equal(control('open-app'), undefined);
 assert.equal(control('copy-resume').textContent, '⧉ copy resume command');
 assert.equal(control('copy-resume').title, 'claude --resume invented-0000');
 control('copy-resume').listeners.click(); await tick();
 assert.deepEqual(copied, ['claude --resume invented-0000']);
 assert.ok(detail().textContent.includes('copied'));
 navigator.clipboard.writeText = async () => { throw new Error('denied'); };
 control('copy-resume').listeners.click(); await tick();
 assert.equal(control('resume-command').value, 'claude --resume invented-0000');
 assert.equal(document.activeElement, control('resume-command'));
 // The target is re-checked before copying; a session that went live is never resumed.
 targets.ask = {available:true,kind:'focus',label:'iTerm'};
 control('copy-resume').listeners.click(); await tick();
 assert.equal(copied.length, 1);
 assert.equal(control('open-app').textContent, '↗ open in iTerm');
 assert.ok(detail().textContent.includes('session is live'));
 assert.equal(posts.length, 1);
 flatten(detail()).find(x => x.dataset.focusKey === 'parent:root').listeners.click(); await tick();
 assert.equal(expanded(), 'root');
 assert.equal(control('open-app'), undefined);
 assert.equal(control('copy-resume'), undefined);
 // Clicking the open row again closes it; keyboard navigation and Escape.
 rows().find(r => r.dataset.runId === 'root').listeners.click(); await tick();
 assert.equal(detail(), undefined);
 docListeners.keydown({key:'j', target:{tagName:'BODY'}, preventDefault(){}}); await tick();
 assert.equal(expanded(), 'root');
 docListeners.keydown({key:'j', target:{tagName:'BODY'}, preventDefault(){}}); await tick();
 assert.equal(expanded(), 'ask');
 docListeners.keydown({key:'Escape', target:{tagName:'BODY'}, preventDefault(){}});
 assert.equal(detail(), undefined);
 // The header menu closes on Escape without closing the open agent, and focus returns to its button.
 rows().find(r => r.dataset.runId === 'root').listeners.click(); await tick();
 ids.more.listeners.click();
 assert.equal(ids.more.getAttribute('aria-expanded'), 'true');
 docListeners.keydown({key:'Escape', target:ids.refresh, preventDefault(){}});
 assert.equal(ids.more.getAttribute('aria-expanded'), 'false');
 assert.equal(document.activeElement, ids.more);
 assert.equal(expanded(), 'root');
 // Choosing an action closes the menu without stranding focus on a hidden button.
 ids.more.listeners.click();
 ids['top-actions'].listeners.click({target:{closest: () => ids.refresh}});
 assert.equal(ids.more.getAttribute('aria-expanded'), 'false');
 assert.equal(document.activeElement, ids.more);
 docListeners.keydown({key:'Escape', target:{tagName:'BODY'}, preventDefault(){}});
 assert.equal(detail(), undefined);
 // An open session keeps the list order still; closing it restores live sorting.
 const order = rows().map(r => r.dataset.runId);
 rows().find(r => r.dataset.runId === 'sdk-run').listeners.click(); await tick();
 const later = new Date(Date.parse(now) + 1000).toISOString();
 stream.listeners.snapshot({data:JSON.stringify({...snapshot, runs:runs.map(r => r.run_id === 'sdk-run' ? {...r, state:'waiting_user', last_seen:later} : r)})}); await tick();
 assert.deepEqual(rows().map(r => r.dataset.runId), order);
 docListeners.keydown({key:'Escape', target:{tagName:'BODY'}, preventDefault(){}});
 assert.equal(rows()[0].dataset.runId, 'sdk-run');
 stream.listeners.snapshot({data:JSON.stringify(snapshot)}); await tick();
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
 const replied = run('replied', null, 'Replied', {state:'idle', activity:'Fixed it; the suite passes.'});
 stream.listeners.snapshot({data:JSON.stringify({...snapshot, runs:[...snapshot.runs, turn, replied]})}); await tick();
 assert.ok(rows().find(r => r.dataset.runId === 'replied').textContent.includes('Fixed it; the suite passes.'));
 const turnRow = rows().find(r => r.dataset.runId === 'turn');
 assert.ok(turnRow.textContent.includes('↩ your turn~'));
 assert.ok(turnRow.textContent.includes('Original task'));
 assert.ok(ids.counts.textContent.includes('3 needs you'));
 turnRow.listeners.click(); await tick();
 assert.ok(detail().textContent.includes('waiting for your next direction'));
 // Letting a turn rest calms it until the agent acts again.
 assert.equal(control('rest').textContent, '☾ let it rest');
 control('rest').listeners.click(); await tick();
 assert.ok(rows().find(r => r.dataset.runId === 'turn').textContent.includes('☾ at rest~'));
 assert.ok(detail().textContent.includes('you checked this turn'));
 assert.equal(control('rest').textContent, '↩ back to your turn');
 assert.ok(ids.counts.textContent.includes('2 needs you') && ids.counts.textContent.includes('1 at rest'));
 docListeners.keydown({key:'w', target:{tagName:'BODY'}, preventDefault(){}}); await tick();
 assert.ok(!rows().some(r => r.dataset.runId === 'turn'));
 docListeners.keydown({key:'a', target:{tagName:'BODY'}, preventDefault(){}}); await tick();
 docListeners.keydown({key:'r', target:{tagName:'BODY'}, preventDefault(){}}); await tick();
 assert.ok(rows().find(r => r.dataset.runId === 'turn').textContent.includes('↩ your turn~'));
 docListeners.keydown({key:'r', target:{tagName:'BODY'}, preventDefault(){}}); await tick();
 const stirred = {...turn, last_seen:new Date(Date.parse(now) + 1000).toISOString()};
 stream.listeners.snapshot({data:JSON.stringify({...snapshot, runs:[...snapshot.runs, stirred, replied]})}); await tick();
 assert.ok(rows().find(r => r.dataset.runId === 'turn').textContent.includes('↩ your turn~'));
 // Checked liveness: no owning process means closed, never "your turn"; an open idle session stays visible.
 const gone = run('gone', null, 'Gone', {state:'idle', live:false, activity:'Wrapped up the release'});
 const hours = new Date(Date.now() - 2 * 3600000).toISOString();
 const open = run('open', null, 'Open', {state:'idle', live:true, last_seen:hours});
 stream.listeners.snapshot({data:JSON.stringify({...snapshot, runs:[...snapshot.runs, gone, open]})}); await tick();
 const goneRow = rows().find(r => r.dataset.runId === 'gone');
 assert.ok(goneRow.textContent.includes('■ closed'));
 assert.ok(goneRow.textContent.includes('last said: Wrapped up the release'));
 assert.ok(ids.counts.textContent.includes('1 needs you'));
 assert.equal(rows().find(r => r.dataset.runId === 'open'), undefined);
 ids.views.children[2].listeners.click();
 assert.ok(rows().find(r => r.dataset.runId === 'open').textContent.includes('open · idle'));
 ids.views.children[0].listeners.click();
 goneRow.listeners.click(); await tick();
 assert.ok(detail().textContent.includes('no running process owns this session'));
 // A user-named Claude desktop session is tagged; an archived one is never active; resume says terminal.
 const mine = {session_id:'claude-personal:observed',source:'claude',account:'personal',app:'Claude desktop'};
 const desk = run('desk', null, 'Desktop title', {...mine,title_source:'user',ai_summary:{title:'Old generated title',text:'Investigating retries'}});
 const shelved = run('shelved', null, 'Shelved', {...mine, archived:true, state:'idle'});
 const evolved = run('evolved', null, 'native-slug', {ai_summary:{title:'Ship retry budget',previous_title:'Tune retry limits',titled_at:now,text:'Shipping it'}});
 targets.desk = {available:true,kind:'resume',label:'resume',command:'claude --resume invented-1111'};
 stream.listeners.snapshot({data:JSON.stringify({...snapshot, runs:[...snapshot.runs, desk, shelved, evolved]})}); await tick();
 const deskRow = rows().find(r => r.dataset.runId === 'desk');
 assert.ok(deskRow.textContent.includes('Desktop title'));
 assert.ok(!deskRow.textContent.includes('Old generated title'));
 assert.equal(nameButton(deskRow).title, 'Click to rename');
 // An evolving AI title wins over the native name and keeps the title it replaced findable.
 const evolvedRow = rows().find(r => r.dataset.runId === 'evolved');
 assert.ok(evolvedRow.textContent.includes('Ship retry budget'));
 assert.equal(nameButton(evolvedRow).title, 'AI title · native: native-slug · Renamed from “Tune retry limits” · Click to rename');
 ids.search.value = 'tune retry'; ids.search.listeners.input(); await tick();
 assert.ok(rows().some(r => r.dataset.runId === 'evolved'));
 ids.search.value = ''; ids.search.listeners.input(); await tick();
 rows().find(r => r.dataset.runId === 'evolved').listeners.click(); await tick();
 assert.ok(flatten(detail()).some(x => x.className === 'was' && x.textContent === 'was: Tune retry limits'));
 rows().find(r => r.dataset.runId === 'evolved').listeners.click(); await tick();
 assert.ok(flatten(deskRow).some(x => x.className === 'note' && x.textContent === 'Claude desktop'));
 assert.equal(flatten(deskRow).find(x => (x.className || '').includes('harness-cell')).title, 'Claude · personal · Claude desktop');
 assert.equal(rows().find(r => r.dataset.runId === 'shelved'), undefined);
 assert.ok(ids.counts.textContent.includes('1 needs you'));
 deskRow.listeners.click(); await tick();
 assert.ok(detail().textContent.includes('Claude desktop'));
 assert.ok(detail().textContent.includes('Desktop title'));
 assert.equal(control('copy-resume').textContent, '⧉ copy terminal resume command');
 assert.ok(control('copy-resume').title.startsWith('Resumes in a terminal, not in Claude desktop'));
 ids.views.children[2].listeners.click();
 const shelvedRow = rows().find(r => r.dataset.runId === 'shelved');
 assert.ok(shelvedRow.textContent.includes('■ archived'));
 shelvedRow.listeners.click(); await tick();
 assert.ok(detail().textContent.includes('archived in Claude desktop'));
 ids.views.children[0].listeners.click();
 // Zoom asks the server for a wider window; the chart keeps its place.
 ids.zoom.children[1].listeners.click(); await tick();
 assert.ok(requests.includes('/api/activity?window=3600'));
 assert.equal(ids['flux-window'].textContent, 'last hour');
 assert.equal(ids.zoom.children[1].attrs['aria-pressed'], 'true');
 assert.ok(ids.axis.textContent.includes('-1h'));
 ids.zoom.children[0].listeners.click(); await tick();
 assert.equal(ids['flux-window'].textContent, 'last 10 min');
 // Clicking a bar shows the board at that moment, paused, with open buttons disabled.
 rows().find(r => r.dataset.runId === 'orphan').listeners.click(); await tick();
 assert.equal(control('open-app').disabled, undefined);
 assert.equal(ids['past-bar'].hidden, true);
 const cols = () => ids.flux.children.filter(x => x.className === 'col');
 cols()[30].listeners.click(); await tick();
 const asked = requests.find(x => x.startsWith('/api/snapshot?at='));
 assert.ok(Math.abs(Number(asked.split('=')[1]) * 1000 - (Date.parse(now) - 290000)) < 1000);
 assert.equal(ids['past-bar'].hidden, false);
 assert.ok(ids['past-bar'].textContent.startsWith('Viewing '));
 assert.ok(ids['past-bar'].textContent.includes('4m ago'));
 assert.ok(ids['past-bar'].textContent.includes('⟲ back to live'));
 assert.equal(ids.connection.textContent, 'past · not live');
 assert.ok(ids.counts.children.some(c => c.className === 'counts-at'));
 assert.deepEqual(rows().map(r => r.dataset.runId), ['orphan']);
 assert.ok(rows()[0].textContent.includes('Earlier step'));
 assert.equal(control('open-app').disabled, true);
 assert.ok(ids.flux.children.some(x => x.className === 'marker'));
 control('open-app').listeners.click(); await tick();
 assert.equal(posts.length, 1);
 // Live updates keep the chart current but leave the past table alone.
 stream.listeners.snapshot({data:JSON.stringify(snapshot)}); await tick();
 assert.deepEqual(rows().map(r => r.dataset.runId), ['orphan']);
 assert.equal(ids['past-bar'].hidden, false);
 flatten(ids['past-bar']).find(x => x.tagName === 'button').listeners.click(); await tick();
 assert.equal(ids['past-bar'].hidden, true);
 assert.ok(rows().length > 1);
 assert.equal(ids.connection.textContent, 'live');
 assert.ok(!ids.counts.children.some(c => c.className === 'counts-at'));
 assert.equal(control('open-app').disabled, undefined);
 // L returns to live, and so does the newest bar.
 cols()[10].listeners.click(); await tick();
 assert.equal(ids['past-bar'].hidden, false);
 docListeners.keydown({key:'L', target:{tagName:'BODY'}, preventDefault(){}}); await tick();
 assert.equal(ids['past-bar'].hidden, true);
 cols()[10].listeners.click(); await tick();
 cols()[59].listeners.click(); await tick();
 assert.equal(ids['past-bar'].hidden, true);
 assert.ok(rows().length > 1);
 // Renaming: the name is its own button beside the row's expand handle, never nested in it.
 docListeners.keydown({key:'Escape', target:{tagName:'BODY'}, preventDefault(){}});
 stream.listeners.snapshot({data:JSON.stringify(snapshot)}); await tick();
 const rootRow = () => rows().find(r => r.dataset.runId === 'root');
 assert.equal(rootRow().tagName, 'div');
 assert.equal(expandOf(rootRow()).tagName, 'button');
 assert.equal(expandOf(rootRow()).dataset.focusKey, 'select:root');
 assert.ok(!flatten(ids.runs).some(x => x.tagName === 'button' && flatten(x).some(y => y.tagName === 'button')));
 assert.equal(nameButton(rootRow()).textContent, 'Root');
 assert.equal(nameButton(rootRow()).title, 'Click to rename');
 // Clicking the name edits it in place, selected, without expanding the row.
 nameButton(rootRow()).listeners.click({stopPropagation(){}});
 assert.equal(expanded(), undefined);
 assert.equal(editor().value, 'Root');
 assert.equal(editor().maxLength, 120);
 assert.equal(editor().attrs['aria-label'], 'Agent name');
 assert.equal(document.activeElement, editor());
 // Shortcuts stay quiet while typing, and a live tick keeps the draft in the same input.
 const field = editor();
 field.value = 'Release train'; field.listeners.input();
 docListeners.keydown({key:'j', target:field, preventDefault(){ throw new Error('shortcut fired while typing'); }});
 assert.equal(expanded(), undefined);
 stream.listeners.snapshot({data:JSON.stringify({...snapshot, runs:runs.map(r => r.run_id === 'root' ? {...r, activity:'Still going'} : r)})}); await tick();
 assert.equal(editor(), field);
 assert.equal(field.value, 'Release train');
 // Enter saves: one POST with the custom header, the new name shows at once with a marker.
 const renamesBefore = posts.length;
 field.listeners.keydown({key:'Enter', preventDefault(){}, stopPropagation(){}});
 assert.deepEqual(posts.slice(renamesBefore), [{url:'/api/runs/root/name', headers:{'X-Spiritboard':'rename','Content-Type':'application/json'}, body:{name:'Release train'}}]);
 assert.equal(editor(), undefined);
 assert.equal(nameButton(rootRow()).textContent, 'Release train');
 assert.equal(document.activeElement, nameButton(rootRow()));
 assert.equal(nameButton(rootRow()).title, 'Your name · automatic: Root');
 assert.ok(flatten(rootRow()).some(x => x.className === 'renamed-mark'));
 await tick();
 // The saved name arrives with the next snapshot; search finds it.
 const renamedRuns = runs.map(r => r.run_id === 'root' ? {...r, local_name:'Release train'} : r);
 stream.listeners.snapshot({data:JSON.stringify({...snapshot, runs:renamedRuns})}); await tick();
 ids.search.value = 'release train'; ids.search.listeners.input();
 assert.deepEqual(rows().map(r => r.dataset.runId), ['root']);
 ids.search.value = ''; ids.search.listeners.input();
 // The heading says it was renamed here; Escape cancels an edit there without saving.
 rootRow().listeners.click(); await tick();
 assert.equal(expanded(), 'root');
 const headingOf = () => flatten(detail()).find(x => x.tagName === 'h3');
 assert.ok(flatten(detail()).find(x => (x.className || '').includes('renamed-note')).textContent.startsWith('Renamed by you in Spiritboard · automatic title: Root'));
 assert.ok(!flatten(detail()).some(x => x.className === 'was'));
 nameButton(headingOf()).listeners.click({stopPropagation(){}});
 assert.equal(editor().dataset.focusKey, 'edit:detail:root');
 editor().value = 'Something else';
 editor().listeners.keydown({key:'Escape', preventDefault(){}, stopPropagation(){}});
 assert.equal(posts.length, renamesBefore + 1);
 assert.equal(headingOf().textContent, 'Release train');
 assert.equal(document.activeElement, nameButton(headingOf()));
 // "Use automatic title" clears the local name.
 control('auto-name').listeners.click(); await tick();
 assert.deepEqual(posts.at(-1).body, {name:''});
 assert.equal(nameButton(rootRow()).textContent, 'Root');
 assert.ok(!flatten(rootRow()).some(x => x.className === 'renamed-mark'));
 assert.equal(control('auto-name'), undefined);
 // Saving an empty name clears it too; saving the unchanged automatic name sends nothing.
 stream.listeners.snapshot({data:JSON.stringify({...snapshot, runs:renamedRuns})}); await tick();
 nameButton(rootRow()).listeners.click({stopPropagation(){}});
 editor().value = '   '; editor().listeners.blur(); await tick();
 assert.equal(posts.length, renamesBefore + 3);
 assert.deepEqual(posts.at(-1).body, {name:''});
 stream.listeners.snapshot({data:JSON.stringify(snapshot)}); await tick();
 nameButton(rootRow()).listeners.click({stopPropagation(){}});
 editor().listeners.keydown({key:'Enter', preventDefault(){}, stopPropagation(){}}); await tick();
 assert.equal(posts.length, renamesBefore + 3);
 // A refused rename restores the previous name and says why.
 renameStatus = 400;
 nameButton(rootRow()).listeners.click({stopPropagation(){}});
 editor().value = 'Bad name'; editor().listeners.keydown({key:'Enter', preventDefault(){}, stopPropagation(){}});
 assert.equal(nameButton(rootRow()).textContent, 'Bad name');
 await tick();
 assert.equal(nameButton(rootRow()).textContent, 'Root');
 assert.equal(ids.error.hidden, false);
 assert.ok(ids.error.textContent.includes('that name is not allowed'));
 renameStatus = 200;
 // The past board is read-only: names are plain text there.
 docListeners.keydown({key:'Escape', target:{tagName:'BODY'}, preventDefault(){}});
 cols()[10].listeners.click(); await tick();
 assert.equal(ids['past-bar'].hidden, false);
 assert.equal(nameButton(rows()[0]), undefined);
 assert.ok(flatten(rows()[0]).some(x => x.className === 'agent-name' && x.tagName === 'span'));
 docListeners.keydown({key:'L', target:{tagName:'BODY'}, preventDefault(){}}); await tick();
 assert.ok(nameButton(rootRow()));
 // Direct API usage has no dollar receipt; never call it free or ChatGPT plan usage.
 for (const provider of ['openai', 'anthropic']) {
   stream.listeners.snapshot({data:JSON.stringify({...snapshot,ai:{enabled:true,status:'ready',provider,api_calls:1,subscription_calls:0,known_cost_usd:0,actual_cost_usd:null,unknown_cost_calls:1}})});
   assert.ok(ids['ai-status'].textContent.includes(provider === 'openai' ? 'OpenAI API' : 'Anthropic'));
   assert.ok(ids['ai-status'].textContent.includes('cost unavailable'));
   assert.ok(!ids['ai-status'].textContent.includes('$0.0000 spent'));
   assert.ok(!ids['ai-status'].textContent.includes('plan calls'));
 }
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
