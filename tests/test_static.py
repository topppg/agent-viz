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
                "detail",
                "profiles",
                "recency-filter",
                "source-filter",
                "account-filter",
                "state-filter",
                "connection",
                "demo-banner",
                "refresh",
                "source-errors",
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
    def test_browser_renders_tree_filters_and_private_text_safely(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let document;
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.value = ''; this.hidden = false; this._text = ''; this.options = []; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this._text = ''; if (this.tagName === 'select') this.options = [...items]; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  add(option) { this.options.push(option); this.children.push(option); }
  focus() { document.activeElement = this; }
  querySelectorAll() { return flatten(this).filter(x => x.dataset.focusKey); }
}
function flatten(el) { return el.children.flatMap(c => [c, ...flatten(c)]); }
const ids = Object.fromEntries(['connection','refresh','demo-banner','error','source-errors','overview','profiles','ai-brief','recency-filter','source-filter','account-filter','state-filter','search','expand-all','freshness','run-count','runs','detail'].map(id => [id, new Element(id.includes('filter') ? 'select' : 'div')]));
document = { getElementById: id => ids[id], createElement: tag => new Element(tag), activeElement: null };
const now = new Date().toISOString();
const run = (id, parent, name, source, account) => ({run_id:id,parent_run_id:parent,name,session_id:'s',source,account,state:'working',health:'observed',inferred:true,task:'Original task',activity:'<img src=x onerror=alert(1)>',last_seen:now,started_at:now,demo:true,operations:[]});
const snapshot = { stream_id:'journal-a',cursor:1, runs:[run('child','root','Child','codex','work'), run('root',null,'Root','codex','work'), run('grandchild','child','Nested','claude','personal'),run('orphan','missing','Orphan','claude','work')],profiles:[{id:'codex-work',kind:'codex',label:'work'},{id:'claude-personal',kind:'claude',label:'personal'},{id:'claude-work',kind:'claude',label:'work'},{id:'codex-empty',kind:'codex',label:'Empty profile'}],source_errors:{unavailable:'Source unavailable'},server_time:now };
let stream;
class EventSource { constructor() { this.listeners = {}; stream = this; } addEventListener(name, fn) { this.listeners[name] = fn; } }
class Option extends Element { constructor(text,value) { super('option'); this.textContent = text; this.value = value; } }
const requests = [];
const context = {document,Option,EventSource,Date,Map,Set,Boolean,Number,String,Array,JSON,console,encodeURIComponent,setInterval:() => {},fetch:async url => { requests.push(url); return {ok:true,json:async()=>url.startsWith('/api/runs/') ? {events:[{type:'run.activity',payload:{text:'<script>not executable</script>'},received_at:now}],next_cursor:1,has_more:false} : snapshot}; }};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const tick = () => new Promise(resolve => setImmediate(resolve));
(async()=>{
 await tick();
 assert.equal(ids['demo-banner'].hidden,false);
 assert.ok(ids.runs.textContent.includes('<img src=x onerror=alert(1)>'));
 assert.ok(!flatten(ids.runs).some(x => x.tagName === 'img'));
 assert.ok(ids.runs.textContent.includes('Parent not yet available'));
 assert.equal(flatten(ids.runs).filter(x => x.tagName === 'article').length,4);
 assert.equal(flatten(ids.profiles).filter(x => x.tagName === 'button').length,4);
 assert.ok(ids.profiles.textContent.includes('Empty profile'));
 assert.ok(ids.profiles.textContent.includes('Configured · no observations yet'));
 const originalOption = ids['source-filter'].options[1];
 const originalRoot = ids.runs.children[0];
 stream.listeners.snapshot({data:JSON.stringify(snapshot)});
 assert.equal(ids['source-filter'].options[1],originalOption);
 assert.equal(ids.runs.children[0],originalRoot);
 const active = run('live-root',null,'Live branch','codex','work');
 active.health='fresh'; active.ai_summary={text:'Checking an invented patch',stale:false};
 stream.listeners.snapshot({data:JSON.stringify({...snapshot,runs:[...snapshot.runs.map(r=>({...r,state:'idle',health:'stale'})),active],ai:{enabled:true,status:'ready',calls:1,max_calls:10,actual_cost_usd:.0001,budget_usd:.05,summaries:[{run_id:'live-root',text:'Checking an invented patch',generated_at:now}]}})});
 assert.ok(flatten(ids.runs).find(x => x.tagName==='article').textContent.includes('Live branch'));
 assert.ok(ids.runs.textContent.includes('Checking an invented patch'));
 assert.ok(ids['ai-brief'].textContent.includes('$0.000100 reported'));
 assert.ok(ids['ai-brief'].textContent.includes('bounded redacted context'));
 stream.listeners.snapshot({data:JSON.stringify({...snapshot,runs:[{...active,state:'completed'}],ai:{enabled:true,status:'ready',summaries:[{run_id:'live-root',text:'No longer running',generated_at:now}]}})});
 assert.ok(!ids['ai-brief'].textContent.includes('No longer running'));
 const firstProfile={...run('profile-a',null,'First profile','codex','same'),session_id:'a:observed'};
 const secondProfile={...run('profile-b',null,'Second profile','codex','same'),session_id:'b:observed'};
 stream.listeners.snapshot({data:JSON.stringify({...snapshot,runs:[firstProfile,secondProfile],profiles:[{id:'a',kind:'codex',label:'same'},{id:'b',kind:'codex',label:'same'}]})});
 ids['account-filter'].value='profile:b'; ids['account-filter'].listeners.change();
 assert.ok(ids.runs.textContent.includes('Second profile'));
 assert.ok(!ids.runs.textContent.includes('First profile'));
 ids['account-filter'].value=''; ids['account-filter'].listeners.change();
 stream.listeners.snapshot({data:JSON.stringify(snapshot)});
 const old = {...snapshot,runs:snapshot.runs.map(r=>({...r,last_seen:'2020-01-01T00:00:00Z'}))};
 stream.listeners.snapshot({data:JSON.stringify(old)});
 ids['recency-filter'].value='5m'; ids['recency-filter'].listeners.change();
 assert.ok(ids.runs.textContent.includes('No runs match'));
 ids['recency-filter'].value=''; ids['recency-filter'].listeners.change();
 stream.listeners.snapshot({data:JSON.stringify(snapshot)});
 ids['source-filter'].value='claude'; ids['source-filter'].listeners.change();
 assert.ok(ids.runs.textContent.includes('parent context'));
 const rootButton = flatten(ids.runs).find(x => x.dataset.focusKey === 'select:grandchild');
 rootButton.listeners.click(); await tick();
 assert.ok(ids.detail.textContent.includes('Original task'));
 assert.ok(ids.detail.textContent.includes('<script>not executable</script>'));
 assert.ok(ids.detail.textContent.includes('claude · personal'));
 assert.ok(flatten(ids.detail).some(x => x.id === 'detail-heading'));
 assert.ok(!flatten(ids.detail).some(x => x.tagName === 'script'));
 stream.onerror(); assert.ok(ids.connection.textContent.includes('disconnected'));
 stream.listeners.snapshot({data:JSON.stringify(snapshot)}); assert.equal(ids.connection.textContent,'Live connection');
 assert.ok(requests.some(x => x === '/api/runs/grandchild/events?after=0&limit=100'));
 stream.listeners.reset({data:JSON.stringify({...snapshot,cursor:0})}); await tick();
 assert.equal(requests.filter(x => x === '/api/runs/grandchild/events?after=0&limit=100').length,2);
 assert.ok(ids.connection.textContent.includes('history reset'));
 assert.equal(flatten(ids.detail).filter(x => x.tagName === 'li').length,1);
 stream.listeners.snapshot({data:JSON.stringify({...snapshot,stream_id:'journal-b',cursor:100})}); await tick();
 assert.equal(requests.filter(x => x === '/api/runs/grandchild/events?after=0&limit=100').length,3);
 assert.equal(flatten(ids.detail).filter(x => x.tagName === 'li').length,1);
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
