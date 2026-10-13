import test from "node:test";
import assert from "node:assert/strict";
import { remainingNaturalWait, segmentWait, CommittedReply, MotionScope } from "../../public_frontend/modules/runtime.js";

test("natural timing deducts real waiting and caps all segment pauses", () => {
  assert.equal(remainingNaturalWait(1000, 1200), 400);
  assert.equal(remainingNaturalWait(1000, 1800), 0);
  assert.equal(remainingNaturalWait(1000, 1200, true), 0);
  let spent = 0;
  for (let n = 0; n < 30; n++) spent += segmentWait(500, spent);
  assert.equal(spent, 1200);
  assert.equal(segmentWait(0, 0), 200);
  assert.equal(segmentWait(500, 0, true), 0);
});

const event = (sequence, text = "已确认。") => ({request_id:"request",character_id:"character",sequence,block_id:`block-${sequence}`,block:{type:"message",text}});
test("committed text accepts immutable replay but rejects reordering and rewritten prefixes", () => {
  const reply = new CommittedReply("request", "character");
  assert.equal(reply.accept(event(0)), true);
  assert.equal(reply.accept(event(0)), false);
  assert.throws(() => reply.accept(event(0,"改写。")), /conflict/);
  assert.throws(() => reply.accept(event(2)), /sequence/);
  assert.throws(() => reply.accept({...event(1),character_id:"other"}), /invalid/);
  assert.throws(() => reply.accept({...event(1),block:{type:"action",text:"动作"}}), /invalid/);
  reply.accept(event(1,"下一句。"));
  reply.verifyFinal([...reply.blocks,{type:"message",text:"完整结尾。"}]);
  assert.throws(() => reply.verifyFinal([{type:"message",text:"改写。"}]), /conflict/);
  const copy = reply.snapshot; copy[0].block.text = "mutated";
  assert.equal(reply.blocks[0].text,"已确认。");
});

test("motion cancellation settles and cleans resources without a network controller", async () => {
  const scope = new MotionScope();
  let cancel = 0, cleaned = 0, reject;
  const animation = {finished:new Promise((_, no) => {reject=no;}),cancel(){cancel++;reject(new Error("cancelled"));}};
  scope.cleanup(() => cleaned++);
  const pending = scope.play({animate:()=>animation},[],{duration:360});
  scope.cancel(); await pending; scope.cancel();
  assert.equal(cleaned,1); assert.ok(cancel >= 1);
});
