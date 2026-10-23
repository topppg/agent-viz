// Mac app only: WebKit opens <select> as a legacy menu that CSS cannot style, so selects
// open this popover instead. The app injects it because it may attach to an older server.
(() => {
  const css = `
    select {
      appearance: none !important;
      border-radius: 8px !important;
      padding-right: 32px !important;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='12' fill='none' stroke='%239a93ad' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M1 4.5l3-3 3 3M1 7.5l3 3 3-3'/%3E%3C/svg%3E") !important;
      background-repeat: no-repeat !important;
      background-position: right 12px center !important;
      box-shadow: 0 1px 0 rgba(255,255,255,.06) inset, 0 1px 4px rgba(0,0,0,.18);
    }
    select:hover:not(:disabled), select[aria-expanded="true"] { border-color: var(--run) !important; }
    .mac-select-menu {
      position: fixed; z-index: 2147483647; box-sizing: border-box; margin: 0; padding: 5px;
      overflow-y: auto; overscroll-behavior: contain; list-style: none;
      border: 1px solid rgba(255,255,255,.1); border-radius: 10px;
      background: rgba(29,22,48,.86); -webkit-backdrop-filter: blur(24px) saturate(1.6); backdrop-filter: blur(24px) saturate(1.6);
      box-shadow: 0 0 0 .5px rgba(0,0,0,.6), 0 12px 32px rgba(0,0,0,.5), 0 2px 6px rgba(0,0,0,.3);
      color: var(--text, #ece8f6); font: 13px/1.3 var(--sans, -apple-system, system-ui, sans-serif);
      opacity: 0; transform: translateY(-3px) scale(.98); transform-origin: top center;
      transition: opacity .12s ease-out, transform .12s ease-out;
    }
    .mac-select-menu.is-above { transform-origin: bottom center; transform: translateY(3px) scale(.98); }
    .mac-select-menu.is-open { opacity: 1; transform: none; }
    .mac-select-menu li {
      display: flex; align-items: center; gap: 6px; min-height: 26px; padding: 3px 12px 3px 6px;
      border-radius: 6px; white-space: nowrap; cursor: default; user-select: none; -webkit-user-select: none;
    }
    .mac-select-menu li::before { content: ""; flex: none; width: 14px; height: 14px; }
    .mac-select-menu li[aria-selected="true"]::before {
      background: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' fill='none' stroke='%23b79cff' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3 7.4l2.6 2.6L11 4.2'/%3E%3C/svg%3E") center no-repeat;
    }
    .mac-select-menu li.is-active { background: rgba(183,156,255,.24); color: var(--bright, #f4f0ff); }
    .mac-select-menu li[aria-disabled="true"] { color: var(--dim, #6b6480); }
    .mac-select-menu li[hidden] { display: none; }
    @media (min-height: 601px) and (orientation: portrait) {
      .flux-panel { flex-direction: column; align-items: stretch; gap: 6px; }
      .flux-panel .panel-head { order: 0; }
      .flux-panel h2, .flux .ymax { display: block; }
      .axis { display: flex; }
      .flux { flex: none; height: clamp(160px, 22vh, 260px); }
    }
    @media (prefers-reduced-motion: reduce) { .mac-select-menu, .mac-select-menu.is-above { transition: none; transform: none; } }
  `;
  // The dashboard's CSP blocks <style> elements but not CSSOM. Older WebKit keeps native menus.
  if (!("adoptedStyleSheets" in document)) return;
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(css);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];

  let open = null;
  let typed = "";
  let typedAt = 0;

  const usable = (select) =>
    select instanceof HTMLSelectElement && !select.multiple && select.size <= 1 && select.options.length > 0;

  function setActive(index, scroll = true) {
    const items = open.list.children;
    items[open.active]?.classList.remove("is-active");
    open.active = index;
    const item = items[index];
    if (!item) return open.select.removeAttribute("aria-activedescendant");
    item.classList.add("is-active");
    open.select.setAttribute("aria-activedescendant", item.id);
    const list = open.list;
    if (!scroll) return;
    if (item.offsetTop < list.scrollTop) list.scrollTop = item.offsetTop - 5;
    else if (item.offsetTop + item.offsetHeight > list.scrollTop + list.clientHeight) {
      list.scrollTop = item.offsetTop + item.offsetHeight - list.clientHeight + 5;
    }
  }

  function step(from, delta) {
    const options = open.select.options;
    for (let index = from + delta; index >= 0 && index < options.length; index += delta) {
      if (!options[index].disabled && !options[index].hidden) return index;
    }
    return from;
  }

  function close(focus = true) {
    if (!open) return;
    const { select, list } = open;
    open = null;
    list.remove();
    select.setAttribute("aria-expanded", "false");
    select.removeAttribute("aria-activedescendant");
    select.removeAttribute("aria-controls");
    if (focus && select.isConnected) select.focus({ preventScroll: true });
  }

  function choose(index) {
    const select = open.select;
    close();
    const option = select.options[index];
    if (!option || option.disabled || select.disabled || index === select.selectedIndex) return;
    select.selectedIndex = index;
    select.dispatchEvent(new Event("input", { bubbles: true }));
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function show(select) {
    close(false);
    typed = "";
    typedAt = 0;
    const list = document.createElement("ul");
    list.className = "mac-select-menu";
    list.id = "mac-select-menu";
    list.setAttribute("role", "listbox");
    const label = select.labels?.[0];
    const name = label
      ? [...label.childNodes].filter((node) => !node.contains(select)).map((node) => node.textContent).join("").trim()
      : select.getAttribute("aria-label");
    if (name) list.setAttribute("aria-label", name);
    [...select.options].forEach((option, index) => {
      const item = document.createElement("li");
      item.id = `mac-select-option-${index}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(option.selected));
      if (option.disabled) item.setAttribute("aria-disabled", "true");
      item.dataset.index = String(index);
      item.append(option.label || option.text);
      item.hidden = option.hidden;
      list.append(item);
    });
    // A modal dialog makes everything outside it inert, so the menu has to live inside it.
    (select.closest("dialog[open]") || document.body).append(list);
    open = { select, list, active: -1 };
    select.setAttribute("aria-expanded", "true");
    select.setAttribute("aria-controls", list.id);
    place();
    setActive(select.selectedIndex >= 0 ? select.selectedIndex : step(-1, 1));
    requestAnimationFrame(() => list.classList.add("is-open"));
  }

  function place() {
    const { select, list } = open;
    const box = select.getBoundingClientRect();
    open.anchor = `${box.left},${box.top}`;
    const gap = 4;
    const margin = 8;
    list.style.minWidth = `${box.width}px`;
    list.style.maxWidth = `${innerWidth - 2 * margin}px`;
    const below = innerHeight - box.bottom - gap - margin;
    const above = box.top - gap - margin;
    const height = list.scrollHeight;
    const flip = height > below && above > below;
    list.classList.toggle("is-above", flip);
    list.style.maxHeight = `${Math.max(flip ? above : below, 80)}px`;
    list.style.top = flip ? "" : `${box.bottom + gap}px`;
    list.style.bottom = flip ? `${innerHeight - box.top + gap}px` : "";
    list.style.left = `${Math.max(margin, Math.min(box.left, innerWidth - list.offsetWidth - margin))}px`;
  }

  function typeahead(key) {
    const now = Date.now();
    typed = now - typedAt > 800 ? key : typed + key;
    typedAt = now;
    // Repeating one letter cycles through items with that initial, as in native menus.
    const search = [...typed].every((letter) => letter === typed[0]) ? typed[0] : typed;
    const options = [...open.select.options];
    const start = search.length === 1 ? open.active + 1 : open.active;
    for (let offset = 0; offset < options.length; offset += 1) {
      const index = (start + offset) % options.length;
      const option = options[index];
      if (!option.disabled && !option.hidden && (option.label || option.text).trim().toLowerCase().startsWith(search.toLowerCase())) {
        return setActive(index);
      }
    }
  }

  document.addEventListener("mousedown", (event) => {
    if (open?.list.contains(event.target)) {
      event.preventDefault();
      return;
    }
    const select = event.target;
    if (event.button !== 0 || !usable(select)) {
      close(false);
      return;
    }
    event.preventDefault();
    if (select.disabled) return;
    if (open?.select === select) return close();
    select.focus({ preventScroll: true });
    show(select);
  }, true);

  const enabledItem = (event) => {
    const item = open && event.target.closest?.(".mac-select-menu li");
    return item && open.list.contains(item) && item.getAttribute("aria-disabled") !== "true" ? item : null;
  };

  document.addEventListener("mouseup", (event) => {
    const item = event.button === 0 && enabledItem(event);
    if (item) choose(Number(item.dataset.index));
  }, true);

  // Assistive tech (VoiceOver, Voice Control) clicks with no preceding mouse press, so detail is 0.
  // Pointer clicks already chose on mouseup and closed the menu.
  document.addEventListener("click", (event) => {
    const item = event.detail === 0 && enabledItem(event);
    if (item) choose(Number(item.dataset.index));
  }, true);

  document.addEventListener("mousemove", (event) => {
    const item = enabledItem(event);
    if (item) setActive(Number(item.dataset.index), false);
  }, true);

  document.addEventListener("keydown", (event) => {
    if (event.metaKey || event.ctrlKey || event.isComposing) return;
    if (!open) {
      const select = event.target;
      const opens = [" ", "Enter", "ArrowDown", "ArrowUp"].includes(event.key);
      if (opens && usable(select) && !select.disabled) {
        event.preventDefault();
        event.stopPropagation();
        show(select);
      }
      return;
    }
    const handled = {
      ArrowDown: () => setActive(event.altKey ? step(open.select.options.length, -1) : step(open.active, 1)),
      ArrowUp: () => setActive(event.altKey ? step(-1, 1) : step(open.active, -1)),
      Home: () => setActive(step(-1, 1)),
      PageUp: () => setActive(step(-1, 1)),
      End: () => setActive(step(open.select.options.length, -1)),
      PageDown: () => setActive(step(open.select.options.length, -1)),
      Enter: () => choose(open.active),
      " ": () => (typed && Date.now() - typedAt < 800 ? typeahead(" ") : choose(open.active)),
      Escape: () => close(),
      Tab: () => close(false),
    }[event.key];
    if (event.key === "Tab") return handled();
    event.preventDefault();
    event.stopPropagation();
    if (handled) handled();
    else if (event.key.length === 1 && !event.altKey) typeahead(event.key);
  }, true);

  // Anything that moves the control or takes focus away dismisses the menu, like a native one.
  addEventListener("blur", () => close(false));
  addEventListener("resize", () => close(false));
  document.addEventListener("scroll", () => {
    const box = open?.select.getBoundingClientRect();
    if (box && `${box.left},${box.top}` !== open.anchor) close(false);
  }, true);
  document.addEventListener("focusout", (event) => {
    if (open && event.target === open.select) setTimeout(() => open && document.activeElement !== open.select && close(false));
  }, true);
  new MutationObserver((records) => {
    if (!open) return;
    const { select } = open;
    const changed = records.some((record) => select.contains(record.target) && record.type === "childList");
    if (changed || !select.isConnected || select.disabled || !select.getClientRects().length) close(false);
  }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["disabled", "hidden", "open"] });
})();
