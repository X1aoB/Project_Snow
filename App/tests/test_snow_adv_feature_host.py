"""Real app feature-host contracts using only loopback synthetic fixtures."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit

from tests import test_dialogue_v2_frontend as dialogue
from tests import test_public_frontend_e2e as fixture
from tests.test_snow_adv_base_map import MapBrowserFixture, MapHandler, PUBLIC


PROBE = b"""
window.__featureProbe={state,currentThread,normalizeMessage,normalizeThread,dbGetThread,dbPutThread,
  storeGet,storePut,storeAll,loadDialogueRecord,dialogueCandidates,commitDialogueResult,
  featureEmit,featureSnapshot,setChannel,selectCharacter,saveWorldPackage,syncInterfaceTone,
  runAutoSummary,saveUiPreferences,refreshStoredHistory,exportHistory,importHistory,clearAllHistory,
  holdPreferenceWrites(){let release;featurePreferenceWrite=new Promise(resolve=>release=resolve);return release;},
  afterStateReadOnce(key,callback){const original=storeGet;storeGet=async(...args)=>{
    const value=await original(...args);if(args[0]==='app_state'&&args[1]===key){storeGet=original;callback();}return value;
  };return()=>{storeGet=original;};},
  settled:()=>firstScreenSettled};
"""
HOST = """export function installSnowFeatures(host) {
  window.__featureHost=host;
  window.__featureInstallCount=(window.__featureInstallCount||0)+1;
  window.__featureEvents=[];
  for(const type of ['selected-changed','channel-changed','message-saved','reply-presented','world-changed','tone-changed','history-changed'])
    host.on(type,detail=>window.__featureEvents.push({type,detail}));
}"""
SEED_MESSAGES = """async ({count=400}={})=>{
  const p=window.__featureProbe,thread=p.currentThread(),now=Date.now();
  thread.messages=Array.from({length:count},(_,i)=>p.normalizeMessage({
    id:'synthetic-history-'+String(i).padStart(4,'0'),characterId:thread.characterId,
    role:'assistant',status:'sent',requestId:'synthetic-request-'+i,
    createdAt:now-count+i,communicationChannel:'text',conversationSegmentId:thread.conversationSegmentId,
    source:'chat',contentBlocks:[{type:'message',text:i===0?'合成旧台词 ＡＢＣ123':'合成普通台词 '+i}],
    usage:{provider_calls:1,total_tokens:10},model:'synthetic-history-model'}));
  thread.messageCount=count;await p.dbPutThread(thread);
  p.state.threads.delete(thread.characterId);await p.dbGetThread(thread.characterId,40);
  return {characterId:thread.characterId,loaded:p.currentThread().messages.length};
}"""


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser feature-host integration tier")
class SnowAdvFeatureHostTests(MapBrowserFixture, TestCase):
    def tearDown(self):
        try:
            self.assertEqual(self.errors, [])
            self.assertEqual(self.blocked, [])
        finally:
            dialogue.DialogueV2FrontendTests.tearDown(self)

    def open_feature_page(self, *, enabled=True, configured=False):
        page = self.new_page(width=1440, height=900, enabled=enabled,
                             script=(PUBLIC / "app.js").read_bytes() + PROBE)
        page.unroute("**/*")
        allowed_posts = {"/public/v2/scene/action", "/public/v2/chat/stream", "/public/v1/byok/session",
                         "/public/v1/byok/models", "/public/v1/chat/summarize"}

        def guard(route):
            request = route.request
            if not request.url.startswith(self.base_url + "/") or (
                    request.method != "GET" and (request.method != "POST" or urlsplit(request.url).path not in allowed_posts)):
                self.blocked.append("non_fixture_request")
                route.abort()
            else:
                route.fallback()

        page.route("**/*", guard)
        self.feature_loads = []

        def feature_asset(route):
            self.feature_loads.append({"path": urlsplit(route.request.url).path,
                                       "settled": page.evaluate("window.__featureProbe.settled()")})
            if route.request.url.endswith("/snow-features.js"):
                route.fulfill(content_type="text/javascript", body=HOST)
            else:
                route.fallback()

        page.route(self.base_url + "/modules/snow-feature*.js", feature_asset)
        self.open_page(page)
        if enabled:
            page.wait_for_function("Boolean(window.__featureHost)")
            page.evaluate("window.__featureHost.logic.ready")
        else:
            page.wait_for_function("window.__featureProbe.settled()")
        if configured:
            fixture.PublicFrontendE2ETests._configure_model(page)
        return page

    send = dialogue.DialogueV2FrontendTests.send
    local = dialogue.DialogueV2FrontendTests.local

    def rows(self):
        return [row for row in MapHandler.requests if row["path"] == "/public/v2/chat/stream"]

    def test_deferred_entry_loads_once_only_for_snow(self):
        page = self.open_feature_page()
        self.assertEqual(page.evaluate("window.__featureInstallCount"), 1)
        self.assertEqual(sorted(item["path"] for item in self.feature_loads),
                         ["/modules/snow-feature-logic.js", "/modules/snow-features.js"])
        self.assertTrue(all(item["settled"] for item in self.feature_loads))
        page.evaluate("() => {window.__featureProbe.syncInterfaceTone(true);window.dispatchEvent(new Event('focus'));}")
        self.assertEqual(page.evaluate("window.__featureInstallCount"), 1)
        page.close()
        self.open_feature_page(enabled=False)
        self.assertEqual(self.feature_loads, [])
        self.assertEqual(self.page.evaluate("typeof window.__featureHost"), "undefined")

    def test_snapshot_and_real_host_events_are_copies_and_unsubscribe(self):
        page = self.open_feature_page()
        result = page.evaluate("""async other=>{
          const h=window.__featureHost,p=window.__featureProbe,s=h.snapshot();
          const name=s.characters[0].display_name,activity=s.scene.character_activity;
          const character={...s.characters[0],avatar:{src:'synthetic-avatar'}},avatar=h.avatarOf(character);
          avatar.src='tampered';if(character.avatar.src!=='synthetic-avatar')throw new Error('avatar_not_detached');
          s.characters[0].display_name='changed';s.scene.character_activity='changed';
          const independent=h.snapshot().characters[0].display_name===name&&h.snapshot().scene.character_activity===activity;
          let count=0;const off=h.on('channel-changed',value=>{count++;value.channel='tampered';});
          await p.setChannel('in_person');off();await p.setChannel('text');
          await p.selectCharacter(other);p.syncInterfaceTone(true);
          const thread=p.currentThread();thread.messages.push(p.normalizeMessage({id:'synthetic-event',characterId:other,
            role:'assistant',status:'sent',createdAt:Date.now(),requestId:'synthetic-event-request',
            contentBlocks:[{type:'message',text:'合成事件台词'}],usage:{provider_calls:1,total_tokens:2}}));
          await p.dbPutThread(thread);
          return {version:h.version,independent,count,channel:h.snapshot().channel,selected:h.snapshot().selected,
            events:window.__featureEvents.map(value=>value.type),hasBusy:typeof h.snapshot().busyFor==='function',
            message:(await p.storeGet('messages','synthetic-event')).contentBlocks[0].text};
        }""", dialogue.OTHER)
        self.assertEqual(result["version"], 1)
        self.assertTrue(result["independent"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["channel"], "text")
        self.assertEqual(result["selected"], dialogue.OTHER)
        self.assertTrue(result["hasBusy"])
        self.assertEqual(result["message"], "合成事件台词")
        for event in ("selected-changed", "channel-changed", "message-saved", "world-changed", "tone-changed"):
            self.assertIn(event, result["events"])

    def test_deleted_memory_is_filtered_from_request_and_late_server_receipts(self):
        page = self.open_feature_page(configured=True)
        self.send()
        page.wait_for_function("window.__featureEvents.some(item=>item.type==='reply-presented')")
        presented = page.evaluate("window.__featureEvents.find(item=>item.type==='reply-presented').detail")
        request_id = self.rows()[0]["payload"]["chat"]["request_id"]
        self.assertEqual(presented["requestId"], request_id)
        self.assertEqual(presented["messageId"], "reply:" + request_id)
        receipt = page.evaluate("async()=>structuredClone((await window.__featureProbe.loadDialogueRecord(window.__featureProbe.state.selected)).memories[0])")
        memory_id = receipt["memory"]["memory_id"]
        self.assertEqual(page.evaluate("async id=>window.__featureHost.logic.memories.remove(window.__featureProbe.state.selected,id)", memory_id), {"ok": True})

        def repeat_deleted_receipt(route):
            response = route.fetch()
            lines = []
            for line in response.text().splitlines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    if "session_memories" in data:
                        data["session_memories"].append(receipt)
                    line = "data: " + json.dumps(data, ensure_ascii=False)
                lines.append(line)
            route.fulfill(status=200, content_type="text/event-stream", body="\n".join(lines) + "\n\n")

        page.route(self.base_url + "/public/v2/chat/stream", repeat_deleted_receipt)
        self.send("合成删除后的对话")
        self.assertNotIn(memory_id, [item["memory"]["memory_id"] for item in self.rows()[-1]["payload"]["memory_candidates"]])
        result = page.evaluate("""async()=>{const p=window.__featureProbe;return {
          ids:(await p.loadDialogueRecord(p.state.selected)).memories.map(item=>item.memory.memory_id),
          deleted:(await p.storeGet('app_state','ui_preferences')).deletedMemoryIds,
          count:await window.__featureHost.logic.memories.count(p.state.selected)}}""")
        self.assertNotIn(memory_id, result["ids"])
        self.assertIn(memory_id, result["deleted"])
        self.assertEqual(result["count"], 1)

    def test_busy_memory_mutation_cannot_change_recovery_uuid_or_body(self):
        page = self.open_feature_page(configured=True)
        self.send()
        memory_id = page.evaluate("async()=>(await window.__featureHost.logic.memories.list(window.__featureProbe.state.selected))[0].id")
        MapHandler.scenario = "disconnect"
        before = len(self.rows())
        busy_results = []

        def observe_busy(route):
            busy_results.append(page.evaluate("async id=>window.__featureHost.logic.memories.remove(window.__featureProbe.state.selected,id)", memory_id))
            route.continue_()

        page.route(self.base_url + "/public/v2/chat/stream", observe_busy)
        self.send("合成断线恢复对话")
        rows = self.rows()[before:]
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["recovery"])
        self.assertTrue(rows[1]["recovery"])
        self.assertEqual(rows[0]["payload"], rows[1]["payload"])
        self.assertEqual(busy_results, [{"ok": False, "reason": "busy"}] * 2)
        self.assertIn(memory_id, [item["memory"]["memory_id"] for item in rows[0]["payload"]["memory_candidates"]])

    def test_summary_usage_binds_checkpoint_and_requested_model_and_survives_storage(self):
        page = self.open_feature_page(configured=True)
        page.evaluate(SEED_MESSAGES, {"count": 2})
        request_models = []

        def summary(route):
            request_models.append(route.request.post_data_json["model"])
            page.evaluate("""()=>{const p=window.__featureProbe;p.state.model='synthetic-later-model';
              const t=p.currentThread();t.messages.push(p.normalizeMessage({id:'synthetic-later-reply',characterId:t.characterId,
                role:'assistant',status:'sent',createdAt:Date.now(),requestId:'synthetic-later-request',
                conversationSegmentId:t.conversationSegmentId,contentBlocks:[{type:'message',text:'合成较晚回复'}],
                usage:{provider_calls:1,total_tokens:7}}));}""")
            route.fulfill(json={"summary": "合成连续性摘要", "pending_topics": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})

        page.route(self.base_url + "/public/v1/chat/summarize", summary)
        page.evaluate("""()=>{const p=window.__featureProbe,t=p.currentThread();
          t.summaryRequestId='00000000-0000-4000-8000-000000000101';t.summaryCheckpointMessageId='synthetic-history-0000';
          window.__summaryDone=p.runAutoSummary(t);}""")
        page.evaluate("async()=>await window.__summaryDone")
        result = page.evaluate("""async()=>{const p=window.__featureProbe;return {
          checkpoint:await p.storeGet('messages','synthetic-history-0000'),later:await p.storeGet('messages','synthetic-later-reply'),
          today:window.__featureHost.logic.usage.today()};}""")
        self.assertEqual(request_models, ["gpt-e2e"])
        self.assertEqual(result["checkpoint"]["extraUsage"], [{"kind": "summary", "requestId": "00000000-0000-4000-8000-000000000101",
                                                              "provider_calls": 1, "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5, "model": "gpt-e2e"}])
        self.assertEqual(result["later"]["extraUsage"], [])
        self.assertEqual(result["today"], {"calls": 4, "tokens": 32, "partial": False})

    def test_full_history_search_favorite_and_transcript_locate_unloaded_message(self):
        page = self.open_feature_page()
        self.assertEqual(page.evaluate(SEED_MESSAGES, {"count": 400})["loaded"], 40)
        result = page.evaluate("""async()=>{
          const h=window.__featureHost,p=window.__featureProbe,selected=p.state.selected;
          const rows=await h.logic.transcript.search(selected,'abc123');
          const ref={messageId:'synthetic-history-0000',blockIndex:0,start:6,end:12};
          const favorite=await h.logic.favorites.toggle(ref),saved=h.logic.favorites.list()[0];
          const loadedBefore=p.currentThread().messages.length,opened=await h.open.transcript({messageId:rows[0].messageId});
          const node=[...document.querySelectorAll('#transcript-content [data-message-id]')].find(item=>item.dataset.messageId===rows[0].messageId);
          const box=node?.getBoundingClientRect(),panel=document.querySelector('#transcript-content').getBoundingClientRect();
          return {rows:rows.map(item=>({id:item.messageId,ranges:item.ranges})),favorite,saved,loadedBefore,opened,
            visible:Boolean(box&&box.bottom>=panel.top&&box.top<=panel.bottom),favorites:(await p.storeGet('app_state','ui_preferences')).favoriteLines.length};
        }""")
        self.assertEqual(result["rows"], [{"id": "synthetic-history-0000", "ranges": [[6, 12]]}])
        self.assertTrue(result["favorite"])
        self.assertEqual(result["saved"]["text"], "ＡＢＣ123")
        self.assertEqual(result["saved"]["id"], "synthetic-history-0000:0:6-12")
        self.assertEqual(result["loadedBefore"], 40)
        self.assertTrue(result["opened"])
        self.assertTrue(result["visible"])
        self.assertEqual(result["favorites"], 1)

    def test_real_backup_export_import_unions_preferences_and_deduplicates_extra_usage(self):
        page = self.open_feature_page()
        page.evaluate(SEED_MESSAGES, {"count": 2})
        page.evaluate("""async()=>{const p=window.__featureProbe,t=p.currentThread();
          t.messages[0].extraUsage=[{kind:'summary',requestId:'synthetic-summary-one',provider_calls:1,total_tokens:3}];
          await p.dbPutThread(t);await p.saveUiPreferences({deletedMemoryIds:['synthetic-deleted-local'],usagePreferences:{showUsage:false},
            favoriteLines:[{id:'synthetic-history-0000:0:0-2',characterId:t.characterId,messageId:'synthetic-history-0000',
              blockIndex:0,start:0,end:2,text:'合成',createdAt:Date.now(),channel:'text',locationName:''}]});}""")
        with page.expect_download() as download:
            page.evaluate("window.__featureProbe.exportHistory()")
        backup = json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
        self.assertEqual(backup["databaseVersion"], 4)
        imported = json.loads(json.dumps(backup))
        prefs = next(item for item in imported["appState"] if item["key"] == "ui_preferences")
        prefs["deletedMemoryIds"] = ["synthetic-deleted-imported"]
        prefs["favoriteLines"] = [{**prefs["favoriteLines"][0], "id": "synthetic-history-0001:0:0-2", "messageId": "synthetic-history-0001"}]
        for message in imported["messages"]:
            if message["id"] == "synthetic-history-0000":
                message["contentBlocks"] = [{"type": "message", "text": "导入不能覆盖本地正文"}]
                message["extraUsage"].append({"kind": "summary", "requestId": "synthetic-summary-two", "provider_calls": 1, "total_tokens": 4})
        page.evaluate("async backup=>{await window.__featureProbe.importHistory(backup);await window.__featureProbe.importHistory(backup);}", imported)
        result = page.evaluate("""async()=>{const p=window.__featureProbe;return {
          prefs:await p.storeGet('app_state','ui_preferences'),message:await p.storeGet('messages','synthetic-history-0000')};}""")
        self.assertEqual(set(result["prefs"]["deletedMemoryIds"]), {"synthetic-deleted-local", "synthetic-deleted-imported"})
        self.assertEqual({item["messageId"] for item in result["prefs"]["favoriteLines"]}, {"synthetic-history-0000", "synthetic-history-0001"})
        self.assertFalse(result["prefs"]["usagePreferences"]["showUsage"])
        self.assertEqual(len(result["message"]["extraUsage"]), 2)
        self.assertEqual(result["message"]["contentBlocks"][0]["text"], "合成旧台词 ＡＢＣ123")

    def test_history_changed_clears_usage_after_real_history_deletion(self):
        page = self.open_feature_page()
        page.evaluate(SEED_MESSAGES, {"count": 4})
        self.assertEqual(page.evaluate("window.__featureHost.logic.usage.today().calls"), 4)
        page.on("dialog", lambda dialog: dialog.accept())
        page.evaluate("window.__featureProbe.clearAllHistory()")
        page.wait_for_function("window.__featureHost.logic.usage.today().calls===0")
        self.assertEqual(page.evaluate("window.__featureHost.logic.usage.today()"), {"calls": 0, "tokens": 0, "partial": False})
        self.assertIn("history-changed", page.evaluate("window.__featureEvents.map(item=>item.type)"))

    def test_queued_preference_write_cannot_restore_favorites_after_history_clear(self):
        page = self.open_feature_page()
        page.evaluate(SEED_MESSAGES, {"count": 2})
        before = page.evaluate("""()=>{const p=window.__featureProbe;
          window.__releasePreferences=p.holdPreferenceWrites();
          window.__pendingPreferenceSave=p.saveUiPreferences({favoriteLines:[{
            id:'synthetic-history-0000:0:0-2',characterId:p.state.selected,messageId:'synthetic-history-0000',
            blockIndex:0,start:0,end:2,text:'合成待保存旧正文',createdAt:Date.now(),channel:'text',locationName:''}]});
          return {epoch:p.state.historyEpoch,favorites:window.__featureHost.logic.favorites.list().length};
        }""")
        self.assertEqual(before["favorites"], 1)
        page.on("dialog", lambda dialog: dialog.accept())
        page.evaluate("window.__featureProbe.clearAllHistory()")
        result = page.evaluate("""async()=>{
          window.__releasePreferences();await window.__pendingPreferenceSave;
          const p=window.__featureProbe,preferences=await p.storeGet('app_state','ui_preferences');
          return {epoch:p.state.historyEpoch,storedFavorites:preferences?.favoriteLines||[],
            favorites:window.__featureHost.logic.favorites.list(),messages:await p.storeAll('messages'),
            revivedText:JSON.stringify(await p.storeAll('app_state')).includes('合成待保存旧正文')
              ||JSON.stringify(p.state.featurePreferences).includes('合成待保存旧正文')};
        }""")
        self.assertGreater(result["epoch"], before["epoch"])
        self.assertEqual(result["storedFavorites"], [])
        self.assertEqual(result["favorites"], [])
        self.assertEqual(result["messages"], [])
        self.assertFalse(result["revivedText"])

    def preference_import_race(self, when):
        page = self.open_feature_page()
        page.evaluate(SEED_MESSAGES, {"count": 2})
        result = page.evaluate("""async when=>{
          const p=window.__featureProbe,characterId=p.state.selected,epochBefore=p.state.historyEpoch;
          const favorite=(index,text)=>({id:`synthetic-history-000${index}:0:0-2`,characterId,
            messageId:`synthetic-history-000${index}`,blockIndex:0,start:0,end:2,text,
            createdAt:Date.now(),channel:'text',locationName:''});
          await p.saveUiPreferences({favoriteLines:[favorite(0,'本地')],deletedMemoryIds:['synthetic-local-deleted']});
          const backup={schema:'project-snow-history-1',databaseVersion:4,exportedAt:new Date().toISOString(),
            threads:[],messages:[],appState:[{key:'ui_preferences',favoriteLines:[favorite(1,'导入')],
              deletedMemoryIds:['synthetic-imported-deleted']}]};
          let racedSave,hit=0,captured,restore=()=>{};
          const race=()=>{hit++;captured={epoch:p.state.historyEpoch,favorites:p.state.featurePreferences.favoriteLines.map(item=>item.messageId)};
            racedSave=p.saveUiPreferences({usagePreferences:{showUsage:false}});};
          if(when==='merge-transaction'){
            const original=IDBDatabase.prototype.transaction;let armed=true;
            IDBDatabase.prototype.transaction=function(names,mode,...rest){const transaction=original.call(this,names,mode,...rest);
              const stores=typeof names==='string'?[names]:Array.from(names);
              if(armed&&mode==='readwrite'&&['threads','messages','app_state'].every(name=>stores.includes(name))){
                armed=false;queueMicrotask(race);
              }return transaction;
            };restore=()=>{IDBDatabase.prototype.transaction=original;};
          }else restore=p.afterStateReadOnce('preferences',race);
          try{
            await p.importHistory(backup);if(racedSave)await racedSave;
            const persisted=await p.storeGet('app_state','ui_preferences');
            return {hit,captured,epochBefore,
              storedFavorites:persisted.favoriteLines.map(item=>item.messageId).sort(),
              storedDeleted:persisted.deletedMemoryIds.slice().sort(),
              hostFavorites:window.__featureHost.logic.favorites.list().map(item=>item.messageId).sort(),
              stateDeleted:p.state.featurePreferences.deletedMemoryIds.slice().sort()};
          }finally{restore();}
        }""", when)
        self.assertEqual(result["hit"], 1)
        self.assertEqual(result["captured"]["favorites"], ["synthetic-history-0000"])
        if when == "merge-transaction":
            self.assertEqual(result["captured"]["epoch"], result["epochBefore"])
        else:
            self.assertGreater(result["captured"]["epoch"], result["epochBefore"])
        expected_favorites = ["synthetic-history-0000", "synthetic-history-0001"]
        expected_deleted = ["synthetic-imported-deleted", "synthetic-local-deleted"]
        self.assertEqual(result["storedFavorites"], expected_favorites)
        self.assertEqual(result["hostFavorites"], expected_favorites)
        self.assertEqual(result["storedDeleted"], expected_deleted)
        self.assertEqual(result["stateDeleted"], expected_deleted)

    def test_preference_write_during_import_transaction_cannot_erase_backup_union(self):
        self.preference_import_race("merge-transaction")

    def test_preference_write_between_refresh_reads_cannot_erase_backup_union(self):
        self.preference_import_race("refresh-reads")

    def test_concurrent_memory_delete_and_usage_write_keep_tombstone_durable_first(self):
        page = self.open_feature_page(configured=True)
        self.send()
        memory_id = page.evaluate("async()=>(await window.__featureHost.logic.memories.list(window.__featureProbe.state.selected))[0].id")
        page.evaluate("""id=>{
          const p=window.__featureProbe,original=IDBObjectStore.prototype.put;
          const tracker={durable:false,sidecarChecks:[],completedPreferences:0};
          IDBObjectStore.prototype.put=function(value,...rest){
            if(this.name==='app_state'&&value.key==='ui_preferences'){
              const includes=(value.deletedMemoryIds||[]).includes(id);
              this.transaction.addEventListener('complete',()=>{tracker.completedPreferences++;if(includes)tracker.durable=true;},{once:true});
            }
            if(this.name==='app_state'&&value.key==='dialogue_context_v1:'+p.state.selected
              &&!(value.memories||[]).some(receipt=>receipt.memory.memory_id===id))tracker.sidecarChecks.push(tracker.durable);
            return original.call(this,value,...rest);
          };
          tracker.restore=()=>{IDBObjectStore.prototype.put=original;};
          tracker.release=p.holdPreferenceWrites();
          tracker.remove=window.__featureHost.logic.memories.remove(p.state.selected,id);
          tracker.usage=window.__featureHost.logic.usage.setPreferences({showUsage:false});
          window.__tombstoneTracker=tracker;
        }""", memory_id)
        page.wait_for_function("id=>(window.__featureProbe.state.featurePreferences.deletedMemoryIds||[]).includes(id)", arg=memory_id)
        result = page.evaluate("""async id=>{
          const p=window.__featureProbe,tracker=window.__tombstoneTracker;
          // Another app preference save may be enqueued while memory removal
          // awaits its own durable preference write. It must not supersede it.
          const parallel=p.saveUiPreferences({usagePreferences:{showUsage:false}});
          tracker.release();
          try{
            const [removed,usage]=await Promise.all([tracker.remove,tracker.usage,parallel]);
            return {removed,usage,durable:tracker.durable,sidecarChecks:tracker.sidecarChecks,
              completedPreferences:tracker.completedPreferences,
              deleted:(await p.storeGet('app_state','ui_preferences')).deletedMemoryIds,
              remaining:(await p.loadDialogueRecord(p.state.selected)).memories.map(item=>item.memory.memory_id),
              storageAvailable:p.state.storageAvailable};
          }finally{tracker.restore();}
        }""", memory_id)
        self.assertEqual(result["removed"], {"ok": True})
        self.assertEqual(result["usage"], {"showUsage": False})
        self.assertTrue(result["durable"])
        self.assertEqual(result["sidecarChecks"], [True])
        self.assertGreaterEqual(result["completedPreferences"], 2)
        self.assertIn(memory_id, result["deleted"])
        self.assertNotIn(memory_id, result["remaining"])
        self.assertTrue(result["storageAvailable"])
