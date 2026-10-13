export const MAX_VISIBLE_MESSAGES = 200;

export function historyWindow<T extends { id: string }>(messages: T[], endId = "", limit = MAX_VISIBLE_MESSAGES) {
  const anchor = endId ? messages.findIndex(message => message.id === endId) : -1;
  const end = anchor < 0 ? messages.length : anchor + 1;
  const start = Math.max(0, end - Math.min(MAX_VISIBLE_MESSAGES, Math.max(1, limit)));
  return { messages: messages.slice(start, end), start, end, hasNewer: end < messages.length };
}

const renderedNodes = new WeakMap<HTMLElement, Map<string, { node: Element; markup: string }>>();

/** Reuse unchanged rows so streaming elsewhere preserves focus, images and selection. */
export function reconcileMarkup(root: HTMLElement, markup: string): void {
  const template = root.ownerDocument.createElement("template");
  template.innerHTML = markup;
  const previous = renderedNodes.get(root) ?? new Map();
  const next = new Map<string, { node: Element; markup: string }>();
  let position: ChildNode | null = root.firstChild;
  for (const candidate of Array.from(template.content.children)) {
    const key = candidate.getAttribute("data-message-id") || candidate.getAttribute("data-ui-key") || candidate.id;
    const candidateMarkup = candidate.outerHTML;
    const cached = key ? previous.get(key) : undefined;
    let node = cached?.markup === candidateMarkup && cached.node.parentNode === root ? cached.node : candidate;
    if (cached && node === candidate && cached.node.parentNode === root && candidate.hasAttribute("data-message-id")) {
      const previousTemplate = root.ownerDocument.createElement("template");
      previousTemplate.innerHTML = cached.markup;
      const prior = previousTemplate.content.querySelector(".message-content-stack");
      const live = cached.node.querySelector(".message-content-stack");
      const incoming = candidate.querySelector(".message-content-stack");
      // Append-only committed replies retain existing text selections, image decodes
      // and focus. Complex card structure changes still use the normal row replacement.
      if (prior && live && incoming && cached.node.querySelector(".message-body")?.childElementCount === candidate.querySelector(".message-body")?.childElementCount) {
        const oldMarkup = Array.from(prior.children).map(item => item.outerHTML);
        const existing = Array.from(live.children) as Element[];
        const incomingChildren = Array.from(incoming.children);
        incomingChildren.forEach((item, index) => {
          if (existing[index] && oldMarkup[index] === item.outerHTML) return;
          if (existing[index]) existing[index]!.replaceWith(item); else live.append(item);
        });
        for (let index = incomingChildren.length; index < existing.length; index += 1) existing[index]?.remove();
        cached.node.setAttribute("class", candidate.getAttribute("class") || "");
        for (const selector of [".meta", ".message-tools", ".rendezvous-card"]) {
          const nextPart = candidate.querySelector(selector), previousPart = cached.node.querySelector(selector);
          if (nextPart && previousPart && nextPart.outerHTML !== previousPart.outerHTML) previousPart.replaceWith(nextPart);
        }
        node = cached.node;
      }
    }
    if (node !== position) root.insertBefore(node, position);
    position = node.nextSibling;
    if (key) next.set(key, { node, markup: candidateMarkup });
  }
  while (position) {
    const unwanted = position;
    position = position.nextSibling;
    unwanted.remove();
  }
  renderedNodes.set(root, next);
}

export function focusWithin(event: KeyboardEvent, panel: HTMLElement): boolean {
  if (event.key !== "Tab") return false;
  const controls = Array.from(panel.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'))
    .filter(item => !item.hidden && !item.closest("[hidden], [inert]") && item.getClientRects().length > 0);
  if (!controls.length) return false;
  const first = controls[0]!;
  const last = controls[controls.length - 1]!;
  if (event.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) {
    event.preventDefault(); last.focus(); return true;
  }
  if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) {
    event.preventDefault(); first.focus(); return true;
  }
  return false;
}
