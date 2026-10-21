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
    element('p', 'Your preferred service is tried first, then other connected services if needed. Only services you connect can receive excerpts. Pause summaries or disconnect at any time.', 'summary-settings-help'),
    element('p', 'Models: DeepSeek V4 Flash on OpenRouter; GPT-6 Luna · medium on ChatGPT.', 'summary-settings-help'),
    guide,
  );
  const providers = {};
  const names = ['openrouter', 'codex', 'anthropic', 'openai'];
  const titles = { openrouter: 'OpenRouter', codex: 'ChatGPT', anthropic: 'Anthropic', openai: 'OpenAI API' };
  const pickerLabel = element('label', 'Provider', 'summary-settings-picker');
  const picker = element('select');
  picker.id = 'summary-provider';
  picker.disabled = true;
  pickerLabel.htmlFor = picker.id;
  for (const name of names) {
    const option = element('option', titles[name]);
    option.value = name;
    picker.append(option);
  }
  picker.value = 'openrouter';
  pickerLabel.append(picker);
  const apiModels = element('p', '', 'summary-settings-help');
  details.append(apiModels);

  for (const [name, title, description] of [
    ['openrouter', 'OpenRouter', 'Uses your OpenRouter credits.'],
    ['codex', 'ChatGPT', 'Uses an eligible ChatGPT Plus or Pro plan. Enterprise workspaces are not supported.'],
    ['anthropic', 'Anthropic', 'API use is billed separately from your Claude subscription.'],
    ['openai', 'OpenAI API', 'API use is billed separately from your ChatGPT subscription.'],
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
    const use = element('button', 'Use for summaries');
    use.type = 'button';
    use.dataset.summaryUse = name;
    use.hidden = true;
    if (name === 'openrouter' || name === 'codex') actions.append(connect);
    actions.append(use, disconnect);
    section.append(titleNode, status, descriptionNode, revocation, actions);
    const install = element('p', '', 'summary-settings-help');
    install.hidden = true;
    if (name === 'codex') {
      install.textContent = 'ChatGPT sign-in is unavailable in this installation. Check the local service setup.';
      section.append(install);
    }
    providers[name] = { section, status, connect, disconnect, install, revocation, use };
  }

  for (const name of ['openrouter', 'anthropic', 'openai']) {
    const keyForm = element('form', '', 'summary-settings-key');
    keyForm.dataset.summaryKey = name;
    const keyLabel = element('label', `Paste an ${titles[name]} API key`);
    const key = element('input');
    key.id = `summary-${name}-key`;
    key.type = 'password';
    key.autocomplete = 'off';
    key.spellcheck = false;
    key.required = true;
    key.disabled = true;
    key.setAttribute('aria-describedby', `summary-${name}-key-help`);
    keyLabel.htmlFor = key.id;
    const saveKey = element('button', 'Save key securely');
    saveKey.type = 'submit';
    saveKey.disabled = true;
    const keyHelp = element('p', 'Saved in your system credential store.', 'summary-settings-help');
    keyHelp.id = `summary-${name}-key-help`;
    keyForm.append(keyLabel, key, saveKey, keyHelp);
    let keyOptions = null;
    if (name === 'openrouter') {
      keyOptions = element('details', '', 'summary-settings-details');
      keyOptions.append(element('summary', 'Use an API key instead'), keyForm);
      providers[name].section.append(keyOptions);
    } else {
      providers[name].section.append(keyForm);
    }
    Object.assign(providers[name], { keyForm, key, saveKey, keyOptions });
  }
  dialog.append(header, intro, notice, message, consentLabel, toggleLabel, pickerLabel, ...names.map(name => providers[name].section), details);
  document.body.append(dialog);

  let settings = null;
  let busy = false;
  let timer = null;
  let generation = 0;
  let pendingPopup = null;
  let selected = null;

  function clearKeys() {
    for (const nodes of Object.values(providers)) {
      if (nodes.key) nodes.key.value = '';
    }
  }

  function hasPending() {
    return Object.values(settings?.providers || {}).some(value => value.pending === true);
  }

  function canConnect(name) {
    return settings && settings.demo !== true && !busy && !hasPending() && consent.checked && settings.providers?.[name]?.available === true;
  }

  function showError(text) {
    message.textContent = text;
    message.hidden = !text;
  }

  function render() {
    if (!settings) return;
    const demo = settings.demo === true;
    notice.textContent = demo ? 'Synthetic demo: sign-in and AI summaries are off.' : settings.enabled ? `Summaries enabled · ${titles[settings.active_provider] || 'waiting for a connection'}` : 'Summaries paused.';
    consent.disabled = busy || demo;
    const pending = hasPending();
    picker.disabled = busy || demo || pending;
    if (selected === null) selected = names.find(name => settings.providers?.[name]?.pending === true) || (names.includes(settings.preferred_provider) ? settings.preferred_provider : 'openrouter');
    picker.value = selected;
    apiModels.textContent = ['anthropic', 'openai'].flatMap(name => {
      const model = settings.providers?.[name]?.model;
      return typeof model === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,100}$/.test(model) ? [`${titles[name]}: ${model}`] : [];
    }).join(' · ');
    apiModels.hidden = !apiModels.textContent;
    let connected = false;
    for (const name of names) {
      const value = settings.providers?.[name] || {};
      const nodes = providers[name];
      const available = value.available === true;
      nodes.section.hidden = name !== selected;
      connected ||= value.connected === true;
      nodes.status.textContent = demo ? 'Disabled in demo' : value.storage_error === true ? 'Secure storage is unavailable. Set up or unlock your system credential store, then try again.' : value.pending ? 'Waiting for sign-in…' : value.error ? 'Connection needs attention. Disconnect and try again.' : value.connected ? 'Connected' : available ? 'Not connected' : 'Unavailable';
      nodes.status.dataset.state = value.connected ? 'connected' : value.pending ? 'pending' : 'disconnected';
      nodes.connect.hidden = value.connected === true || value.pending === true;
      nodes.connect.disabled = busy || demo || pending || !available || !consent.checked;
      nodes.use.hidden = value.connected !== true;
      nodes.use.textContent = settings.preferred_provider === name ? 'Preferred for summaries' : 'Use for summaries';
      nodes.use.disabled = !canConnect(name) || settings.preferred_provider === name;
      nodes.revocation.hidden = demo || name !== 'codex' || value.revocation_unconfirmed !== true;
      nodes.disconnect.hidden = !value.connected && !value.pending && !value.error;
      nodes.disconnect.textContent = value.pending ? 'Cancel sign-in' : 'Disconnect';
      nodes.disconnect.disabled = busy || demo;
      nodes.install.hidden = demo || available || name !== 'codex';
      if (nodes.key) {
        nodes.key.disabled = !canConnect(name);
        nodes.saveKey.disabled = !canConnect(name);
      }
    }
    toggle.checked = settings.enabled === true;
    toggle.disabled = busy || demo || (!connected && !toggle.checked);
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
    clearKeys();
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
    if (!canConnect(provider)) return;
    clearKeys();
    const popup = window.open('about:blank', '_blank');
    if (!popup) {
      showError('Allow pop-up windows for this local dashboard, then try connecting again.');
      return;
    }
    popup.opener = null;
    pendingPopup = popup;
    void change(`/api/summaries/${provider}/connect`, {}, popup, provider);
  }

  for (const name of names) {
    providers[name].connect.addEventListener('click', () => connect(name));
    providers[name].disconnect.addEventListener('click', () => {
      clearKeys();
      void change('/api/summaries/disconnect', { provider: name });
    });
    providers[name].use.addEventListener('click', () => {
      if (!canConnect(name) || settings.providers?.[name]?.connected !== true || settings.preferred_provider === name) return;
      clearKeys();
      void change('/api/summaries/provider', { provider: name });
    });
    if (providers[name].keyForm) providers[name].keyForm.addEventListener('submit', event => {
      event.preventDefault();
      const value = providers[name].key.value.trim();
      clearKeys();
      if (!value || !canConnect(name)) return;
      void change(`/api/summaries/${name}/key`, { key: value });
    });
  }
  picker.addEventListener('change', () => {
    clearKeys();
    if (!picker.disabled && names.includes(picker.value)) selected = picker.value;
    for (const nodes of Object.values(providers)) if (nodes.keyOptions) nodes.keyOptions.open = false;
    showError('');
    render();
  });
  consent.addEventListener('change', render);
  toggle.addEventListener('change', () => {
    void change('/api/summaries/enabled', { enabled: toggle.checked });
  });
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    generation += 1;
    clearTimeout(timer);
    clearKeys();
    for (const nodes of Object.values(providers)) if (nodes.keyOptions) nodes.keyOptions.open = false;
    selected = null;
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
