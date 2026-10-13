/** Presentation budgets are elapsed-time budgets, never extra provider latency. */
export const PRESENTATION_TIMING = Object.freeze({ feedbackMin: 250, feedbackMax: 400, firstReply: 600, segmentMin: 200, segmentMax: 450, segmentBudget: 1200, mode: 360 });
export function remainingNaturalWait(startedAt: number, now: number, reduced = false): number {
  return reduced ? 0 : Math.max(0, PRESENTATION_TIMING.firstReply - Math.max(0, now - startedAt));
}
export function segmentWait(textLength: number, spent: number, reduced = false): number {
  return reduced ? 0 : Math.max(0, Math.min(PRESENTATION_TIMING.segmentBudget - spent, Math.max(PRESENTATION_TIMING.segmentMin, Math.min(PRESENTATION_TIMING.segmentMax, 200 + textLength * 3))));
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
  play(node: Element, frames: Keyframe[], options: KeyframeAnimationOptions): Promise<void> {
    if (typeof node.animate !== "function") return Promise.resolve();
    const animation = node.animate(frames, options);
    this.animations.add(animation);
    return animation.finished.catch(() => {}).then(() => { this.animations.delete(animation); animation.cancel(); });
  }
  cleanup(callback: () => void): void { this.cleanups.add(callback); }
}
