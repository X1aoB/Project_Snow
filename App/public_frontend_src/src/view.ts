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
    const node = cached?.markup === candidateMarkup && cached.node.parentNode === root ? cached.node : candidate;
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
