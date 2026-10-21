"""Summary settings use explicit consent and never persist browser credentials."""

import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "spiritboard" / "static"

FIXTURE = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let document;
class Element {
  constructor(tag) {
    this.tagName=tag; this.children=[]; this.listeners={}; this.dataset={}; this.attrs={};
    this._text=''; this.value=''; this.checked=false; this.disabled=false;
    this.hidden=false; this.open=false;
  }
  append(...items) { this.children.push(...items); }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() {
    return this._text+this.children.map(c => typeof c==='string' ? c : c.textContent).join('');
  }
  setAttribute(key,value) { this.attrs[key]=value; }
  addEventListener(name,fn) { this.listeners[name]=fn; }
  focus() { document.activeElement=this; }
  showModal() { this.open=true; }
  close() { this.open=false; this.listeners.close?.(); }
}
const button=new Element('button'), body=new Element('body');
document={body, activeElement:null,
  getElementById:id=>id==='summary-settings-open' ? button : null,
  createElement:tag=>new Element(tag)};
const posts=[],requests=[],popups=[],timers=new Map(); let nextTimer=1, fail=false, blocked=false, authURL='https://openrouter.ai/auth?code_challenge=synthetic';
let state={enabled:false,active_provider:null,preferred_provider:'openrouter',
  policy:'preferred-first',demo:false,providers:{
  openrouter:{available:true,connected:false,pending:false,error:null,
    model:'deepseek/deepseek-v4-flash'},
  codex:{available:true,connected:false,pending:false,error:null,
    model:'gpt-6-luna',effort:'medium'},
  anthropic:{available:true,connected:false,pending:false,error:null,model:'claude-sonnet-synthetic'},
  openai:{available:true,connected:false,pending:false,error:null,model:'gpt-synthetic'}}};
const context={document,URL,
  setTimeout:fn=>{ const id=nextTimer++; timers.set(id,fn); return id; },
  clearTimeout:id=>timers.delete(id),
  window:{open:(url,target)=>{
    if(blocked) return null;
    const popup={opener:{},location:{href:url},closed:false,close(){this.closed=true;}};
    popups.push(popup); return popup;
  }},
  fetch:async(url,options)=>{
  requests.push(url);
  if(options.method==='POST') {
    const data=JSON.parse(options.body); posts.push({url,options,data});
    if(fail) return {ok:false,json:async()=>({error:'sensitive detail'})};
    if(url.endsWith('/connect')) {
      const name=url.includes('/openrouter/')?'openrouter':'codex';
      state.providers[name].pending=true;
      return {ok:true,json:async()=>({auth_url:authURL})};
    }
    if(url.endsWith('/key')) {
      const name=url.split('/')[3]; state.providers[name].connected=true;
      state.preferred_provider=name;
    }
    if(url.endsWith('/provider')) state.preferred_provider=data.provider;
    if(url.endsWith('/enabled')) state.enabled=data.enabled;
    if(url.endsWith('/disconnect')) {
      state.providers[data.provider].connected=false;
      state.providers[data.provider].pending=false; state.providers[data.provider].error=null;
    }
  }
  return {ok:true,json:async()=>structuredClone(state)};
}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const flatten=node=>node.children.filter(c=>typeof c!=='string').flatMap(c=>[c,...flatten(c)]);
const all=()=>flatten(body), find=fn=>all().find(fn), dialog=()=>find(n=>n.tagName==='dialog');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const click=async node=>{ node.listeners.click?.({preventDefault(){}}); await tick(); };
const change=async node=>{ node.listeners.change?.(); await tick(); };
const open=()=>click(button);
const consent=()=>find(n=>n.tagName==='input'&&n.type==='checkbox');
const toggle=()=>all().filter(n=>n.tagName==='input'&&n.type==='checkbox')[1];
const connect=name=>find(n=>n.dataset.summaryConnect===name);
const disconnect=name=>find(n=>n.dataset.summaryDisconnect===name);
const selector=()=>find(n=>n.tagName==='select');
const select=async name=>{selector().value=name; await change(selector());};
const key=(name='openrouter')=>find(n=>n.id===`summary-${name}-key`);
const submit=async(name='openrouter')=>{
  find(n=>n.dataset.summaryKey===name).listeners.submit({preventDefault(){}}); await tick();
};
"""


@unittest.skipUnless(shutil.which("node"), "Node is optional; summary settings DOM checks skipped")
class SummarySettingsTests(unittest.TestCase):
    def run_browser(self, assertions: str) -> None:
        result = subprocess.run(
            [
                "node",
                "-e",
                FIXTURE + "\n(async()=>{\n" + assertions + "\n})();",
                str(STATIC / "summary-settings.js"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_opt_in_connection_and_exact_provider_models(self) -> None:
        self.run_browser(r"""
assert.equal(requests.length,0);
await open();
assert.ok(dialog().open);
assert.equal(connect('openrouter').disabled,true);
assert.equal(connect('codex').disabled,true);
assert.equal(toggle().disabled,true);
assert.ok(body.textContent.includes('DeepSeek V4 Flash'));
assert.ok(body.textContent.includes('GPT-6 Luna · medium'));
assert.equal(connect('codex').textContent,'Continue with ChatGPT');
assert.ok(body.textContent.includes('short excerpts'));
assert.ok(body.textContent.includes('OpenRouter credits'));
assert.ok(body.textContent.includes('Plus or Pro'));
consent().checked=true; await change(consent());
await click(connect('openrouter'));
assert.equal(posts.length,1);
assert.equal(posts[0].url,'/api/summaries/openrouter/connect');
assert.equal(posts[0].options.headers['X-Spiritboard'],'summaries');
assert.equal(posts[0].options.credentials,'same-origin');
assert.equal(popups[0].opener,null);
assert.ok(popups[0].location.href.startsWith('https://openrouter.ai/auth?'));
assert.equal(disconnect('openrouter').textContent,'Cancel sign-in');
assert.ok(body.textContent.includes('Waiting for sign-in'));
assert.equal(connect('codex').disabled,true);
assert.equal(key().disabled,true);
await click(disconnect('openrouter'));
assert.equal(posts.at(-1).data.provider,'openrouter');
assert.equal(connect('openrouter').hidden,false);
""")

    def test_remote_revocation_warning_is_fixed_and_visible(self) -> None:
        self.run_browser(r"""
state.providers.codex.revocation_unconfirmed=true;
state.providers.codex.error='upstream-private-detail';
await open();
assert.ok(body.textContent.includes('Signed out here'));
assert.ok(body.textContent.includes('could not confirm'));
assert.ok(!body.textContent.includes('upstream-private-detail'));
const link=find(n=>n.tagName==='a' && n.href==='https://chatgpt.com/#settings');
assert.ok(link); assert.equal(link.rel,'noopener noreferrer');
""")

    def test_secure_storage_failures_are_clear_without_showing_provider_details(self) -> None:
        self.run_browser(r"""
state.providers.openrouter.storage_error=true;
state.providers.codex.storage_error=true;
state.providers.openrouter.error='private-backend-detail';
await open();
assert.ok(body.textContent.includes('Secure storage is unavailable'));
assert.ok(body.textContent.includes('Set up or unlock your system credential store'));
assert.ok(!body.textContent.includes('private-backend-detail'));
assert.equal(connect('openrouter').disabled,true);
consent().checked=true; await change(consent());
assert.equal(connect('openrouter').disabled,false);
""")

    def test_main_view_is_short_and_advanced_options_start_collapsed(self) -> None:
        self.run_browser(r"""
await open();
const details=all().filter(n=>n.tagName==='details');
assert.equal(details.length,2);
assert.ok(details.every(n=>!n.open));
assert.ok(details.some(n=>n.textContent.includes('Use an API key instead')));
assert.ok(details.some(n=>n.textContent.includes('How it works')));
const visible=n=>{
  if(typeof n==='string') return n;
  if(n.hidden) return '';
  const children=n.tagName==='details'&&!n.open
    ? n.children.filter(c=>c.tagName==='summary') : n.children;
  return n._text+children.map(visible).join(' ');
};
const text=visible(dialog());
assert.ok(text.includes('private work'));
assert.ok(text.includes('OpenRouter credits'));
assert.ok(!text.includes('Plus or Pro'));
assert.ok(!text.includes('DeepSeek V4 Flash'));
assert.ok(!text.includes('GPT-6 Luna'));
assert.ok(!text.includes('Or paste an OpenRouter API key'));
assert.ok(text.split(/\s+/).length<130);
details.forEach(n=>n.open=true);
key().value='synthetic-unsaved'; dialog().close();
assert.ok(details.every(n=>!n.open));
assert.equal(key().value,'');
""")

    def test_keys_clear_on_submit_and_close_and_errors_never_render_raw_data(self) -> None:
        self.run_browser(r"""
await open();
key().value='synthetic-no-consent'; await submit();
assert.equal(key().value,''); assert.equal(posts.length,0);
consent().checked=true; await change(consent());
key().value='synthetic-private-key'; await submit();
assert.equal(key().value,'');
assert.equal(posts[0].data.key,'synthetic-private-key');
assert.ok(!body.textContent.includes('synthetic-private-key'));
assert.equal(toggle().disabled,false);
toggle().checked=true; await change(toggle());
assert.equal(posts.at(-1).url,'/api/summaries/enabled');
assert.equal(posts.at(-1).data.enabled,true);
fail=true; key().value='second-synthetic-key'; await submit();
assert.equal(key().value,'');
assert.ok(body.textContent.includes('could not be completed'));
assert.ok(!body.textContent.includes('sensitive detail'));
key().value='unsubmitted'; dialog().close();
assert.equal(key().value,''); assert.equal(consent().checked,false);
assert.equal(timers.size,0); assert.equal(document.activeElement,button);
""")

    def test_polling_only_open_and_no_implicit_login_or_disconnect(self) -> None:
        self.run_browser(r"""
await open();
assert.equal(timers.size,1); assert.equal(posts.length,0);
state.providers.codex.connected=true; state.enabled=true; state.active_provider='codex';
const [id,poll]=[...timers.entries()][0]; timers.delete(id); await poll();
assert.ok(body.textContent.includes('Summaries enabled · ChatGPT'));
assert.equal(disconnect('codex').hidden,false);
assert.equal(posts.length,0);
dialog().close(); assert.equal(timers.size,0); assert.equal(posts.length,0);
assert.equal(popups.length,0);
""")

    def test_demo_disables_all_mutations_and_missing_sign_in_is_explained(self) -> None:
        self.run_browser(r"""
state.providers.codex.available=false;
await open();
assert.equal(connect('codex').disabled,true);
assert.ok(body.textContent.includes('ChatGPT sign-in is unavailable'));
dialog().close(); state.demo=true; await open();
assert.ok(body.textContent.includes('Synthetic demo'));
assert.equal(consent().disabled,true); assert.equal(key().disabled,true);
assert.equal(toggle().disabled,true); assert.equal(connect('openrouter').disabled,true);
consent().checked=true; await click(connect('openrouter'));
key().value='synthetic'; await submit();
assert.equal(posts.length,0); assert.equal(popups.length,0);
""")

    def test_external_login_url_is_validated_and_popup_failures_are_safe(self) -> None:
        self.run_browser(r"""
await open(); consent().checked=true; await change(consent());
blocked=true; await click(connect('openrouter'));
assert.equal(posts.length,0); assert.ok(body.textContent.includes('Allow pop-up'));
blocked=false; authURL='https://openrouter.ai.evil.test/auth';
await click(connect('openrouter'));
assert.equal(popups[0].closed,true);
assert.ok(body.textContent.includes('could not be completed'));
assert.ok(!body.textContent.includes('evil.test'));
await click(disconnect('openrouter'));
authURL='https://auth.openai.com/oauth/authorize?synthetic=1';
await click(connect('codex'));
assert.equal(popups[1].location.href,authURL);
assert.equal(popups[1].opener,null);
""")

    def test_provider_errors_are_fixed_copy_and_remote_markup_is_not_injected(self) -> None:
        self.run_browser(r"""
state.providers.codex.error='<img src=x onerror=secret>';
state.providers.openrouter.model='<script>sensitive</script>';
await open();
assert.ok(body.textContent.includes('Connection needs attention'));
assert.ok(!body.textContent.includes('<img'));
assert.ok(!body.textContent.includes('<script>'));
""")

    def test_provider_selector_uses_preference_but_polling_preserves_user_choice(self) -> None:
        self.run_browser(r"""
state.preferred_provider='anthropic';
await open();
assert.equal(selector().value,'anthropic');
const sections=()=>all().filter(n=>n.tagName==='section');
assert.equal(sections().filter(n=>!n.hidden).length,1);
assert.ok(!find(n=>n.attrs['aria-labelledby']==='summary-provider-anthropic').hidden);
key('anthropic').value='synthetic-unsaved';
await select('openai');
assert.equal(key('anthropic').value,'');
key('openai').value='synthetic-still-typing';
const [id,poll]=[...timers.entries()][0]; timers.delete(id); await poll();
assert.equal(selector().value,'openai');
assert.equal(key('openai').value,'synthetic-still-typing');
assert.equal(sections().filter(n=>!n.hidden).length,1);
dialog().close();
assert.equal(key('openai').value,'');
await open(); assert.equal(selector().value,'anthropic');
""")

    def test_all_key_providers_require_consent_and_use_their_own_endpoint(self) -> None:
        self.run_browser(r"""
await open();
for(const name of ['openrouter','anthropic','openai']) {
  await select(name); key(name).value='synthetic-no-consent'; await submit(name);
  assert.equal(key(name).value,''); assert.equal(posts.length,0);
}
consent().checked=true; await change(consent());
for(const name of ['openrouter','anthropic','openai']) {
  await select(name); key(name).value=`synthetic-${name}`; await submit(name);
  assert.equal(key(name).value,'');
  assert.equal(posts.at(-1).url,`/api/summaries/${name}/key`);
  assert.equal(posts.at(-1).data.key,`synthetic-${name}`);
  assert.ok(!body.textContent.includes(`synthetic-${name}`));
}
assert.ok(body.textContent.includes('API use is billed separately'));
assert.ok(body.textContent.includes('Enterprise workspaces are not supported'));
""")

    def test_connected_provider_can_be_preferred_with_explicit_consent(self) -> None:
        self.run_browser(r"""
state.providers.anthropic.connected=true;
await open(); await select('anthropic');
const use=()=>find(n=>n.dataset.summaryUse==='anthropic');
assert.equal(use().hidden,false); assert.equal(use().disabled,true);
await click(use()); assert.equal(posts.length,0);
consent().checked=true; await change(consent()); await click(use());
assert.equal(posts.at(-1).url,'/api/summaries/provider');
assert.equal(posts.at(-1).data.provider,'anthropic');
assert.equal(use().disabled,true); assert.equal(use().textContent,'Preferred for summaries');
assert.ok(body.textContent.includes('Your preferred service is tried first'));
assert.ok(body.textContent.includes('other connected services'));
""")

    def test_pending_and_demo_disable_new_provider_actions(self) -> None:
        self.run_browser(r"""
state.providers.codex.pending=true;
await open(); consent().checked=true; await change(consent());
assert.equal(selector().disabled,true);
for(const name of ['openrouter','anthropic','openai']) {
  assert.equal(key(name).disabled,true);
  key(name).value='synthetic'; await submit(name);
}
assert.equal(posts.length,0);
dialog().close(); state.providers.codex.pending=false; state.demo=true;
await open(); consent().checked=true; await change(consent());
for(const name of ['openrouter','anthropic','openai']) {
  assert.equal(key(name).disabled,true);
  key(name).value='synthetic'; await submit(name);
}
assert.equal(selector().disabled,true); assert.equal(posts.length,0);
""")

    def test_any_setting_change_clears_unsaved_keys(self) -> None:
        self.run_browser(r"""
state.providers.anthropic.connected=true;
await open();
key('openrouter').value='synthetic-unsaved';
toggle().checked=true; await change(toggle());
assert.equal(key('openrouter').value,'');
assert.equal(posts.at(-1).url,'/api/summaries/enabled');
assert.ok(!JSON.stringify(posts).includes('synthetic-unsaved'));
""")

    def test_reopening_a_pending_sign_in_keeps_cancel_reachable(self) -> None:
        self.run_browser(r"""
state.providers.codex.pending=true;
await open();
assert.equal(selector().value,'codex');
const section=find(n=>n.attrs['aria-labelledby']==='summary-provider-codex');
assert.equal(section.hidden,false);
assert.equal(disconnect('codex').hidden,false);
assert.equal(disconnect('codex').disabled,false);
await click(disconnect('codex'));
assert.equal(posts.at(-1).data.provider,'codex');
assert.equal(selector().disabled,false);
""")


class SummarySettingsAssetsTests(unittest.TestCase):
    def test_script_has_no_browser_secret_storage_or_html_injection(self) -> None:
        script = (STATIC / "summary-settings.js").read_text()
        for forbidden in (
            "localStorage",
            "sessionStorage",
            "document.cookie",
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "console.",
        ):
            self.assertNotIn(forbidden, script)
