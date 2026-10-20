import { createProactiveClient } from "/modules/snow-proactive-client.js";

// Browser lifecycle only. Pagination layout and settings/reminder UI are registered by Kiro.
export async function installFeatureRuntime(logic, adapter) {
  const cleanups = [], listen = (target, type, listener) => {
    target.addEventListener(type, listener); cleanups.push(() => target.removeEventListener(type, listener));
  };
  const on = (type, listener) => cleanups.push(adapter.on(type, listener));
  const safely = action => { void Promise.resolve().then(action).catch(() => {}); };
  const visible = () => !document.hidden && document.documentElement.dataset.ui === "snow-adv";
  const proactive = createProactiveClient({ ...adapter, visible, activeReminders: () => logic.reminders.active() });
  logic.proactive = proactive;
  let typing = false, typingTimer, environmentKey = "";
  function environment() {
    const state = adapter.snapshot();
    const next = { hidden: document.hidden, modalOpen: Boolean(document.querySelector("dialog[open]")),
      uiHidden: document.getElementById("in-person-surface")?.classList.contains("ui-hidden") || false,
      typing, paused: !visible() || state.channel !== "in_person" || state.writerOwned === false };
    const key = JSON.stringify(next);
    if (key !== environmentKey) { environmentKey = key; logic.reading.environment(next); }
  }
  function check() { environment(); safely(() => logic.reminders.reconcile(adapter.snapshot().rendezvous)); }
  function wake() { check(); safely(() => proactive.refresh()); }
  listen(document, "visibilitychange", wake); listen(window, "focus", wake);
  listen(document, "input", event => {
    if (!event.target.matches?.("textarea,input,[contenteditable='true']")) return;
    typing = true; clearTimeout(typingTimer); environment();
    typingTimer = setTimeout(() => { typing = false; environment(); }, 700);
  });
  listen(document, "focusout", () => { typing = false; clearTimeout(typingTimer); environment(); });
  const observer = new MutationObserver(environment);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-ui"] });
  observer.observe(document.body, { subtree: true, attributes: true, attributeFilter: ["open", "class"] });
  on("selected-changed", () => { logic.reading.cancel(); check(); });
  on("channel-changed", () => { logic.reading.cancel(); check(); });
  on("history-changed", () => { proactive.invalidate(); check(); });
  on("world-changed", check);
  on("rendezvous-settled", detail => safely(() => logic.reminders.settle(detail)));
  on("reminder-due", ({ reminder }) => { if (visible()) safely(() => proactive.send(reminder)); });
  const dispose = logic.dispose;
  logic.dispose = () => { clearTimeout(typingTimer); observer.disconnect(); cleanups.forEach(cleanup => cleanup?.());
    proactive.dispose(); dispose(); };
  environment();
  await logic.reminders.start();
}
