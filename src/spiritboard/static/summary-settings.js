(() => {
  'use strict';

  const opener = document.getElementById('summary-settings-open');
  if (!opener) return;

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  const dialog = element('dialog', '', 'summary-settings');
  dialog.setAttribute('aria-labelledby', 'summary-settings-title');
  dialog.setAttribute('aria-describedby', 'summary-settings-intro');
  const header = element('div', '', 'summary-settings-head');
  const heading = element('h2', 'AI summaries');
  heading.id = 'summary-settings-title';
  const close = element('button', 'Close');
  close.type = 'button';
  close.setAttribute('aria-label', 'Close summary settings');
  header.append(heading, close);
  const intro = element('p', 'See what your agents are working on, at a glance.', 'summary-settings-intro');
  intro.id = 'summary-settings-intro';
  const notice = element('p', '', 'summary-settings-notice');
  notice.setAttribute('role', 'status');
  notice.setAttribute('aria-live', 'polite');
  const message = element('p', '', 'summary-settings-error');
  message.setAttribute('role', 'alert');
  message.hidden = true;
  const consentLabel = element('label', '', 'summary-settings-consent');
  const consent = element('input');
  consent.type = 'checkbox';
  consent.disabled = true;
  consentLabel.append(consent, element('span', 'Share short excerpts of my sessions with connected services. These may include private work.'));
  const toggleLabel = element('label', '', 'summary-settings-toggle');
  const toggle = element('input');
  toggle.type = 'checkbox';
  toggle.disabled = true;
  toggleLabel.append(toggle, element('span', 'Enable AI summaries'));
  const details = element('details', '', 'summary-settings-details');
  const guide = element('a', 'Privacy & setup');
  guide.href = 'https://github.com/topppg/spiritboard/blob/main/docs/SUMMARIES.md';
  guide.target = '_blank';
  guide.rel = 'noopener noreferrer';
  details.append(
    element('summary', 'How it works'),
    element('p', 'Connect either service. If both are connected, OpenRouter is tried first. You can pause summaries or disconnect at any time.', 'summary-settings-help'),
    element('p', 'Models: DeepSeek V4 Flash on OpenRouter; GPT-6 Luna · medium on ChatGPT.', 'summary-settings-help'),
    guide,
  );
  const providers = {};

  for (const [name, title, description] of [
    ['openrouter', 'OpenRouter', 'Uses your OpenRouter credits.'],
    ['codex', 'ChatGPT', 'Uses your ChatGPT plan limits.'],
  ]) {
    const section = element('section', '', 'summary-settings-provider');
    section.setAttribute('aria-labelledby', `summary-provider-${name}`);
    const titleNode = element('h3', title);
    titleNode.id = `summary-provider-${name}`;
    const status = element('p', 'Not connected', 'summary-provider-status');
    status.setAttribute('aria-live', 'polite');
    const descriptionNode = element('p', description, 'summary-settings-help');
    const revocation = element('p', 'Signed out here, but we could not confirm sign-out in ChatGPT. Remove Spiritboard in ', 'summary-settings-error');
    const accountSettings = element('a', 'ChatGPT settings');
    accountSettings.href = 'https://chatgpt.com/#settings';
    accountSettings.target = '_blank';
    accountSettings.rel = 'noopener noreferrer';
    revocation.append(accountSettings);
    revocation.hidden = true;
    const actions = element('div', '', 'summary-settings-actions');
    const connect = element('button', name === 'codex' ? 'Continue with ChatGPT' : `Connect ${title}`);
    connect.type = 'button';
    connect.disabled = true;
    connect.dataset.summaryConnect = name;
    const disconnect = element('button', 'Disconnect');
    disconnect.type = 'button';
    disconnect.dataset.summaryDisconnect = name;
    disconnect.hidden = true;
    actions.append(connect, disconnect);
    section.append(titleNode, status, descriptionNode, revocation, actions);
    const install = element('p', '', 'summary-settings-help');
    install.hidden = true;
    if (name === 'codex') {
      install.textContent = 'ChatGPT sign-in is unavailable in this installation. Check the local service setup.';
      section.append(install);
    }
    providers[name] = { section, status, connect, disconnect, install, revocation };
  }

  const keyForm = element('form', '', 'summary-settings-key');
  const keyOptions = element('details', '', 'summary-settings-details');
  keyOptions.append(element('summary', 'Use an API key instead'), keyForm);
  const keyLabel = element('label', 'Or paste an OpenRouter API key');
  const key = element('input');
  key.id = 'summary-openrouter-key';
  key.type = 'password';
  key.autocomplete = 'off';
  key.spellcheck = false;
  key.required = true;
  key.disabled = true;
  key.setAttribute('aria-describedby', 'summary-key-help');
  keyLabel.htmlFor = key.id;
  const saveKey = element('button', 'Save key securely');
  saveKey.type = 'submit';
  saveKey.disabled = true;
  const keyHelp = element('p', 'Saved in your system credential store.', 'summary-settings-help');
  keyHelp.id = 'summary-key-help';
  keyForm.append(keyLabel, key, saveKey, keyHelp);
  providers.openrouter.section.append(keyOptions);
  dialog.append(header, intro, notice, message, consentLabel, toggleLabel, providers.openrouter.section, providers.codex.section, details);
  document.body.append(dialog);

  let settings = null;
  let busy = false;
  let timer = null;
  let generation = 0;
  let pendingPopup = null;

  function showError(text) {
    message.textContent = text;
    message.hidden = !text;
  }

  function render() {
    if (!settings) return;
    const demo = settings.demo === true;
    notice.textContent = demo ? 'Synthetic demo: sign-in and AI summaries are off.' : settings.enabled ? `Summaries enabled · ${settings.active_provider === 'openrouter' ? 'OpenRouter' : settings.active_provider === 'codex' ? 'ChatGPT' : 'waiting for a connection'}` : 'Summaries paused.';
    consent.disabled = busy || demo;
    const pending = Object.values(settings.providers || {}).some(value => value.pending === true);
    let connected = false;
    for (const name of ['openrouter', 'codex']) {
      const value = settings.providers?.[name] || {};
      const nodes = providers[name];
      const available = value.available === true;
      connected ||= value.connected === true;
      nodes.status.textContent = demo ? 'Disabled in demo' : value.storage_error === true ? 'Secure storage is unavailable. Set up or unlock your system credential store, then try again.' : value.pending ? 'Waiting for sign-in…' : value.error ? 'Connection needs attention. Disconnect and try again.' : value.connected ? 'Connected' : available ? 'Not connected' : 'Unavailable';
      nodes.status.dataset.state = value.connected ? 'connected' : value.pending ? 'pending' : 'disconnected';
      nodes.connect.hidden = value.connected === true || value.pending === true;
      nodes.connect.disabled = busy || demo || pending || !available || !consent.checked;
      nodes.revocation.hidden = demo || name !== 'codex' || value.revocation_unconfirmed !== true;
      nodes.disconnect.hidden = !value.connected && !value.pending && !value.error;
      nodes.disconnect.textContent = value.pending ? 'Cancel sign-in' : 'Disconnect';
      nodes.disconnect.disabled = busy || demo;
      nodes.install.hidden = demo || available || name !== 'codex';
    }
    toggle.checked = settings.enabled === true;
    toggle.disabled = busy || demo || (!connected && !toggle.checked);
    key.disabled = busy || demo || pending;
    saveKey.disabled = busy || demo || pending || !consent.checked;
  }

  async function request(path, data) {
    const response = await fetch(path, {
      method: data === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      ...(data === undefined ? {} : {
        headers: { 'Content-Type': 'application/json', 'X-Spiritboard': 'summaries' },
        body: JSON.stringify(data),
      }),
    });
    if (!response.ok) throw new Error('request failed');
    return response.json();
  }

  async function refresh() {
    const current = generation;
    try {
      const result = await request('/api/summaries/settings');
      if (!dialog.open || current !== generation) return;
      settings = result;
      render();
    } catch {
      if (dialog.open && current === generation) showError('Could not load summary settings. Close this panel and try again.');
    }
  }

  function schedule() {
    clearTimeout(timer);
    if (!dialog.open) return;
    timer = setTimeout(async () => {
      if (!busy) await refresh();
      schedule();
    }, 2000);
  }

  async function change(path, data, popup = null, provider = null) {
    if (busy || !settings || settings.demo) {
      if (popup) popup.close();
      return;
    }
    busy = true;
    const current = generation;
    showError('');
    render();
    try {
      const result = await request(path, data);
      if (!dialog.open || current !== generation) {
        if (popup) popup.close();
        return;
      }
      if (popup) {
        const url = new URL(result.auth_url);
        const allowed = provider === 'openrouter' ? url.origin === 'https://openrouter.ai' && (url.pathname === '/auth' || url.pathname.startsWith('/auth/')) : ['https://auth.openai.com', 'https://chatgpt.com'].includes(url.origin);
        if (!allowed || url.username || url.password || popup.closed) throw new Error('invalid login URL');
        popup.location.href = url.href;
      }
      await refresh();
    } catch {
      if (popup) popup.close();
      if (dialog.open && current === generation) showError('The action could not be completed. Check the local service and try again.');
    } finally {
      if (pendingPopup === popup) pendingPopup = null;
      busy = false;
      if (dialog.open) render();
    }
  }

  function connect(provider) {
    if (!consent.checked || !settings || settings.demo || busy) return;
    const popup = window.open('about:blank', '_blank');
    if (!popup) {
      showError('Allow pop-up windows for this local dashboard, then try connecting again.');
      return;
    }
    popup.opener = null;
    pendingPopup = popup;
    void change(`/api/summaries/${provider}/connect`, {}, popup, provider);
  }

  for (const name of ['openrouter', 'codex']) {
    providers[name].connect.addEventListener('click', () => connect(name));
    providers[name].disconnect.addEventListener('click', () => {
      void change('/api/summaries/disconnect', { provider: name });
    });
  }
  consent.addEventListener('change', render);
  toggle.addEventListener('change', () => {
    void change('/api/summaries/enabled', { enabled: toggle.checked });
  });
  keyForm.addEventListener('submit', event => {
    event.preventDefault();
    const value = key.value.trim();
    key.value = '';
    if (!value || !consent.checked || busy) return;
    void change('/api/summaries/openrouter/key', { key: value });
  });
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    generation += 1;
    clearTimeout(timer);
    key.value = '';
    keyOptions.open = false;
    details.open = false;
    consent.checked = false;
    if (pendingPopup) pendingPopup.close();
    pendingPopup = null;
    opener.focus();
  });
  opener.addEventListener('click', () => {
    if (dialog.open) return;
    generation += 1;
    showError('');
    notice.textContent = 'Loading summary settings…';
    dialog.showModal();
    void refresh();
    schedule();
  });
})();
