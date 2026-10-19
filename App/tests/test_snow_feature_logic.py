"""Pure local feature contracts exercised by a real browser with synthetic data."""
from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless

from tests.test_public_frontend_e2e import _launch_browser

PUBLIC = Path(__file__).resolve().parents[1] / "public_frontend"
RUN_PUBLIC_E2E = os.getenv("RUN_PUBLIC_E2E") == "1"
PIN_LIGHT_TONE = "localStorage.setItem('project-snow-public:tone','light')"

HARNESS = r"""async () => {
  window.featureModule=await import('/modules/snow-feature-logic.js');
  window.harness=(initial={})=>{
    const clone=value=>structuredClone(value);
    const state={writerOwned:true,busy:false,historyEpoch:0,localDayKey:'2026-10-19',movementCatalog:[{location_id:'archive',display_name:'资料室'}],
      ...clone(initial.state||{})};
    const controls={busyIds:new Set(),writes:[],dialogueGate:null,preferenceGate:null,allMessagesGate:null,
      allMessagesSnapshot:null,failDialogue:false,guardDepth:0};
    state.busyFor=id=>state.busy||controls.busyIds.has(id);
    let prefs=clone(initial.preferences||{}),records=clone(initial.records||{}),all=clone(initial.messages||[]);
    const listeners=new Map();
    const adapter={
      snapshot:()=>({...clone({...state,busyFor:undefined}),busyFor:state.busyFor}),
      preferences:()=>clone(prefs),
      async savePreferences(patch){controls.writes.push(['preferences',clone(patch)]);
        if(controls.preferenceGate)await controls.preferenceGate;
        prefs={...prefs,...clone(patch)};},
      async loadDialogue(id){if(controls.dialogueGate)await controls.dialogueGate;return clone(records[id]||{});},
      async saveDialogue(id,record){controls.writes.push(['dialogue',id,clone(record)]);
        if(controls.failDialogue)throw new Error('synthetic_disk_failure');records[id]=clone(record);},
      async getMessage(id){return clone(all.find(message=>message.id===id));},
      async messages(id){return clone(all.filter(message=>message.characterId===id));},
      async allMessages(){const values=clone(controls.allMessagesSnapshot||all);if(controls.allMessagesGate)await controls.allMessagesGate;return values;},
      on(type,listener){if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(listener);
        return()=>listeners.get(type).delete(listener);},
    };
    const h={state,controls,adapter,record:id=>clone(records[id]),preferences:()=>clone(prefs),
      replaceMessages:values=>{all=clone(values);},
      replacePreferences:value=>{prefs=clone(value);},
      replaceRecords:value=>{records=clone(value);},
      emitHistory(){for(const listener of listeners.get('history-changed')||[])listener();},
      emit(message){for(const listener of listeners.get('message-saved')||[])listener(clone(message));},
      emitUsage(receipt){for(const listener of listeners.get('usage-saved')||[])listener(clone(receipt));},
      install(){h.logic=featureModule.createFeatureLogic(adapter);return h.logic;}};
    return h;
  };
  window.receipt=(id,date='2026-10-19T12:00:00+08:00')=>({memory:{memory_id:id,kind:'shared_scene_event',value:'合成记忆'+id,
    occurred_at:date,location_id:'archive',scene_epoch:4,source_message_ids:['synthetic-source']},proof:'synthetic-proof-'+id,extension:{keep:true}});
  window.message=(id,patch={})=>({id,characterId:'synthetic-a',role:'assistant',status:'sent',requestId:'request-'+id,
    createdAt:Date.parse('2026-10-19T12:00:00+08:00'),communicationChannel:'in_person',
    contentBlocks:[{type:'speech',text:'合成台词 ＡＢＣ123 abc'}],usage:{provider_calls:1,total_tokens:10},...patch});
}"""


class FeatureModuleHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/":
            body, kind = b"<!doctype html><title>synthetic feature logic</title>", "text/html"
        elif self.path in {"/modules/snow-feature-logic.js", "/modules/snow-reading-logic.js", "/modules/snow-reminder-logic.js"}:
            body, kind = (PUBLIC / self.path.lstrip("/")).read_bytes(), "text/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@skipUnless(RUN_PUBLIC_E2E, "browser feature logic runs in the UI/full CI tier")
class SnowFeatureLogicTests(TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FeatureModuleHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.playwright = sync_playwright().start()
        cls.browser = _launch_browser(cls.playwright)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.page = self.browser.new_page(timezone_id="Asia/Hong_Kong")
        self.page.add_init_script(PIN_LIGHT_TONE)
        self.errors = []
        self.unexpected = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

        def guard(route):
            if route.request.method != "GET" or not route.request.url.startswith(self.base_url + "/"):
                self.unexpected.append("unexpected_network")
                route.abort()
            else:
                route.continue_()

        self.page.route("**/*", guard)
        self.page.goto(self.base_url)
        self.page.evaluate(HARNESS)

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected, [])

    def test_memory_tombstone_precedes_record_and_undo_preserves_signed_receipt(self):
        result = self.page.evaluate("""async()=>{
          const original=receipt('old','2026-10-18T12:00:00+08:00'), recent=receipt('new');
          const h=harness({records:{'synthetic-a':{key:'dialogue_context_v1:synthetic-a',sessionContextPackage:'opaque',memories:[original,recent]}}});
          const logic=h.install();await logic.ready;
          const before=await logic.memories.list('synthetic-a');before[0].sourceMessageIds.push('mutated');
          const removed=await logic.memories.remove('synthetic-a','old');
          const hidden=await logic.memories.count('synthetic-a'),deleted=h.preferences().deletedMemoryIds;
          const restored=await logic.memories.restore('synthetic-a','old');
          const after=h.record('synthetic-a');
          return {beforeIds:before.map(item=>item.id),removed,hidden,deleted,restored,
            receiptEqual:JSON.stringify(after.memories.find(item=>item.memory.memory_id==='old'))===JSON.stringify(original),
            package:after.sessionContextPackage,ids:h.preferences().deletedMemoryIds,writes:h.controls.writes.map(item=>item[0]),
            sources:(await logic.memories.list('synthetic-a'))[0].sourceMessageIds,
            location:(await logic.memories.list('synthetic-a'))[0].locationName,
            noProof:!(await logic.memories.list('synthetic-a')).some(item=>'proof'in item)};
        }""")
        self.assertEqual(result, {"beforeIds": ["new", "old"], "removed": {"ok": True}, "hidden": 1,
                                  "deleted": ["old"], "restored": {"ok": True}, "receiptEqual": True,
                                  "package": "opaque", "ids": [], "writes": ["preferences", "dialogue", "dialogue", "preferences"],
                                  "sources": ["synthetic-source"], "location": "资料室", "noProof": True})

    def test_memory_busy_before_and_after_load_and_writer_ownership_reject_without_writes(self):
        result = self.page.evaluate("""async()=>{
          const results=[];
          for(const mode of ['busy','load-race','readonly']){
            const h=harness({records:{'synthetic-a':{memories:[receipt('one')]}}});const logic=h.install();await logic.ready;
            let release;
            if(mode==='busy')h.controls.busyIds.add('synthetic-a');
            if(mode==='readonly')h.state.writerOwned=false;
            if(mode==='load-race')h.controls.dialogueGate=new Promise(resolve=>release=resolve);
            const pending=logic.memories.remove('synthetic-a','one');
            if(release){await Promise.resolve();await Promise.resolve();h.controls.busyIds.add('synthetic-a');release();}
            results.push({mode,result:await pending,writes:h.controls.writes.length,count:h.record('synthetic-a').memories.length});
          }
          return results;
        }""")
        self.assertEqual(result, [{"mode": mode, "result": {"ok": False, "reason": "busy"}, "writes": 0, "count": 1}
                                  for mode in ("busy", "load-race", "readonly")])

    def test_memory_failure_stays_filtered_and_undo_expires_with_dialog(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({records:{'synthetic-a':{memories:[receipt('one')]}}});const logic=h.install();await logic.ready;
          h.controls.failDialogue=true;let failed=false;try{await logic.memories.remove('synthetic-a','one');}catch{failed=true;}
          const hidden=await logic.memories.count('synthetic-a');h.controls.failDialogue=false;
          const restored=await logic.memories.restore('synthetic-a','one');
          await logic.memories.remove('synthetic-a','one');logic.memories.clearUndo('synthetic-a');
          return {failed,hidden,restored,expired:await logic.memories.restore('synthetic-a','one'),
            filtered:featureModule.filterDeletedMemories([receipt('one'),receipt('two')],h.preferences().deletedMemoryIds).map(item=>item.memory.memory_id)};
        }""")
        self.assertEqual(result, {"failed": True, "hidden": 0, "restored": {"ok": True}, "expired": {"ok": False}, "filtered": ["two"]})

    def test_memory_write_guard_covers_both_writes_and_requests_for_other_characters(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({records:{'synthetic-a':{memories:[receipt('one'),receipt('two')]}}});
          h.controls.busyIds.add('synthetic-b');let seen=[];
          h.adapter.withMemoryWrite=async(id,operation)=>{h.controls.guardDepth++;try{return await operation();}finally{h.controls.guardDepth--;}};
          for(const name of ['savePreferences','saveDialogue']){const old=h.adapter[name];h.adapter[name]=async(...args)=>{seen.push(h.controls.guardDepth);return old(...args);};}
          const logic=h.install();await logic.ready;
          const results=await Promise.all([logic.memories.remove('synthetic-a','one'),logic.memories.remove('synthetic-a','two')]);
          return {results,seen,ids:h.preferences().deletedMemoryIds,count:await logic.memories.count('synthetic-a')};
        }""")
        self.assertEqual(result, {"results": [{"ok": True}, {"ok": True}], "seen": [1, 1, 1, 1], "ids": ["one", "two"], "count": 0})

    def test_memory_busy_during_tombstone_write_rolls_back_without_losing_old_ids(self):
        result = self.page.evaluate("""async()=>{
          const previous=Array.from({length:500},(_,i)=>'old-'+i),h=harness({preferences:{deletedMemoryIds:previous},
            records:{'synthetic-a':{memories:[receipt('new')]}}});const logic=h.install();await logic.ready;
          let release;h.controls.preferenceGate=new Promise(resolve=>release=resolve);
          const operation=logic.memories.remove('synthetic-a','new');
          await Promise.resolve();await Promise.resolve();await Promise.resolve();h.controls.busyIds.add('synthetic-a');release();
          const result=await operation;return {result,unchanged:JSON.stringify(h.preferences().deletedMemoryIds)===JSON.stringify(previous),
            record:h.record('synthetic-a').memories.length};
        }""")
        self.assertEqual(result, {"result": {"ok": False, "reason": "busy"}, "unchanged": True, "record": 1})

    def test_favorite_canonical_anchor_survives_display_reflow_and_original_cleanup(self):
        result = self.page.evaluate("""async()=>{
          const original=message('line',{contentBlocks:[{type:'action',text:'合成动作'},{type:'speech',text:'原文里的稳定台词'}],
            displayBlocks:[{type:'speech',text:'完全不同的排版'}],locationName:'资料室'});
          const h=harness({messages:[original]}),logic=h.install();await logic.ready;
          const ref={messageId:'line',blockIndex:1,start:3,end:7};
          const on=await logic.favorites.toggle(ref),has=logic.favorites.has(ref),first=logic.favorites.list();
          first[0].text='changed';h.replaceMessages([]);const preserved=logic.favorites.list();
          const off=await logic.favorites.toggle(ref);
          return {on,has,off,preserved:preserved.map(({id,text,channel,locationName})=>({id,text,channel,locationName})),count:logic.favorites.list().length};
        }""")
        self.assertEqual(result, {"on": True, "has": True, "off": False,
                                  "preserved": [{"id": "line:1:3-7", "text": "的稳定台", "channel": "in_person", "locationName": "资料室"}], "count": 0})

    def test_favorites_reject_unfinished_overlong_invalid_and_split_surrogate_anchors(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({messages:[message('pending',{status:'pending'}),message('failed',{status:'failed'}),
            message('long',{contentBlocks:[{type:'speech',text:'字'.repeat(241)}]}),
            message('emoji',{contentBlocks:[{type:'speech',text:'甲😀乙'}]})]});const logic=h.install();await logic.ready;
          const refs=[['pending',0,2],['failed',0,2],['long',0,241],['emoji',1,2],['emoji',2,3],['emoji',-1,2],['emoji',0,99]];
          const rejected=[];for(const [messageId,start,end]of refs)rejected.push(await logic.favorites.toggle({messageId,blockIndex:0,start,end}));
          const valid=await logic.favorites.toggle({messageId:'emoji',blockIndex:0,start:1,end:3});
          return {rejected,valid,text:logic.favorites.list()[0].text};
        }""")
        self.assertEqual(result, {"rejected": [False] * 7, "valid": True, "text": "😀"})

    def test_concurrent_favorites_preferences_and_limits_keep_existing_snapshots(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({messages:[message('one'),message('two')]});const logic=h.install();await logic.ready;
          await Promise.all(['one','two'].map(messageId=>logic.favorites.toggle({messageId,blockIndex:0,start:0,end:2})));
          const counts=logic.favorites.list().length;const preferences=await logic.usage.setPreferences({showUsage:false});
          const favorites=Array.from({length:205},(_,index)=>({id:'ignored',characterId:'synthetic-a',messageId:'old-'+index,
            blockIndex:0,start:0,end:1,text:'字',createdAt:index+1,extension:{keep:true}}));
          const normalized=featureModule.normalizeFeaturePreferences({favoriteLines:favorites,deletedMemoryIds:Array.from({length:510},(_,i)=>'memory-'+i)});
          return {counts,preferences,stillThere:logic.favorites.list().length,maxFavorites:normalized.favoriteLines.length,
            newest:normalized.favoriteLines[0].messageId,oldest:normalized.favoriteLines.at(-1).messageId,
            maxDeleted:normalized.deletedMemoryIds.length,firstDeleted:normalized.deletedMemoryIds[0]};
        }""")
        self.assertEqual(result, {"counts": 2, "preferences": {"showUsage": False}, "stillThere": 2,
                                  "maxFavorites": 200, "newest": "old-204", "oldest": "old-5", "maxDeleted": 500, "firstDeleted": "memory-10"})

    def test_history_clear_or_import_cancels_favorite_waiting_for_original_message(self):
        result = self.page.evaluate("""async()=>{
          const results=[];
          for(const mode of ['clear','import']){
            const original=message('old'),h=harness({messages:[original]}),logic=h.install();await logic.ready;
            let release,started;const gate=new Promise(resolve=>release=resolve),entered=new Promise(resolve=>started=resolve);
            h.adapter.getMessage=async()=>{started();await gate;return structuredClone(original);};
            const operation=logic.favorites.toggle({messageId:'old',blockIndex:0,start:0,end:2});await entered;
            const imported={characterId:'synthetic-a',messageId:'imported',blockIndex:0,start:0,end:2,text:'导入',createdAt:3};
            h.replaceMessages(mode==='import'?[message('imported')]:[]);
            h.replacePreferences(mode==='import'?{favoriteLines:[imported],usagePreferences:{showUsage:false}}:{});
            h.emitHistory();release();
            results.push({mode,current:await operation,favorites:logic.favorites.list().map(item=>item.messageId),
              messages:(await h.adapter.allMessages()).map(item=>item.id),writes:h.controls.writes.length});
          }
          return results;
        }""")
        self.assertEqual(result, [{"mode": "clear", "current": False, "favorites": [], "messages": [], "writes": 0},
                                  {"mode": "import", "current": False, "favorites": ["imported"], "messages": ["imported"], "writes": 0}])

    def test_history_epoch_invalidates_queued_favorites_memories_and_preferences_at_invocation(self):
        result = self.page.evaluate("""async()=>{
          const original=message('old'),h=harness({messages:[original],records:{'synthetic-a':{memories:[receipt('old-memory')]}}}),
            logic=h.install();await logic.ready;let release,started;
          const gate=new Promise(resolve=>release=resolve),entered=new Promise(resolve=>started=resolve);
          h.adapter.getMessage=async()=>{started();await gate;return structuredClone(original);};
          const favorite=logic.favorites.toggle({messageId:'old',blockIndex:0,start:0,end:2});await entered;
          const preference=logic.usage.setPreferences({showUsage:false}),memory=logic.memories.remove('synthetic-a','old-memory');
          // The host increments this synchronously, before its later history-changed event.
          h.state.historyEpoch++;h.replacePreferences({usagePreferences:{showUsage:true,imported:true}});h.replaceRecords({});h.replaceMessages([]);
          release();return {favorite:await favorite,preference:await preference,memory:await memory,
            writes:h.controls.writes.length,records:h.record('synthetic-a'),preferences:h.preferences()};
        }""")
        self.assertEqual(result, {"favorite": False, "preference": {"showUsage": True, "imported": True}, "memory": {"ok": False},
                                  "writes": 0, "records": None, "preferences": {"usagePreferences": {"showUsage": True, "imported": True}}})

    def test_history_replacement_cancels_memory_loads_and_preserves_imported_records(self):
        result = self.page.evaluate("""async()=>{
          const results=[];
          for(const restoring of [false,true]){
            const h=harness({records:{'synthetic-a':{memories:[receipt('old-memory')]}}}),logic=h.install();await logic.ready;
            if(restoring)await logic.memories.remove('synthetic-a','old-memory');h.controls.writes=[];
            const original=h.record('synthetic-a');let release,started;
            const gate=new Promise(resolve=>release=resolve),entered=new Promise(resolve=>started=resolve);
            h.adapter.loadDialogue=async()=>{started();await gate;return structuredClone(original);};
            const operation=logic.memories[restoring?'restore':'remove']('synthetic-a','old-memory');await entered;
            h.replaceRecords({'synthetic-a':{memories:[receipt('imported-memory')]}});
            h.replacePreferences({deletedMemoryIds:['imported-tombstone']});h.state.historyEpoch++;h.emitHistory();release();
            results.push({restoring,result:await operation,writes:h.controls.writes.length,
              ids:h.record('synthetic-a').memories.map(item=>item.memory.memory_id),deleted:h.preferences().deletedMemoryIds});
          }
          return results;
        }""")
        self.assertEqual(result, [{"restoring": restoring, "result": {"ok": False}, "writes": 0,
                                  "ids": ["imported-memory"], "deleted": ["imported-tombstone"]} for restoring in (False, True)])

    def test_history_clear_after_tombstone_save_never_rolls_old_data_back_into_new_history(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({records:{'synthetic-a':{memories:[receipt('old-memory')]}}}),logic=h.install();await logic.ready;
          let release,started;const gate=new Promise(resolve=>release=resolve),entered=new Promise(resolve=>started=resolve),save=h.adapter.savePreferences;
          h.adapter.savePreferences=async patch=>{await save(patch);started();await gate;};
          const operation=logic.memories.remove('synthetic-a','old-memory');await entered;
          h.replaceRecords({});h.replacePreferences({deletedMemoryIds:['new-history']});h.state.historyEpoch++;h.emitHistory();release();
          return {result:await operation,record:h.record('synthetic-a'),deleted:h.preferences().deletedMemoryIds,
            writes:h.controls.writes.map(item=>item[0])};
        }""")
        self.assertEqual(result, {"result": {"ok": False}, "record": None, "deleted": ["new-history"], "writes": ["preferences"]})

    def test_history_import_during_restore_does_not_remove_imported_tombstones(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({records:{'synthetic-a':{memories:[receipt('old-memory')]}}}),logic=h.install();await logic.ready;
          await logic.memories.remove('synthetic-a','old-memory');h.controls.writes=[];
          let release,started;const gate=new Promise(resolve=>release=resolve),entered=new Promise(resolve=>started=resolve),save=h.adapter.saveDialogue;
          h.adapter.saveDialogue=async(...args)=>{await save(...args);started();await gate;};
          const operation=logic.memories.restore('synthetic-a','old-memory');await entered;
          h.replaceRecords({'synthetic-a':{memories:[receipt('imported-memory')]}});
          h.replacePreferences({deletedMemoryIds:['old-memory','imported-tombstone']});h.state.historyEpoch++;h.emitHistory();release();
          return {result:await operation,ids:h.record('synthetic-a').memories.map(item=>item.memory.memory_id),
            deleted:h.preferences().deletedMemoryIds,writes:h.controls.writes.map(item=>item[0])};
        }""")
        self.assertEqual(result, {"result": {"ok": False}, "ids": ["imported-memory"],
                                  "deleted": ["old-memory", "imported-tombstone"], "writes": ["dialogue"]})

    def test_history_clear_discards_pending_memory_and_transcript_reads(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({messages:[message('old')],records:{'synthetic-a':{memories:[receipt('old-memory')]}}}),logic=h.install();await logic.ready;
          const oldRecord=h.record('synthetic-a'),oldMessages=await h.adapter.messages('synthetic-a');let release,started,count=0;
          const gate=new Promise(resolve=>release=resolve),entered=new Promise(resolve=>started=resolve),enter=()=>{if(++count===2)started();};
          h.adapter.loadDialogue=async()=>{enter();await gate;return oldRecord;};
          h.adapter.messages=async()=>{enter();await gate;return oldMessages;};
          const memories=logic.memories.list('synthetic-a'),transcript=logic.transcript.search('synthetic-a','合成');await entered;
          h.replaceRecords({});h.replaceMessages([]);h.state.historyEpoch++;h.emitHistory();release();
          return {memories:await memories,transcript:await transcript};
        }""")
        self.assertEqual(result, {"memories": [], "transcript": []})

    def test_search_reads_all_history_and_maps_case_fullwidth_and_utf16_offsets(self):
        result = self.page.evaluate("""async()=>{
          const history=Array.from({length:100},(_,i)=>message('recent-'+i,{contentBlocks:[{type:'speech',text:'普通合成台词'}]}));
          history.unshift(message('oldest',{createdAt:1,contentBlocks:[{type:'action',text:'😀 ＡｂＣ１２３ abc123'},
            {type:'speech',text:'İ İ'}],displayBlocks:[{type:'speech',text:'not canonical'}]}));
          const h=harness({messages:history}),logic=h.install();await logic.ready;
          const rows=await logic.transcript.search('synthetic-a','aBC123');
          const dotted=await logic.transcript.search('synthetic-a','i');
          rows[0].ranges[0][0]=99;const fresh=await logic.transcript.search('synthetic-a','ＡＢＣ１２３');
          return {ids:fresh.map(item=>item.messageId),ranges:fresh[0].ranges,text:fresh[0].text,
            dotted:dotted[0].ranges,empty:await logic.transcript.search('synthetic-a','  '),none:await logic.transcript.search('synthetic-b','abc')};
        }""")
        self.assertEqual(result, {"ids": ["oldest"], "ranges": [[3, 9], [10, 16]], "text": "😀 ＡｂＣ１２３ abc123",
                                  "dotted": [[0, 1], [2, 3]], "empty": [], "none": []})

    def test_search_limits_results_and_keeps_canonical_block_indices(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({messages:Array.from({length:240},(_,i)=>message('line-'+i,{createdAt:i+1,
            contentBlocks:[{type:'sticker',caption:'needle'},{type:'speech',text:'needle needle'}]}))});
          const logic=h.install();await logic.ready;const short=await logic.transcript.search('synthetic-a','needle',{limit:2});
          return {max:(await logic.transcript.search('synthetic-a','needle',{limit:999})).length,
            zero:(await logic.transcript.search('synthetic-a','needle',{limit:0})).length,
            short:short.map(item=>({messageId:item.messageId,blockIndex:item.blockIndex,ranges:item.ranges}))};
        }""")
        self.assertEqual(result, {"max": 200, "zero": 0, "short": [
            {"messageId": "line-239", "blockIndex": 1, "ranges": [[0, 6], [7, 13]]},
            {"messageId": "line-238", "blockIndex": 1, "ranges": [[0, 6], [7, 13]]}]})

    def test_usage_groups_reply_summary_and_arrival_and_deduplicates_replays(self):
        result = self.page.evaluate("""async()=>{
          const extra={kind:'summary',requestId:'summary-1',provider_calls:1,total_tokens:5,model:'synthetic-summary'};
          const h=harness({messages:[message('user',{role:'user',requestId:'chat-1',usage:null}),
            message('reply',{requestId:'chat-1',usage:{provider_calls:2,total_tokens:20,model:'synthetic-reply'},extraUsage:[extra,extra]}),
            message('replay',{requestId:'chat-1',usage:{provider_calls:2,total_tokens:20,model:'synthetic-reply'},extraUsage:[extra]}),
            message('arrival',{source:'presence_arrival',usage:{provider_calls:1,total_tokens:7}})]});
          const logic=h.install();await logic.ready;const round=logic.usage.round('user');round.parts[0].tokens=999;
          return {round:logic.usage.round('reply'),arrival:logic.usage.round('arrival'),today:logic.usage.today()};
        }""")
        self.assertEqual(result, {"round": {"calls": 3, "tokens": 25, "partial": False, "parts": [
            {"kind": "reply", "calls": 2, "tokens": 20, "model": "synthetic-reply"},
            {"kind": "summary", "calls": 1, "tokens": 5, "model": "synthetic-summary"}]},
            "arrival": {"calls": 1, "tokens": 7, "partial": False, "parts": [
                {"kind": "arrival", "calls": 1, "tokens": 7, "model": ""}]},
            "today": {"calls": 4, "tokens": 32, "partial": False}})

    def test_arrival_kind_survives_scene_placeholder_primary_key_order_after_reload(self):
        result = self.page.evaluate("""async()=>{
          const requestId='join-request',placeholder=message('reply:'+requestId,{requestId,source:'scene_action',usage:{provider_calls:0}}),
            arrival=message('presence_message_'+requestId,{requestId,source:'presence_arrival',
              usage:{provider_calls:1,total_tokens:42,model:'synthetic-arrival'}});
          const live=harness({messages:[placeholder]}),logic=live.install();await logic.ready;
          live.emit(arrival);const before=logic.usage.round(placeholder.id),todayBefore=logic.usage.today();
          // getAll on the messages store returns primary keys in ascending order.
          const stored=[placeholder,arrival].sort((a,b)=>a.id<b.id?-1:a.id>b.id?1:0);
          live.replaceMessages(stored);await logic.refresh();
          const reloaded=harness({messages:stored}),reloadedLogic=reloaded.install();await reloadedLogic.ready;
          return {order:stored.map(item=>item.source),before,afterRefresh:logic.usage.round(placeholder.id),
            afterReload:reloadedLogic.usage.round(arrival.id),todayBefore,todayAfter:reloadedLogic.usage.today()};
        }""")
        round_usage = {"calls": 1, "tokens": 42, "partial": False, "parts": [
            {"kind": "arrival", "calls": 1, "tokens": 42, "model": "synthetic-arrival"}]}
        self.assertEqual(result, {"order": ["presence_arrival", "scene_action"], "before": round_usage,
                                  "afterRefresh": round_usage, "afterReload": round_usage,
                                  "todayBefore": {"calls": 1, "tokens": 42, "partial": False},
                                  "todayAfter": {"calls": 1, "tokens": 42, "partial": False}})

    def test_usage_unknown_tokens_zero_placeholders_and_partial_totals_are_not_fabricated(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({messages:[message('unknown',{usage:{provider_calls:1,total_tokens:0},extraUsage:[
            {kind:'summary',requestId:'summary-known',provider_calls:1,total_tokens:9}]}),
            message('none',{usage:{provider_calls:2}}),message('placeholder',{usage:{provider_calls:0}}),
            message('legacy',{usage:{total_tokens:4}})]});const logic=h.install();await logic.ready;
          return {unknown:logic.usage.round('unknown'),none:logic.usage.round('none'),
            zero:logic.usage.round('placeholder'),legacy:logic.usage.round('legacy'),today:logic.usage.today()};
        }""")
        self.assertEqual(result["unknown"]["calls"], 2)
        self.assertEqual(result["unknown"]["tokens"], 9)
        self.assertTrue(result["unknown"]["partial"])
        self.assertIsNone(result["none"]["tokens"])
        self.assertFalse(result["none"]["partial"])
        self.assertEqual(result["zero"], {"calls": 0, "tokens": 0, "partial": False,
                                          "parts": [{"kind": "reply", "calls": 0, "tokens": 0, "model": ""}]})
        self.assertEqual(result["legacy"]["calls"], None)
        self.assertTrue(result["legacy"]["partial"])
        self.assertEqual(result["today"], {"calls": 4, "tokens": 13, "partial": True})

    def test_usage_hong_kong_day_and_live_saved_message_win_over_initial_snapshot(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({messages:[message('before',{createdAt:Date.parse('2026-10-18T15:59:59Z')}),
            message('start',{createdAt:Date.parse('2026-10-18T16:00:00Z')}),message('end',{createdAt:Date.parse('2026-10-19T16:00:00Z')}),
            message('updated')]});let release;h.controls.allMessagesGate=new Promise(resolve=>release=resolve);
          const logic=h.install();await Promise.resolve();
          h.emit(message('updated',{usage:{provider_calls:2,total_tokens:50}}));release();await logic.ready;
          const today=logic.usage.today();h.emit(message('new',{usage:{provider_calls:1,total_tokens:3}}));
          const next=logic.usage.today();logic.dispose();h.emit(message('ignored'));
          return {today,next,disposed:logic.usage.today()};
        }""")
        self.assertEqual(result, {"today": {"calls": 3, "tokens": 60, "partial": False},
                                  "next": {"calls": 4, "tokens": 63, "partial": False},
                                  "disposed": {"calls": 0, "tokens": 0, "partial": False}})

    def test_usage_cross_midnight_reply_and_summary_belong_only_to_user_send_day(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({messages:[
            message('user',{role:'user',usage:null,requestId:'cross-midnight',createdAt:Date.parse('2026-10-19T23:59:58+08:00')}),
            message('reply',{requestId:'cross-midnight',createdAt:Date.parse('2026-10-20T00:00:02+08:00'),
              usage:{provider_calls:2,total_tokens:20},extraUsage:[{kind:'summary',requestId:'summary-after-midnight',
                provider_calls:1,total_tokens:5,createdAt:Date.parse('2026-10-20T00:00:05+08:00')}]}),
            message('replay',{requestId:'cross-midnight',createdAt:Date.parse('2026-10-20T00:01:00+08:00'),
              usage:{provider_calls:2,total_tokens:20}})]});
          const logic=h.install();await logic.ready;
          h.state.localDayKey='2026-10-19';const sentDay=logic.usage.today();
          h.state.localDayKey='2026-10-20';const replyDay=logic.usage.today();
          return {sentDay,replyDay,round:logic.usage.round('reply')};
        }""")
        self.assertEqual(result, {"sentDay": {"calls": 3, "tokens": 25, "partial": False},
                                  "replyDay": {"calls": 0, "tokens": 0, "partial": False},
                                  "round": {"calls": 3, "tokens": 25, "partial": False, "parts": [
                                      {"kind": "reply", "calls": 2, "tokens": 20, "model": ""},
                                      {"kind": "summary", "calls": 1, "tokens": 5, "model": ""}]}})

    def test_usage_arrival_uses_earliest_own_timestamp_and_user_time_takes_precedence(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({messages:[
            message('arrival',{source:'presence_arrival',requestId:'arrival-midnight',createdAt:Date.parse('2026-10-19T23:59:58+08:00')}),
            message('arrival-replay',{source:'presence_arrival',requestId:'arrival-midnight',createdAt:Date.parse('2026-10-20T00:00:02+08:00')}),
            message('invalid-time',{createdAt:1e30}),
            message('skewed-assistant',{requestId:'user-priority',createdAt:Date.parse('2026-10-19T23:59:59+08:00')}),
            message('user-priority',{role:'user',usage:null,requestId:'user-priority',createdAt:Date.parse('2026-10-20T00:00:01+08:00')})]});
          const logic=h.install();await logic.ready;
          h.state.localDayKey='2026-10-19';const previous=logic.usage.today();
          h.state.localDayKey='2026-10-20';const next=logic.usage.today();
          return {previous,next};
        }""")
        self.assertEqual(result, {"previous": {"calls": 1, "tokens": 10, "partial": False},
                                  "next": {"calls": 1, "tokens": 10, "partial": False}})

    def test_history_refresh_forgets_deleted_records_and_stale_reads_cannot_restore_them(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({messages:[message('old')],records:{'synthetic-a':{memories:[receipt('one')]}}});
          const logic=h.install();await logic.ready;await logic.memories.remove('synthetic-a','one');
          let firstRelease;h.controls.allMessagesGate=new Promise(resolve=>firstRelease=resolve);
          const first=logic.refresh();await Promise.resolve();h.controls.allMessagesGate=null;h.replaceMessages([message('new')]);
          await logic.refresh();firstRelease();await first;
          const afterRefresh=logic.usage.today(),expiredUndo=await logic.memories.restore('synthetic-a','one');
          h.replaceMessages([]);h.emitHistory();await Promise.resolve();await Promise.resolve();await Promise.resolve();
          return {afterRefresh,old:logic.usage.round('old'),expiredUndo,afterClear:logic.usage.today()};
        }""")
        self.assertEqual(result, {"afterRefresh": {"calls": 1, "tokens": 10, "partial": False},
                                  "old": {"calls": 0, "tokens": 0, "partial": False, "parts": []},
                                  "expiredUndo": {"ok": False}, "afterClear": {"calls": 0, "tokens": 0, "partial": False}})

    def test_usage_empty_replay_cannot_erase_known_receipt_or_count_summary_twice(self):
        result = self.page.evaluate("""async()=>{
          const extra={kind:'summary',requestId:'same-summary',provider_calls:1,total_tokens:3};
          const h=harness({messages:[message('original',{requestId:'shared',extraUsage:[extra]}),
            message('empty-replay',{requestId:'shared',usage:{},extraUsage:[{kind:'summary',requestId:'same-summary'}]}),
            message('other',{extraUsage:[extra]})]});const logic=h.install();await logic.ready;
          return {round:logic.usage.round('empty-replay'),today:logic.usage.today()};
        }""")
        self.assertEqual(result, {"round": {"calls": 2, "tokens": 13, "partial": False,
                                           "parts": [{"kind": "reply", "calls": 1, "tokens": 10, "model": ""},
                                                     {"kind": "summary", "calls": 1, "tokens": 3, "model": ""}]},
                                  "today": {"calls": 3, "tokens": 23, "partial": False}})

    def test_backup_union_keeps_unknown_fields_and_extra_usage_receipts(self):
        result = self.page.evaluate("""async()=>{
          const favorite=(name,text)=>({characterId:'synthetic-a',messageId:name,blockIndex:0,start:0,end:1,text,createdAt:2});
          const current={favoriteLines:[favorite('same','本'),favorite('local','地')],deletedMemoryIds:['local-memory'],
            usagePreferences:{showUsage:false,unknown:1},localExtension:{keep:true}};
          const incoming={favoriteLines:[favorite('same','入'),favorite('imported','导')],deletedMemoryIds:['imported-memory','local-memory'],
            remoteExtension:{keep:true}};
          const merged=featureModule.mergeFeaturePreferences(current,JSON.parse(JSON.stringify(incoming)));
          const normalized=featureModule.normalizeFeaturePreferences(JSON.parse(JSON.stringify(merged)));
          const extra=featureModule.mergeExtraUsage([{kind:'summary',requestId:'s',total_tokens:4,prompt_tokens:2,extension:{keep:true}}],
            [{kind:'summary',request_id:'s2',provider_calls:1},{kind:'summary',requestId:'s',total_tokens:5,prompt_tokens:3,extension:{keep:true}}]);
          return {ids:normalized.favoriteLines.map(item=>item.messageId).sort(),same:normalized.favoriteLines.find(item=>item.messageId==='same').text,
            deleted:normalized.deletedMemoryIds,usage:normalized.usagePreferences,local:normalized.localExtension,remote:normalized.remoteExtension,
            extra,defaultUsage:featureModule.normalizeFeaturePreferences().usagePreferences,
            importedFalse:featureModule.mergeFeaturePreferences({}, {usagePreferences:{showUsage:false}}).usagePreferences.showUsage};
        }""")
        self.assertEqual(result, {"ids": ["imported", "local", "same"], "same": "本", "deleted": ["imported-memory", "local-memory"],
                                  "usage": {"showUsage": False, "unknown": 1}, "local": {"keep": True}, "remote": {"keep": True},
                                  "extra": [{"kind": "summary", "requestId": "s", "total_tokens": 5, "prompt_tokens": 3, "extension": {"keep": True}},
                                            {"kind": "summary", "request_id": "s2", "requestId": "s2", "provider_calls": 1}],
                                  "defaultUsage": {"showUsage": True}, "importedFalse": False})

    def test_readonly_preferences_and_dom_network_free_operations(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({messages:[message('line')],state:{writerOwned:false}}),logic=h.install();await logic.ready;
          const previousFetch=window.fetch,previousCreate=document.createElement;
          window.fetch=()=>{throw new Error('feature_network_forbidden');};document.createElement=()=>{throw new Error('feature_dom_forbidden');};
          try{return {favorite:await logic.favorites.toggle({messageId:'line',blockIndex:0,start:0,end:2}),
            preferences:await logic.usage.setPreferences({showUsage:false}),rows:(await logic.transcript.search('synthetic-a','合成')).length,
            usage:logic.usage.today(),writes:h.controls.writes.length};}
          finally{window.fetch=previousFetch;document.createElement=previousCreate;}
        }""")
        self.assertEqual(result, {"favorite": False, "preferences": {"showUsage": True}, "rows": 1,
                                  "usage": {"calls": 1, "tokens": 10, "partial": False}, "writes": 0})

    def test_readonly_existing_favorite_reports_unchanged_current_state(self):
        result = self.page.evaluate("""async()=>{
          const ref={messageId:'line',blockIndex:0,start:0,end:2};
          const h=harness({state:{writerOwned:false},preferences:{favoriteLines:[{
            ...ref,characterId:'synthetic-a',text:'合成',createdAt:1}]}}),logic=h.install();await logic.ready;
          return {current:await logic.favorites.toggle(ref),has:logic.favorites.has(ref),writes:h.controls.writes.length};
        }""")
        self.assertEqual(result, {"current": True, "has": True, "writes": 0})

    def test_message_copy_discards_reads_after_history_replacement_or_disposal(self):
        result = self.page.evaluate("""async()=>{
          const results=[];
          for(const mode of ['epoch','history','dispose','normal']){
            const h=harness({messages:[message('line')]}),logic=h.install();await logic.ready;
            let release;h.adapter.getMessage=()=>new Promise(resolve=>release=()=>resolve(message('line')));
            const pending=logic.messages.get('line');
            if(mode==='epoch')h.state.historyEpoch++;
            if(mode==='history')h.emitHistory();
            if(mode==='dispose')logic.dispose();
            release();const value=await pending;
            if(value)value.contentBlocks[0].text='changed copy';
            results.push({mode,id:value?.id||null,original:h.record('none')||null});
          }
          return results;
        }""")
        self.assertEqual(result, [{"mode": mode, "id": "line" if mode == "normal" else None, "original": None}
                                  for mode in ("epoch", "history", "dispose", "normal")])

    def test_proactive_failed_unknown_receipts_count_without_messages_and_completed_receipt_deduplicates(self):
        result = self.page.evaluate("""async()=>{
          const receipt=(id,usage,status='completed',extra={})=>({requestId:id,characterId:'synthetic-a',createdAt:Date.parse('2026-10-19T12:00:00+08:00'),
            source:'proactive_reminder',model:'synthetic-model',usage,status,...extra});
          const rows=[receipt('complete',{provider_calls:1,total_tokens:10}),receipt('failed',{provider_calls:1},'failed'),
            receipt('unknown',{provider_calls:1,total_tokens:3},'unknown'),receipt('not-called',{provider_calls:0,total_tokens:999},'failed'),
            receipt('yesterday',{provider_calls:1,total_tokens:100},'completed',{createdAt:Date.parse('2026-10-18T12:00:00+08:00')})];
          const h=harness({messages:[message('complete',{source:'proactive_reminder',requestId:'complete',usage:{provider_calls:1,total_tokens:5}})],
            preferences:{proactiveUsage:[rows[0],rows[1]]}});h.adapter.usageReceipts=async()=>rows;
          const logic=h.install();await logic.ready;const before=logic.usage.today(),round=logic.usage.round('complete');
          h.emitUsage(receipt('complete',{}));return {before,after:logic.usage.today(),round};
        }""")
        self.assertEqual(result, {"before": {"calls": 3, "tokens": 13, "partial": True}, "after": {"calls": 3, "tokens": 13, "partial": True},
                                  "round": {"calls": 1, "tokens": 10, "partial": False,
                                            "parts": [{"kind": "proactive", "calls": 1, "tokens": 10, "model": "synthetic-model"}]}})

    def test_live_usage_wins_stale_initial_read_and_history_replacement_discards_stale_receipts(self):
        result = self.page.evaluate("""async()=>{
          const value=(id,tokens)=>({requestId:id,characterId:'synthetic-a',createdAt:Date.parse('2026-10-19T12:00:00+08:00'),
            usage:{provider_calls:1,total_tokens:tokens},status:'unknown'}),h=harness();let release;
          h.adapter.usageReceipts=()=>new Promise(resolve=>release=()=>resolve([value('a',5)]));const logic=h.install();
          await Promise.resolve();await Promise.resolve();h.emitUsage(value('a',9));release();await logic.ready;const before=logic.usage.today();
          let oldRelease;h.adapter.usageReceipts=()=>new Promise(resolve=>oldRelease=()=>resolve([value('stale',100)]));const old=logic.refresh();
          await Promise.resolve();await Promise.resolve();h.adapter.usageReceipts=async()=>[];h.emitHistory();await logic.refresh();oldRelease();await old;
          return {before,after:logic.usage.today()};
        }""")
        self.assertEqual(result, {"before": {"calls": 1, "tokens": 9, "partial": False}, "after": {"calls": 0, "tokens": 0, "partial": False}})

    def test_completed_proactive_arrival_after_midnight_uses_original_receipt_day(self):
        result = self.page.evaluate("""async()=>{
          const createdAt=Date.parse('2026-10-18T23:59:00+08:00'),row={requestId:'late',characterId:'synthetic-a',createdAt,
            usage:{provider_calls:1,total_tokens:8},status:'completed'};
          const h=harness({messages:[message('late',{requestId:'late',source:'proactive_reminder'})]});h.adapter.usageReceipts=async()=>[row];
          const logic=h.install();await logic.ready;const today=logic.usage.today();h.state.localDayKey='2026-10-18';return {today,originalDay:logic.usage.today()};
        }""")
        self.assertEqual(result, {"today": {"calls": 0, "tokens": 0, "partial": False}, "originalDay": {"calls": 1, "tokens": 10, "partial": False}})

    def test_proactive_portable_ledger_whitelists_cost_fields_without_request_envelope_or_read_marks(self):
        result = self.page.evaluate("""()=>{
          const base={requestId:'paid',characterId:'synthetic-a',createdAt:Date.parse('2026-10-19T12:00:00+08:00'),source:'ignored',
            model:'synthetic-model',usage:{provider_calls:1,total_tokens:6,prompt_tokens:2,completion_tokens:4,credential:'synthetic-secret'},modelCalled:true,status:'failed'};
          const records=[{key:'drafts',values:{a:'合成草稿'}},{key:'preferences',autoSummaryEnabled:true},
            {key:'ui_preferences',unknown:{keep:true},proactiveUsage:[{...base,usage:{provider_calls:1,total_tokens:3}}]},
            {...base,key:'proactive_usage_v1:paid',envelope:{credential:'synthetic-secret',body:'不得导出'},proof:'private'},
            {key:'proactive_request_v1:paid',envelope:{credential:'synthetic-secret'}},{key:'stage_read_v1:synthetic-a',messages:[]},
            {key:'proactive_consent_v1',receipt:'private'}];
          const portable=featureModule.portableFeatureState(records),ui=portable.find(item=>item.key==='ui_preferences');
          const merged=featureModule.mergeFeaturePreferences(ui,{proactiveUsage:[{...base,requestId:'imported'},base]});
          return {keys:portable.map(item=>item.key),row:ui.proactiveUsage[0],count:ui.proactiveUsage.length,unknown:ui.unknown,
            ids:merged.proactiveUsage.map(item=>item.requestId).sort(),secret:JSON.stringify(portable).includes('synthetic-secret'),
            originalEnvelope:records[3].envelope.body};
        }""")
        self.assertEqual(result, {"keys": ["drafts", "preferences", "ui_preferences"], "row": {
            "requestId": "paid", "characterId": "synthetic-a", "createdAt": 1792382400000, "source": "proactive_reminder", "model": "synthetic-model",
            "usage": {"provider_calls": 1, "input_tokens": None, "output_tokens": None, "prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6},
            "modelCalled": True, "status": "failed"}, "count": 1, "unknown": {"keep": True}, "ids": ["imported", "paid"], "secret": False,
            "originalEnvelope": "不得导出"})
