import test from "node:test";
import assert from "node:assert/strict";
import { remainingNaturalWait, segmentWait, COMMUNICATOR_TIMING, graphemeCount, communicatorBubbleTiming, communicatorDelayPlan, CommittedReply, MotionScope } from "../../public_frontend/modules/runtime.js";

test("communicator pacing counts graphemes and keeps forty characters within the chosen cadence", () => {
  assert.equal(graphemeCount("你👨‍👩‍👧‍👦e\u0301"),3);
  for (let index=0;index<100;index++) {
    const timing=communicatorBubbleTiming("雪".repeat(40),`request-${index}`,0);
    assert.ok(timing.typing >=8500 && timing.typing <=11500);
    assert.equal(timing.gap,0);
    assert.deepEqual(timing,communicatorBubbleTiming("雪".repeat(40),`request-${index}`,0));
    const next=communicatorBubbleTiming("雪".repeat(40),`request-${index}`,1);
    assert.ok(next.gap >=600 && next.gap <=1200);
  }
  assert.equal(communicatorBubbleTiming("", "request", 0).typing,1500);
  assert.equal(communicatorBubbleTiming("雪".repeat(1000), "request", 0).typing,20000);
});

test("communicator timing credits provider elapsed and compresses every remaining bubble proportionally", () => {
  const texts=Array.from({length:8},(_,index)=>"雪".repeat(100+index));
  const unconstrained=texts.map((text,index)=>{const value=communicatorBubbleTiming(text,"long",index);return value.typing+value.gap;});
  const plan=communicatorDelayPlan(texts,"long");
  assert.ok(Math.abs(plan.reduce((sum,ms)=>sum+ms,0)-60000)<.001);
  const factor=plan[0]/unconstrained[0];
  plan.forEach((value,index)=>{
    assert.ok(value>0); // No last-block dump when the budget becomes tight.
    assert.ok(Math.abs(value/unconstrained[index]-factor)<.00001);
  });
  assert.equal(communicatorDelayPlan(["雪".repeat(40)],"slow-provider",0,20000)[0],0);
  const first=communicatorDelayPlan(["雪".repeat(40)],"provider",0,0)[0];
  assert.equal(communicatorDelayPlan(["雪".repeat(40)],"provider",0,3000)[0],first-3000);
  const remaining=communicatorDelayPlan(texts.slice(3),"long",3,0,48000);
  assert.ok(Math.abs(remaining.reduce((sum,ms)=>sum+ms,0)-12000)<.001);
  assert.ok(remaining.every(ms=>ms>0));
});

test("an unfinished verified prefix reserves presentation budget for later immutable blocks", () => {
  const original=Object.freeze(["雪".repeat(100),"风".repeat(100),"雨".repeat(100)]);
  const prefix=communicatorDelayPlan(original,"stream",0,0,0,false);
  assert.ok(Math.abs(prefix.reduce((a,b)=>a+b,0)-30000)<.001);
  const tail=communicatorDelayPlan(["下一句。","完整结尾。"],"stream",3,0,30000,true);
  assert.ok(tail.every(ms=>ms>0));
  assert.ok(prefix.reduce((a,b)=>a+b,0)+tail.reduce((a,b)=>a+b,0)<=COMMUNICATOR_TIMING.artificialBudget);
  assert.deepEqual(original,["雪".repeat(100),"风".repeat(100),"雨".repeat(100)]);
});

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
