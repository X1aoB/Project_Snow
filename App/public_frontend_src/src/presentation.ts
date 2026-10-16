/** Presentation budgets are elapsed-time budgets, never extra provider latency. */
export const PRESENTATION_TIMING = Object.freeze({ feedbackMin: 250, feedbackMax: 400, firstReply: 600, segmentMin: 200, segmentMax: 450, segmentBudget: 1200, mode: 360 });
export function remainingNaturalWait(startedAt: number, now: number, reduced = false): number {
  return reduced ? 0 : Math.max(0, PRESENTATION_TIMING.firstReply - Math.max(0, now - startedAt));
}
export function segmentWait(textLength: number, spent: number, reduced = false): number {
  return reduced ? 0 : Math.max(0, Math.min(PRESENTATION_TIMING.segmentBudget - spent, Math.max(PRESENTATION_TIMING.segmentMin, Math.min(PRESENTATION_TIMING.segmentMax, 200 + textLength * 3))));
}
export const COMMUNICATOR_TIMING = Object.freeze({ perGrapheme: 250, jitter: .15, bubbleMin: 1500, bubbleMax: 20000, gapMin: 600, gapMax: 1200, artificialBudget: 60000 });
function timingFraction(key: string): number {
  let hash = 2166136261;
  for (const character of key) hash = Math.imul(hash ^ character.codePointAt(0)!, 16777619);
  return (hash >>> 0) / 0xffffffff;
}
export function graphemeCount(text: string): number {
  return Array.from(new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(text)).length;
}
export function communicatorBubbleTiming(text: string, requestId: string, index: number): { typing: number; gap: number } {
  const factor = 1 + (timingFraction(`${requestId}:typing:${index}`) * 2 - 1) * COMMUNICATOR_TIMING.jitter;
  return {
    typing: Math.max(COMMUNICATOR_TIMING.bubbleMin, Math.min(COMMUNICATOR_TIMING.bubbleMax, graphemeCount(text) * COMMUNICATOR_TIMING.perGrapheme * factor)),
    gap: index === 0 ? 0 : COMMUNICATOR_TIMING.gapMin + timingFraction(`${requestId}:gap:${index}`) * (COMMUNICATOR_TIMING.gapMax - COMMUNICATOR_TIMING.gapMin),
  };
}
/** Only presentation time: immutable confirmed blocks and their order remain untouched.
 * While the terminal packet is absent, reserve half the remaining budget for later
 * blocks. Once complete, proportionally distribute the full remaining budget. */
export function communicatorDelayPlan(texts: readonly string[], requestId: string, firstIndex = 0,
  elapsedBeforeFirst = 0, spent = 0, terminal = true): number[] {
  const waits = texts.map((text, offset) => {
    const timing = communicatorBubbleTiming(text, requestId, firstIndex + offset);
    return Math.max(0, timing.typing + timing.gap - (offset === 0 ? Math.max(0, elapsedBeforeFirst) : 0));
  });
  const budget = Math.max(0, COMMUNICATOR_TIMING.artificialBudget - spent) * (terminal ? 1 : .5);
  const total = waits.reduce((sum, value) => sum + value, 0);
  const scale = total > budget ? budget / total : 1;
  return waits.map(value => value * scale);
}
export interface ConfirmedBlock { type: "message" | "speech"; text: string }
export interface CommittedBlockEvent { request_id: string; character_id: string; sequence: number; block_id: string; block: ConfirmedBlock }
/** Only server-committed text may enter the visible reply. Replays are immutable. */
export class CommittedReply {
  private events: CommittedBlockEvent[] = [];
  constructor(readonly requestId: string, readonly characterId: string) {}
  accept(value: unknown): boolean {
    const event = value as CommittedBlockEvent;
    if (!event || event.request_id !== this.requestId || event.character_id !== this.characterId
      || !Number.isSafeInteger(event.sequence) || event.sequence < 0 || typeof event.block_id !== "string" || !event.block_id
      || !event.block || !["message", "speech"].includes(event.block.type) || typeof event.block.text !== "string" || !event.block.text.trim()) throw new Error("stream_commit_invalid");
    const previous = this.events.find(item => item.block_id === event.block_id || item.sequence === event.sequence);
    if (previous) {
      if (previous.block_id !== event.block_id || previous.sequence !== event.sequence || previous.block.type !== event.block.type || previous.block.text !== event.block.text) throw new Error("stream_commit_conflict");
      return false;
    }
    const last = this.events.at(-1);
    if (last ? event.sequence !== last.sequence + 1 : event.sequence !== 0) throw new Error("stream_commit_sequence");
    this.events.push({ ...event, block: { ...event.block } });
    return true;
  }
  get blocks(): ConfirmedBlock[] { return this.events.map(item => ({ ...item.block })); }
  get snapshot(): CommittedBlockEvent[] { return this.events.map(item => ({ ...item, block: { ...item.block } })); }
  verifyFinal(blocks: Array<{ type: string; text?: string }>): void {
    for (let index = 0; index < this.events.length; index += 1) {
      const expected = this.events[index]!.block;
      if (blocks[index]?.type !== expected.type || blocks[index]?.text !== expected.text) throw new Error("stream_commit_conflict");
    }
  }
}

/** A visual cancellation must never abort a business request. */
export class MotionScope {
  private animations = new Set<Animation>();
  private cleanups = new Set<() => void>();
  cancel(): void {
    for (const animation of this.animations) animation.cancel();
    this.animations.clear();
    for (const cleanup of this.cleanups) cleanup();
    this.cleanups.clear();
  }
  async play(node: Element, frames: Keyframe[], options: KeyframeAnimationOptions): Promise<void> {
    let animation: Animation | undefined;
    try {
      if (typeof node.animate !== "function") return;
      animation = node.animate(frames, options);
      this.animations.add(animation);
      await animation.finished;
    } catch { /* Decorative motion cannot change a saved reply or reject a render. */ }
    finally {
      if (animation) {
        this.animations.delete(animation);
        try { animation.cancel(); } catch { /* The element may already be gone. */ }
      }
    }
  }
  cleanup(callback: () => void): void { this.cleanups.add(callback); }
}
