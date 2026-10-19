"""Reading and reminder contracts in Chromium, with local fixtures and a deterministic clock."""
from __future__ import annotations

from unittest import TestCase, skipUnless

from tests import test_snow_feature_logic as feature_tests


HARNESS = r"""async()=>{
  window.readingModule=await import('/modules/snow-reading-logic.js');
  window.reminderModule=await import('/modules/snow-reminder-logic.js');
  window.featureHarness=(initial={})=>{
    const clone=value=>structuredClone(value),listeners=new Map(),local=new Map(),tab=new Map(),states=new Map();
    const state={historyEpoch:0,writerOwned:true,characters:[{id:'synthetic-a',name:'合成角色'}],
      ...clone(initial.state||{})};
    let now=initial.now??Date.parse('2026-10-19T12:00:00+08:00'),sequence=0,prefs=clone(initial.preferences||{});
    const timers=new Map(),messages=clone(initial.messages||[]);
    for(const record of initial.states||[])states.set(record.key,clone(record));
    const controls={writes:[],events:[],changes:[],emissionOrder:[],announcements:[],rendered:[],failState:false,failPreferences:false,
      messageGate:null,stateGate:null,preferenceGate:null,readGate:null,readSnapshot:null,revealPlans:[]};
    const clock={now:()=>now,setTimeout(fn,delay){const id=++sequence;timers.set(id,{at:now+Math.max(0,delay),fn});return id;},clearTimeout:id=>timers.delete(id)};
    const storage=values=>({getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)});
    const day=()=>new Date(now+8*3600000).toISOString().slice(0,10);
    const adapter={clock,localPreferences:storage(local),tabPreferences:storage(tab),
      snapshot:()=>clone({...state,localDayKey:day()}),preferences:()=>clone(prefs),
      async savePreferences(patch){const epoch=state.historyEpoch;controls.writes.push(['preferences',clone(patch)]);
        if(controls.preferenceGate)await controls.preferenceGate;
        if(controls.failPreferences)throw new Error('synthetic_disk_failure');
        if(epoch!==state.historyEpoch||!state.writerOwned)throw new Error('history_busy');prefs={...prefs,...clone(patch)};},
      async getMessage(id){if(controls.messageGate)await controls.messageGate;return clone(messages.find(item=>item.id===id));},
      async readStates(){const result=clone(controls.readSnapshot||[...states.values()]);if(controls.readGate)await controls.readGate;return result;},
      async saveState(key,value){const epoch=state.historyEpoch;controls.writes.push(['state',key,clone(value)]);
        if(controls.stateGate)await controls.stateGate;if(controls.failState)throw new Error('synthetic_disk_failure');
        if(epoch!==state.historyEpoch||!state.writerOwned)throw new Error('history_busy');states.set(key,clone(value));},
      loadDialogue:async()=>({}),saveDialogue:async()=>{},messages:async()=>clone(messages),allMessages:async()=>clone(messages),
      on(type,listener){if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(listener);return()=>listeners.get(type).delete(listener);},
      announce:value=>controls.announcements.push(value),
      emit(type,payload){controls.emissionOrder.push(type);(type==='reminders-changed'?controls.changes:controls.events).push([type,clone(payload)]);
        for(const listener of listeners.get(type)||[])listener(clone(payload));},
      revealPlan(graphemes,start){controls.revealPlans.push({graphemes:clone(graphemes),start});return {initialDelay:100,
        steps:graphemes.slice(start).map((_,index)=>({end:start+index+1,delayAfter:start+index===graphemes.length-1?0:100}))};},
    };
    const node=document.createElement('div');node.id='synthetic-speech';document.body.append(node);
    let advance=()=>{},toggle=()=>{};
    const pager={paginate:text=>[{start:0,end:text.length}],
      render(value){controls.rendered.push(clone(value));const page=value.pages[value.index];node.textContent=value.text.slice(page.start,Math.min(page.end,value.revealedEnd));},
      onAdvance(fn){advance=fn;return()=>{advance=()=>{};};},onToggleAuto(fn){toggle=fn;return()=>{toggle=()=>{};};}};
    const h={state,adapter,controls,clock,pager,node,local,tab,states,messages,advance:()=>advance(),toggle:value=>toggle(value),
      preferences:()=>clone(prefs),replacePreferences:value=>{prefs=clone(value);},day,
      at(value){now=typeof value==='number'?value:Date.parse(value);},
      async flush(){for(let i=0;i<30;i++)await Promise.resolve();},
      async tick(ms){const target=now+ms;let count=0;while(true){const next=[...timers].filter(([,value])=>value.at<=target).sort((a,b)=>a[1].at-b[1].at||a[0]-b[0])[0];
        if(!next)break;if(++count>10000)throw new Error('synthetic_timer_loop');now=next[1].at;timers.delete(next[0]);next[1].fn();await h.flush();}now=target;await h.flush();},
      timers:()=>[...timers.values()].map(value=>value.at-now),
      history(){state.historyEpoch++;for(const listener of listeners.get('history-changed')||[])listener();},
      reading(){return readingModule.createReadingLogic(adapter);},reminders(){return reminderModule.createReminderLogic(adapter);},
      install(){return featureModule.createFeatureLogic(adapter);}};
    return h;
  };
  window.speech=(id,text='甲乙丙丁',patch={})=>({id,characterId:'synthetic-a',role:'assistant',status:'sent',
    contentBlocks:[{type:'speech',text}],...patch});
  window.world=(items=[{character_id:'synthetic-a',rendezvous_id:'rv-a',location_id:'archive',location_name:'资料室',schedule_date:'2026-10-19'}],authoritative=true)=>
    ({authoritative,dayKey:'2026-10-19',items});
  window.reminder=(id='r1',patch={})=>({id,characterId:'synthetic-a',rendezvousId:'rv-a',pendingRequestId:'request-a',
    locationId:'archive',locationName:'资料室',remindAt:'2026-10-19T12:01:00+08:00',dayKey:'2026-10-19',status:'scheduled',
    createdAt:Date.parse('2026-10-19T12:00:00+08:00'),...patch});
}"""


@skipUnless(feature_tests.RUN_PUBLIC_E2E, "browser reading/reminder logic runs in the UI/full CI tier")
class SnowReadingReminderLogicTests(TestCase):
    setUpClass = classmethod(feature_tests.SnowFeatureLogicTests.setUpClass.__func__)
    tearDownClass = classmethod(feature_tests.SnowFeatureLogicTests.tearDownClass.__func__)

    def setUp(self):
        feature_tests.SnowFeatureLogicTests.setUp(self)
        self.page.evaluate(HARNESS)

    def tearDown(self):
        feature_tests.SnowFeatureLogicTests.tearDown(self)

    def test_reading_builds_canonical_utf16_spans_and_preserves_action_separation(self):
        result = self.page.evaluate("""()=>readingModule.buildReadingText([
          {type:'speech',text:' 你好。 '},{type:'message',text:'😀回应'},
          {type:'action',text:'动作不可读'},{type:'speech',text:' ABC! '},{type:'speech',text:'Next'}])""")
        self.assertEqual(result, {"text": "你好。😀回应\nABC! Next", "spans": [
            {"blockIndex": 0, "textStart": 0, "textEnd": 3, "blockStart": 1},
            {"blockIndex": 1, "textStart": 3, "textEnd": 7, "blockStart": 0},
            {"blockIndex": 3, "textStart": 8, "textEnd": 12, "blockStart": 1},
            {"blockIndex": 4, "textStart": 13, "textEnd": 17, "blockStart": 0}]})

    def test_without_pager_keeps_legacy_and_lifecycle_cancels_owned_waits(self):
        result = self.page.evaluate("""async()=>{
          const msg=speech('one'),h=featureHarness({messages:[msg]}),r=h.reading();await r.ready;
          const before={enabled:r.enabled(),present:r.present({message:msg}),active:r.active(),owns:r.owns('one')};
          h.pager.paginate=()=>[{start:0,end:2},{start:2,end:4}];r.setPager(h.pager);
          r.present({message:msg,key:'one'});const waiting=r.wait('one');const during={enabled:r.enabled(),active:r.active(),owns:r.owns('one')};
          r.setPager(null);const cancelled=await waiting;const after={enabled:r.enabled(),active:r.active(),owns:r.owns('one')};
          r.dispose();return {before,during,cancelled,after,disposed:r.owns('one')};
        }""")
        self.assertEqual(result, {"before": {"enabled": False, "present": False, "active": False, "owns": False},
                                  "during": {"enabled": True, "active": True, "owns": True}, "cancelled": False,
                                  "after": {"enabled": False, "active": False, "owns": True}, "disposed": False})

    def test_mark_read_is_monotonic_canonical_and_rejects_surrogate_or_action_offsets(self):
        result = self.page.evaluate("""async()=>{
          const h=featureHarness({messages:[speech('one','甲😀乙',{contentBlocks:[{type:'action',text:'动作'},{type:'speech',text:'甲😀乙'}]})]}),r=h.reading();await r.ready;
          const checks=[];for(const [block,end] of [[1,3],[1,1],[1,2],[0,2],[1,5],[-1,1]])checks.push(await r.markRead('one',block,end));
          const record=h.states.get('stage_read_v1:synthetic-a');return {checks,upTo:r.readUpTo('one',1),record,writes:h.controls.writes.length,
            noText:!JSON.stringify(record).includes('甲')};
        }""")
        self.assertEqual(result["checks"], [True, True, False, False, False, False])
        self.assertEqual(result["upTo"], 3)
        self.assertEqual(result["writes"], 1)
        self.assertTrue(result["noText"])
        self.assertEqual(result["record"]["messages"][0]["blocks"], {"1": 3})

    def test_read_retention_is_200_per_character_and_refresh_returns_copies(self):
        result = self.page.evaluate("""async()=>{
          const messages=Array.from({length:201},(_,i)=>speech('m'+i)),h=featureHarness({messages}),r=h.reading();await r.ready;
          for(const msg of messages){await r.markRead(msg.id,0,4);await h.tick(1);}
          const record=h.states.get('stage_read_v1:synthetic-a');record.messages[0].blocks[0]=0;
          return {count:record.messages.length,old:r.readUpTo('m0',0),latest:r.readUpTo('m200',0),first:record.messages[0].messageId};
        }""")
        self.assertEqual(result, {"count": 200, "old": 0, "latest": 4, "first": "m200"})

    def test_read_only_epoch_race_and_failed_storage_never_commit_read_cache(self):
        result = self.page.evaluate("""async()=>{
          const results=[];
          for(const mode of ['readonly','epoch','history','dispose','disk']){
            const h=featureHarness({messages:[speech('one')]}),r=h.reading();await r.ready;let release;
            if(mode==='readonly')h.state.writerOwned=false;
            if(mode==='disk')h.controls.failState=true;
            else h.controls.messageGate=new Promise(resolve=>release=resolve);
            const pending=r.markRead('one',0,4);await h.flush();
            if(mode==='epoch')h.state.historyEpoch++;
            if(mode==='history')h.history();
            if(mode==='dispose')r.dispose();
            release?.();let value;try{value=await pending;}catch{value='failed';}
            results.push({mode,value,upTo:r.readUpTo('one',0),writes:h.controls.writes.length});
          }return results;
        }""")
        self.assertEqual(result, [{"mode": mode, "value": "failed" if mode == "disk" else False, "upTo": 0,
                                   "writes": 1 if mode == "disk" else 0} for mode in ("readonly", "epoch", "history", "dispose", "disk")])

    def test_read_refresh_discards_late_snapshot_after_history_import(self):
        result = self.page.evaluate("""async()=>{
          const old={key:'stage_read_v1:synthetic-a',characterId:'synthetic-a',messages:[{messageId:'old',blocks:{0:4},updatedAt:1}]};
          const h=featureHarness({states:[old]});let release;h.controls.readGate=new Promise(resolve=>release=resolve);
          const r=h.reading();h.states.clear();h.controls.readGate=null;h.history();await h.flush();release();await r.ready;await h.flush();
          return {old:r.readUpTo('old',0),states:h.states.size};
        }""")
        self.assertEqual(result, {"old": 0, "states": 0})

    def test_read_save_in_flight_cannot_restore_old_offsets_after_import(self):
        result = self.page.evaluate("""async()=>{
          const h=featureHarness({messages:[speech('one')]}),r=h.reading();await r.ready;
          let release;h.controls.stateGate=new Promise(resolve=>release=resolve);
          const pending=r.markRead('one',0,4);await h.flush();h.states.clear();h.history();await h.flush();release();
          let failed=false;try{await pending;}catch{failed=true;}
          return {failed,upTo:r.readUpTo('one',0),states:h.states.size};
        }""")
        self.assertEqual(result, {"failed": True, "upTo": 0, "states": 0})

    def test_paging_updates_canonical_offsets_only_after_text_paint_and_announces_page(self):
        result = self.page.evaluate("""async()=>{
          const msg=speech('one',' 甲😀乙。 ',{contentBlocks:[{type:'action',text:'动作'},{type:'speech',text:' 甲😀乙。 '},{type:'speech',text:'下一句'}]});
          const h=featureHarness({messages:[msg]}),r=h.reading();await r.ready;r.setPager(h.pager);
          h.pager.paginate=()=>[{start:0,end:5},{start:5,end:8}];let paints=0,completions=[];
          r.present({message:msg,key:'one',onFirstPaint:()=>{if(!h.node.textContent)throw new Error('empty_first_paint');paints++;},onComplete:value=>completions.push(value)});
          const firstEmpty=h.node.textContent;await h.tick(100);const first=h.node.textContent;await h.tick(200);
          const before=[r.readUpTo('one',1),r.readUpTo('one',2)];await h.tick(100);const pageDone=[r.readUpTo('one',1),r.readUpTo('one',2)];
          h.advance();r.skip();await h.flush();return {firstEmpty,first,before,pageDone,final:[r.readUpTo('one',1),r.readUpTo('one',2)],
            paints,completions,wait:await r.wait('one'),announcements:h.controls.announcements};
        }""")
        self.assertEqual(result, {"firstEmpty": "", "first": "甲", "before": [0, 0], "pageDone": [6, 0],
                                  "final": [6, 3], "paints": 1, "completions": [{"reason": "complete"}], "wait": True,
                                  "announcements": ["甲😀乙。", "下一句"]})

    def test_pager_rejects_incomplete_or_surrogate_split_and_noncanonical_text(self):
        result = self.page.evaluate("""async()=>{
          const msg=speech('one','甲😀乙'),h=featureHarness({messages:[msg]}),r=h.reading();await r.ready;r.setPager(h.pager);
          const result=[];for(const pages of [[{start:0,end:2},{start:2,end:4}],[{start:0,end:3}],[{start:1,end:4}],[{start:0,end:1},{start:2,end:4}]]){
            h.pager.paginate=()=>pages;result.push(r.present({message:msg}));}
          h.pager.paginate=text=>[{start:0,end:text.length}];result.push(r.present({message:msg,text:'display only'}));
          result.push(r.present({message:msg,text:'甲😀乙'}));return result;
        }""")
        self.assertEqual(result, [False, False, False, False, False, True])

    def test_speed_scales_original_plan_only_and_instant_avoids_typing(self):
        result = self.page.evaluate("""async()=>{
          const results=[];for(const [speed,time] of [['slow',160],['normal',100],['fast',55],['instant',0]]){
            const msg=speech('one','甲😀'),h=featureHarness({messages:[msg]}),r=h.reading();await r.ready;await r.setPreferences({textSpeed:speed});r.setPager(h.pager);
            r.present({message:msg,key:'one'});const before=h.node.textContent;if(time)await h.tick(time-1);const early=h.node.textContent;
            if(time)await h.tick(1);const first=h.node.textContent;await h.tick(time);
            results.push({speed,before,early,first,end:h.node.textContent,plans:h.controls.revealPlans.length,done:await r.wait('one')});
          }return results;
        }""")
        self.assertEqual(result, [{"speed": speed, "before": "甲😀" if speed == "instant" else "", "early": "甲😀" if speed == "instant" else "",
                                   "first": "甲😀" if speed == "instant" else "甲", "end": "甲😀", "plans": 0 if speed == "instant" else 1,
                                   "done": True} for speed in ("slow", "normal", "fast", "instant")])

    def test_skip_completes_then_skips_read_pages_to_first_unread_with_stable_offsets(self):
        result = self.page.evaluate("""async()=>{
          const msg=speech('one','甲乙丙丁戊己庚辛'),h=featureHarness({messages:[msg]}),r=h.reading();await r.ready;
          await r.markRead('one',0,6);h.pager.paginate=()=>[{start:0,end:2},{start:2,end:4},{start:4,end:6},{start:6,end:8}];r.setPager(h.pager);
          r.present({message:msg,key:'one'});const first={index:h.controls.rendered.at(-1).index,text:h.node.textContent};
          r.skip();const skipped={index:h.controls.rendered.at(-1).index,text:h.node.textContent};r.skip();await h.flush();
          return {first,skipped,last:h.node.textContent,upTo:r.readUpTo('one',0),done:await r.wait('one')};
        }""")
        self.assertEqual(result, {"first": {"index": 0, "text": "甲乙"}, "skipped": {"index": 3, "text": ""}, "last": "庚辛", "upTo": 8, "done": True})

    def test_auto_pauses_for_each_environment_and_resumes_remaining_duration(self):
        result = self.page.evaluate("""async()=>{
          const results=[];for(const key of ['hidden','modalOpen','uiHidden','typing','paused']){
            const msg=speech('one'),h=featureHarness({messages:[msg]}),r=h.reading();await r.ready;await r.setPreferences({textSpeed:'instant'});
            h.pager.paginate=()=>[{start:0,end:2},{start:2,end:4}];r.setPager(h.pager);r.setAuto(true);r.present({message:msg,key:'one'});
            await h.tick(600);r.environment({[key]:true});await h.tick(10000);const paused=h.controls.rendered.at(-1);
            r.environment({[key]:false});await h.tick(899);const before=h.controls.rendered.at(-1).index;await h.tick(1);
            results.push({key,paused:paused.index,progress:paused.autoProgress,before,after:h.controls.rendered.at(-1).index,done:await r.wait('one')});
          }return results;
        }""")
        self.assertEqual(result, [{"key": key, "paused": 0, "progress": 0.4, "before": 0, "after": 1, "done": True}
                                  for key in ("hidden", "modalOpen", "uiHidden", "typing", "paused")])

    def test_auto_runs_with_animation_disabled_and_prefs_storage_scopes_are_separate(self):
        result = self.page.evaluate("""async()=>{
          const msg=speech('one'),h=featureHarness({messages:[msg]}),r=h.reading();await r.ready;
          await r.setPreferences({textSpeed:'fast',autoDelay:'long',fastForwardRead:false});r.setAuto(true);
          const slow=r.autoDelayMs(20),cap=r.autoDelayMs(999),floor=r.autoDelayMs(0);
          h.pager.paginate=()=>[{start:0,end:2},{start:2,end:4}];r.setPager(h.pager);r.present({message:msg,key:'one',animate:false});
          await h.tick(1500);const after=h.controls.rendered.at(-1).index;
          const other=h.reading();await other.ready;return {slow,cap,floor,after,done:await r.wait('one'),prefs:other.preferences(),auto:other.autoEnabled(),
            local:[...h.local.keys()],tab:[...h.tab.keys()],writes:h.controls.writes.map(row=>row[0])};
        }""")
        self.assertEqual(result, {"slow": 2800, "cap": 9000, "floor": 1500, "after": 1, "done": True,
                                  "prefs": {"textSpeed": "fast", "autoDelay": "long", "fastForwardRead": False}, "auto": True,
                                  "local": ["project-snow-public:reading:v1"], "tab": ["project-snow-public:reading:auto"], "writes": ["state", "state"]})

    def test_new_reply_supersedes_pending_manual_pages_and_wait_supports_abort(self):
        result = self.page.evaluate("""async()=>{
          const one=speech('one'),two=speech('two'),h=featureHarness({messages:[one,two]}),r=h.reading();await r.ready;
          h.pager.paginate=()=>[{start:0,end:2},{start:2,end:4}];r.setPager(h.pager);r.present({message:one,key:'one',animate:false});
          const first=r.wait('one'),controller=new AbortController(),aborted=r.wait('one',controller.signal).catch(error=>error.name);
          controller.abort();r.present({message:two,key:'two',animate:false});h.advance();await h.flush();
          return {first:await first,aborted:await aborted,second:await r.wait('two'),active:r.active(),ownsOld:r.owns('one'),ownsNew:r.owns('two')};
        }""")
        self.assertEqual(result, {"first": False, "aborted": "AbortError", "second": True, "active": False, "ownsOld": True, "ownsNew": True})

    def test_reflow_preserves_absolute_reveal_and_first_paint_without_reading_unseen_expansion(self):
        result = self.page.evaluate("""async()=>{
          const msg=speech('one','甲乙丙丁戊己庚辛'),h=featureHarness({messages:[msg]}),r=h.reading();await r.ready;
          h.pager.paginate=()=>[{start:0,end:4},{start:4,end:8}];r.setPager(h.pager);let paints=0,completed=0;
          r.present({message:msg,key:'one',onFirstPaint:()=>paints++,onComplete:()=>completed++});await h.tick(400);
          const before={text:h.node.textContent,read:r.readUpTo('one',0)};
          h.pager.paginate=()=>[{start:0,end:6},{start:6,end:8}];const changed=r.reflow();await h.flush();
          const expanded={text:h.node.textContent,read:r.readUpTo('one',0),offset:h.controls.rendered.at(-1).revealedEnd};
          h.pager.paginate=()=>[{start:0,end:3}];const invalid=r.reflow();h.pager.paginate=()=>{throw new Error('synthetic_fonts_not_ready');};const threw=r.reflow();
          await h.tick(100);const partial={text:h.node.textContent,read:r.readUpTo('one',0)};await h.tick(100);
          const completedPage={text:h.node.textContent,read:r.readUpTo('one',0)};
          h.pager.paginate=()=>[{start:0,end:6},{start:6,end:8}];h.advance();await h.tick(100);
          h.pager.paginate=()=>[{start:0,end:2},{start:2,end:4},{start:4,end:6},{start:6,end:8}];r.reflow();
          const narrow={text:h.node.textContent,index:h.controls.rendered.at(-1).index,offset:h.controls.rendered.at(-1).revealedEnd};
          await h.tick(100);return {before,changed,expanded,invalid,threw,partial,completedPage,narrow,paints,completed,done:await r.wait('one'),
            starts:h.controls.revealPlans.map(plan=>plan.start)};
        }""")
        self.assertEqual(result, {"before": {"text": "甲乙丙丁", "read": 4}, "changed": True,
                                  "expanded": {"text": "甲乙丙丁", "read": 4, "offset": 4}, "invalid": False, "threw": False,
                                  "partial": {"text": "甲乙丙丁戊", "read": 4}, "completedPage": {"text": "甲乙丙丁戊己", "read": 6},
                                  "narrow": {"text": "庚", "index": 3, "offset": 7}, "paints": 1, "completed": 1, "done": True,
                                  "starts": [0, 4, 0, 1]})

    def test_reminder_validates_future_today_and_requires_authoritative_rendezvous_to_set(self):
        result = self.page.evaluate("""async()=>{
          const h=featureHarness(),r=h.reminders(),results=[];
          for(const at of ['2026-10-19T11:59:00+08:00','2026-10-20T00:00:00+08:00','bad'])results.push(await r.plan({characterId:'synthetic-a',locationId:'archive',requestId:'x',remindAt:at}));
          results.push(await r.set({characterId:'synthetic-a',rendezvousId:'rv-a',remindAt:'2026-10-19T12:01:00+08:00'}));
          await r.reconcile(world());results.push(await r.set({characterId:'synthetic-a',rendezvousId:'rv-a',remindAt:'2026-10-19T23:59:00+08:00'}));
          return {results,active:r.active(),writes:h.controls.writes.length};
        }""")
        self.assertEqual(result["results"], [{"ok": False, "reason": "invalid"}] * 4 + [{"ok": True, "id": "rendezvous:synthetic-a:rv-a"}])
        self.assertEqual(result["writes"], 1)
        self.assertEqual(result["active"][0]["remindAt"], "2026-10-19T15:59:00.000Z")

    def test_pending_invitation_binds_exact_request_and_never_blindly_uses_same_location(self):
        result = self.page.evaluate("""async()=>{
          const h=featureHarness(),r=h.reminders();await r.reconcile(world());
          await r.plan({characterId:'synthetic-a',locationId:'archive',requestId:'new-request',remindAt:'2026-10-19T12:01:00+08:00'});
          await r.settle({requestId:'old-request',status:'character_waiting',rendezvous:world().items[0]});
          const old=r.active()[0].rendezvousId;await r.settle({requestId:'new-request',status:'request_result_unknown'});
          await h.tick(70000);await r.check();const before={bound:r.active()[0].rendezvousId,events:h.controls.events.length};
          await r.settle({requestId:'new-request',status:'character_waiting',rendezvous:world().items[0]});
          return {old,before,after:r.active()[0].status,bound:r.active()[0].rendezvousId,events:h.controls.events.map(row=>row[0])};
        }""")
        self.assertEqual(result, {"old": None, "before": {"bound": None, "events": 0}, "after": "fired", "bound": "rv-a", "events": ["reminder-due"]})

    def test_declined_rescheduled_arrived_and_changed_authoritative_world_end_reminders(self):
        result = self.page.evaluate("""async()=>{
          const results=[];for(const status of ['rejected','rescheduled','cancelled','already_arrived']){
            const h=featureHarness(),r=h.reminders();await r.plan({characterId:'synthetic-a',locationId:'archive',requestId:'req',remindAt:'2026-10-19T12:01:00+08:00'});
            await r.settle({requestId:'req',status});results.push({status,active:r.active().length,stored:h.preferences().rendezvousReminders[0].status});
          }
          const h=featureHarness({preferences:{rendezvousReminders:[reminder()]}}),r=h.reminders();
          await r.reconcile(world([],false));const unknown=r.active().length;await r.reconcile(world([]));
          return {results,unknown,ended:r.active().length,stored:h.preferences().rendezvousReminders[0].status};
        }""")
        self.assertEqual(result, {"results": [{"status": status, "active": 0, "stored": "done"} for status in ("rejected", "rescheduled", "cancelled", "already_arrived")],
                                  "unknown": 1, "ended": 0, "stored": "done"})

    def test_due_requires_current_world_confirmation_without_zero_delay_timer_loop(self):
        result = self.page.evaluate("""async()=>{
          const h=featureHarness({preferences:{rendezvousReminders:[reminder()]}}),r=h.reminders();await r.start();
          await h.tick(120000);const unknown={events:h.controls.events.length,timers:h.timers(),status:r.active()[0].status};
          await r.reconcile(world([],false));const nonauthoritative=h.controls.events.length;await r.reconcile(world());
          return {unknown,nonauthoritative,events:h.controls.events.length,status:r.active()[0].status};
        }""")
        self.assertEqual(result, {"unknown": {"events": 0, "timers": [43080000], "status": "scheduled"}, "nonauthoritative": 0, "events": 1, "status": "fired"})

    def test_due_is_saved_before_emit_and_concurrent_checks_or_reload_do_not_repeat(self):
        result = self.page.evaluate("""async()=>{
          const h=featureHarness({state:{rendezvous:world()},preferences:{rendezvousReminders:[reminder()]}}),r=h.reminders();
          await r.start();h.at('2026-10-19T12:02:00+08:00');let release;h.controls.preferenceGate=new Promise(resolve=>release=resolve);
          const checks=[r.check(),r.check()];await h.flush();const before=h.controls.events.length;release();await Promise.all(checks);
          const saved=h.preferences().rendezvousReminders[0].status,copy=h.controls.events[0][1];copy.reminder.status='changed';
          r.dispose();h.controls.preferenceGate=null;const other=h.reminders();await other.start();await other.check();
          return {before,saved,events:h.controls.events.length,status:other.active()[0].status,character:h.controls.events[0][1].character.id,
            order:h.controls.emissionOrder};
        }""")
        self.assertEqual(result, {"before": 0, "saved": "fired", "events": 1, "status": "fired", "character": "synthetic-a",
                                  "order": ["reminders-changed", "reminder-due"]})

    def test_failed_storage_and_reader_tab_never_emit_or_change_saved_status(self):
        result = self.page.evaluate("""async()=>{
          const results=[];for(const mode of ['disk','readonly','lost-owner','history']){
            const h=featureHarness({state:{rendezvous:world()},preferences:{rendezvousReminders:[reminder()]}}),r=h.reminders();
            h.at('2026-10-19T12:02:00+08:00');let release;if(mode==='disk')h.controls.failPreferences=true;
            if(mode==='readonly')h.state.writerOwned=false;
            if(['lost-owner','history'].includes(mode))h.controls.preferenceGate=new Promise(resolve=>release=resolve);
            const pending=r.check();await h.flush();if(mode==='lost-owner')h.state.writerOwned=false;if(mode==='history')h.history();release?.();
            let failed=false;try{await pending;}catch{failed=true;}results.push({mode,failed,events:h.controls.events.length,status:h.preferences().rendezvousReminders[0].status,
              writes:h.controls.writes.length});
          }return results;
        }""")
        self.assertEqual(result, [{"mode": mode, "failed": mode != "readonly", "events": 0, "status": "scheduled", "writes": 0 if mode == "readonly" else 1}
                                  for mode in ("disk", "readonly", "lost-owner", "history")])

    def test_throttled_background_focus_catches_up_and_snooze_and_close_are_local(self):
        result = self.page.evaluate("""async()=>{
          const h=featureHarness({state:{rendezvous:world()},preferences:{rendezvousReminders:[reminder()]}}),r=h.reminders();await r.start();
          const fetch=window.fetch;window.fetch=()=>{throw new Error('reminder_network_forbidden');};
          try{h.at('2026-10-19T12:03:00+08:00');await r.environment();const first=h.controls.events.length;
            const snoozed=await r.snooze('r1');await h.tick(599999);const before=h.controls.events.length;await h.tick(1);
            const second=h.controls.events.length,closed=await r.clear('r1');await h.tick(600000);
            return {first,snoozed,before,second,closed,active:r.active().length,events:h.controls.events.length};}
          finally{window.fetch=fetch;}
        }""")
        self.assertEqual(result, {"first": 1, "snoozed": {"ok": True}, "before": 1, "second": 2, "closed": {"ok": True}, "active": 0, "events": 2})

    def test_disabled_reminders_expire_at_midnight_and_reenable_catches_up_today(self):
        result = self.page.evaluate("""async()=>{
          const h=featureHarness({state:{rendezvous:world()},preferences:{rendezvousReminders:[reminder()]}}),r=h.reminders();await r.start();
          await r.setPreferences({enabled:false});h.at('2026-10-19T12:02:00+08:00');await r.environment();const off=h.controls.events.length;
          await r.setPreferences({enabled:true});await r.environment();const on=h.controls.events.length;
          await r.setPreferences({enabled:false});h.at('2026-10-20T00:00:01+08:00');await r.environment();
          return {off,on,active:r.active().length,status:h.preferences().rendezvousReminders[0].status,lateSnooze:await r.snooze('r1')};
        }""")
        self.assertEqual(result, {"off": 0, "on": 1, "active": 0, "status": "expired", "lateSnooze": {"ok": False, "reason": "invalid"}})

    def test_limit_protects_active_reminders_and_reuses_only_ended_slots(self):
        result = self.page.evaluate("""async()=>{
          const rows=Array.from({length:50},(_,i)=>reminder('r'+i,{rendezvousId:null,pendingRequestId:'request-'+i,createdAt:i}));
          const h=featureHarness({preferences:{rendezvousReminders:rows}}),r=h.reminders();
          const input={characterId:'synthetic-a',locationId:'archive',requestId:'new',remindAt:'2026-10-19T12:01:00+08:00'};
          const full=await r.plan(input);await r.clear('r0');const created=await r.plan(input);
          return {full,created,count:h.preferences().rendezvousReminders.length,old:h.preferences().rendezvousReminders.some(row=>row.id==='r0'),active:r.active().length};
        }""")
        self.assertEqual(result, {"full": {"ok": False, "reason": "limit"}, "created": {"ok": True, "id": "plan:synthetic-a:new"}, "count": 50, "old": False, "active": 50})

    def test_history_change_invalidates_queued_reminder_edits_and_keeps_imported_rows(self):
        result = self.page.evaluate("""async()=>{
          const h=featureHarness(),r=h.reminders();let release;
          h.controls.preferenceGate=new Promise(resolve=>release=resolve);
          const first=r.plan({characterId:'synthetic-a',locationId:'archive',requestId:'first',remindAt:'2026-10-19T12:01:00+08:00'});
          const queued=r.plan({characterId:'synthetic-a',locationId:'archive',requestId:'queued',remindAt:'2026-10-19T12:02:00+08:00'});
          await h.flush();h.replacePreferences({rendezvousReminders:[reminder('imported',{rendezvousId:null})]});h.history();release();
          const results=await Promise.allSettled([first,queued]);return {results:results.map(row=>row.status==='rejected'?'rejected':row.value),
            ids:r.active().map(row=>row.id),events:h.controls.events.length,changes:h.controls.changes.length};
        }""")
        self.assertEqual(result, {"results": ["rejected", {"ok": False, "reason": "readonly"}], "ids": ["imported"], "events": 0, "changes": 0})

    def test_backup_import_only_today_union_preserves_existing_fields_and_does_not_import_read_state(self):
        result = self.page.evaluate("""()=>{
          const current={rendezvousReminders:[reminder('same',{locationName:'current',extension:{keep:true}})],reminderPreferences:{enabled:false,current:1},
            favoriteLines:[{messageId:'m',blockIndex:0,start:0,end:1,characterId:'a',text:'字'}],deletedMemoryIds:['old'],unknown:{keep:1}};
          const incoming={rendezvousReminders:[reminder('same',{locationName:'imported'}),reminder('today'),
            reminder('yesterday',{dayKey:'2026-10-18',remindAt:'2026-10-18T12:01:00+08:00'}),
            reminder('tomorrow',{dayKey:'2026-10-20',remindAt:'2026-10-20T12:01:00+08:00'})],reminderPreferences:{enabled:true,imported:2},deletedMemoryIds:['new']};
          const merged=featureModule.mergeFeaturePreferences(current,incoming,{dayKey:'2026-10-19'});
          return {ids:merged.rendezvousReminders.map(row=>row.id).sort(),same:merged.rendezvousReminders.find(row=>row.id==='same'),prefs:merged.reminderPreferences,
            favorites:merged.favoriteLines.length,deleted:merged.deletedMemoryIds,unknown:merged.unknown,readKeys:Object.keys(merged).filter(key=>key.startsWith('stage_read'))};
        }""")
        self.assertEqual(result["ids"], ["same", "today"])
        self.assertEqual(result["same"]["locationName"], "current")
        self.assertEqual(result["same"]["extension"], {"keep": True})
        self.assertEqual(result["prefs"], {"enabled": False, "current": 1, "imported": 2})
        self.assertEqual({key: result[key] for key in ("favorites", "deleted", "unknown", "readKeys")},
                         {"favorites": 1, "deleted": ["new", "old"], "unknown": {"keep": 1}, "readKeys": []})

    def test_reading_preferences_backup_round_trip_excludes_tab_auto_and_applies_after_explicit_import(self):
        result = self.page.evaluate("""async()=>{
          const source=featureHarness(),reading=source.reading();await reading.ready;
          await reading.setPreferences({textSpeed:'slow',autoDelay:'long',fastForwardRead:false});reading.setAuto(true);
          const records=[{key:'ui_preferences',unknown:{keep:true},readingPreferences:{auto:true,credential:'synthetic-secret'}}];
          const backup=featureModule.portableFeatureState(records,source.adapter.localPreferences),imported=backup.find(row=>row.key==='ui_preferences');
          const target=featureHarness(),other=target.reading();await other.ready;await other.setPreferences({textSpeed:'fast',autoDelay:'short'});
          const before=other.preferences();target.replacePreferences(featureModule.mergeFeaturePreferences({readingPreferences:before},imported));
          await other.importPreferences(imported.readingPreferences);const recreated=target.reading();await recreated.ready;
          return {backup:imported,before,after:other.preferences(),recreated:recreated.preferences(),auto:other.autoEnabled(),tab:[...target.tab.keys()],
            autoExported:JSON.stringify(backup).includes('"auto"'),secret:JSON.stringify(backup).includes('synthetic-secret'),original:records[0].readingPreferences};
        }""")
        expected = {"textSpeed": "slow", "autoDelay": "long", "fastForwardRead": False}
        self.assertEqual(result, {"backup": {"key": "ui_preferences", "unknown": {"keep": True}, "readingPreferences": expected},
                                  "before": {"textSpeed": "fast", "autoDelay": "short", "fastForwardRead": True}, "after": expected, "recreated": expected,
                                  "auto": False, "tab": [], "autoExported": False, "secret": False,
                                  "original": {"auto": True, "credential": "synthetic-secret"}})

    def test_history_clear_or_older_backup_cannot_reset_local_reading_preferences_or_auto(self):
        result = self.page.evaluate("""async()=>{
          const h=featureHarness(),r=h.reading();await r.ready;await r.setPreferences({textSpeed:'fast',autoDelay:'long',fastForwardRead:false});r.setAuto(true);
          h.replacePreferences({readingPreferences:{textSpeed:'normal',autoDelay:'normal',fastForwardRead:true}});h.history();await h.flush();
          const afterHistory=r.preferences();await r.importPreferences(undefined);const older=r.preferences();
          await r.importPreferences({textSpeed:'invalid',autoDelay:'invalid',fastForwardRead:'false',auto:false});const invalid=r.preferences();
          await r.importPreferences({textSpeed:'instant',auto:false,credential:'synthetic-secret'});
          return {afterHistory,older,invalid,partial:r.preferences(),auto:r.autoEnabled(),stored:JSON.parse(h.local.get('project-snow-public:reading:v1'))};
        }""")
        expected = {"textSpeed": "fast", "autoDelay": "long", "fastForwardRead": False}
        partial = {**expected, "textSpeed": "instant"}
        self.assertEqual(result, {"afterHistory": expected, "older": expected, "invalid": expected, "partial": partial, "auto": True, "stored": partial})

    def test_reading_import_before_module_install_and_export_without_existing_ui_state(self):
        result = self.page.evaluate("""async()=>{
          const h=featureHarness();const value={textSpeed:'slow',autoDelay:'short',fastForwardRead:false,auto:true,unknown:'ignored'};
          const imported=featureModule.importReadingPreferences(h.adapter.localPreferences,value),r=h.reading();await r.ready;
          const backup=featureModule.portableFeatureState([{key:'stage_read_v1:synthetic-a',messages:[]}],h.adapter.localPreferences);
          return {imported,preferences:r.preferences(),auto:r.autoEnabled(),backup,tab:[...h.tab.keys()]};
        }""")
        expected = {"textSpeed": "slow", "autoDelay": "short", "fastForwardRead": False}
        self.assertEqual(result, {"imported": expected, "preferences": expected, "auto": False,
                                  "backup": [{"key": "ui_preferences", "readingPreferences": expected}], "tab": []})

    def test_composed_logic_ready_and_dispose_cover_child_records_and_timers_without_dom_or_network(self):
        result = self.page.evaluate("""async()=>{
          const msg=speech('one'),h=featureHarness({messages:[msg],state:{rendezvous:world()},preferences:{rendezvousReminders:[reminder()]}}),logic=h.install();
          await logic.ready;const fetch=window.fetch,create=document.createElement;window.fetch=()=>{throw new Error('logic_network_forbidden');};
          document.createElement=()=>{throw new Error('logic_dom_forbidden');};
          try{await logic.reading.markRead('one',0,4);await logic.reminders.start();const before=h.timers().length;
            logic.dispose();await h.tick(120000);return {before,after:h.timers().length,events:h.controls.events.length,read:logic.reading.readUpTo('one',0),enabled:logic.reading.enabled()};}
          finally{window.fetch=fetch;document.createElement=create;}
        }""")
        self.assertEqual(result, {"before": 1, "after": 0, "events": 0, "read": 0, "enabled": False})
