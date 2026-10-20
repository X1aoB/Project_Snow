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
  renderStageSpeech,recordReplyPaint,speechTypewriterKey,presentAssistantTurn,saveCredential,clearCredential,
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
  for(const type of ['selected-changed','channel-changed','message-saved','reply-presented','world-changed','tone-changed','history-changed',
      'rendezvous-settled','reminders-changed','reminder-due'])
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

        for pattern in ("snow-feature*.js", "snow-reading-logic.js", "snow-reminder-logic.js", "snow-proactive-client.js"):
            page.route(self.base_url + "/modules/" + pattern, feature_asset)
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
                         ["/modules/snow-feature-logic.js", "/modules/snow-feature-runtime.js", "/modules/snow-features.js",
                          "/modules/snow-proactive-client.js", "/modules/snow-reading-logic.js", "/modules/snow-reminder-logic.js"])
        self.assertTrue(all(item["settled"] for item in self.feature_loads))
        page.evaluate("() => {window.__featureProbe.syncInterfaceTone(true);window.dispatchEvent(new Event('focus'));}")
        self.assertEqual(page.evaluate("window.__featureInstallCount"), 1)
        page.close()
        self.open_feature_page(enabled=False)
        self.assertEqual(self.feature_loads, [])
        self.assertEqual(self.page.evaluate("typeof window.__featureHost"), "undefined")

    def test_real_model_a_b_a_and_credential_save_or_clear_invalidate_paid_confirmation(self):
        page = self.open_feature_page(configured=True)
        consents = []

        def consent_fixture(route):
            self.assertEqual(route.request.method, "POST")
            payload = route.request.post_data_json
            consents.append({"model": payload["model"], "session": payload["model_session_id"],
                             "accepted": payload["accepted_cost_notice"]})
            route.fulfill(json={"consent_token": "synthetic-host-consent", "notice_version": "proactive-cost-1",
                                "model_session_id": payload["model_session_id"], "expires_at": "2099-01-01T00:00:00Z"})

        page.route(self.base_url + "/public/v2/rendezvous/consent", consent_fixture)
        page.evaluate("""()=>{window.__featureProbe.state.config.proactive_messages={enabled:true,notice_version:'proactive-cost-1'};
          window.__featureHost.open.settings('models');const select=document.querySelector('#discovered-models');
          select.replaceChildren(new Option('合成模型 A','gpt-e2e'),new Option('合成模型 B','synthetic-model-b'));
          select.value='gpt-e2e';select.hidden=false;}""")

        def enable():
            value = page.evaluate("window.__featureHost.logic.proactive.enable({acceptedCostNotice:true})")
            self.assertTrue(value["ok"])
            self.assertTrue(value["enabled"])
            self.assertFalse(value["requiresConfirmation"])

        def assert_confirmation_required():
            value = page.evaluate("window.__featureHost.logic.proactive.preferences()")
            self.assertFalse(value["enabled"])
            self.assertTrue(value["requiresConfirmation"])

        enable()
        page.locator("#discovered-models").select_option("synthetic-model-b")
        self.assertEqual(page.evaluate("window.__featureProbe.state.model"), "synthetic-model-b")
        assert_confirmation_required()
        page.locator("#discovered-models").select_option("gpt-e2e")
        self.assertEqual(page.evaluate("window.__featureProbe.state.model"), "gpt-e2e")
        assert_confirmation_required()
        self.assertEqual(len(consents), 1)
        enable()
        self.assertNotEqual(consents[0]["session"], consents[1]["session"])
        page.evaluate("window.__featureProbe.saveCredential()")
        assert_confirmation_required()
        enable()
        page.evaluate("window.__featureProbe.clearCredential()")
        assert_confirmation_required()
        self.assertFalse(page.evaluate("window.__featureHost.snapshot().modelReady"))
        self.assertIsNone(page.evaluate("sessionStorage.getItem('project-snow-public:byok')"))
        self.assertEqual([row["model"] for row in consents], ["gpt-e2e"] * 3)
        self.assertTrue(all(row["accepted"] is True for row in consents))
        self.assertEqual(len({row["session"] for row in consents}), 3)
        self.assertNotIn(("POST", "/public/v2/rendezvous/message"), self.http)
        self.assertEqual(self.rows(), [])

    def test_first_short_page_records_effective_paint_and_next_request_replaces_unfinished_paging(self):
        page = self.open_feature_page(configured=True)
        page.locator("#go-in-person").click()
        page.locator("#confirm-presence-transition").click()
        page.locator('#chat-app[data-channel="in_person"]').wait_for(state="visible")
        page.locator("#presence-arrival-loading").wait_for(state="hidden")
        page.evaluate("""async()=>{
          const h=window.__featureHost,node=h.stage.speechNode(),pager={state:null,advance:null};
          await h.logic.reading.setPreferences({textSpeed:'instant'});
          h.stage.setPager({paginate(text){return [{start:0,end:3},{start:3,end:text.length}];},
            render(state){pager.state=structuredClone(state);const current=state.pages[state.index];
              node.textContent=state.text.slice(current.start,Math.min(current.end,state.revealedEnd));},
            onAdvance(callback){pager.advance=callback;return()=>{pager.advance=null;};},
            onToggleAuto(){return()=>{};}});window.__syntheticPager=pager;
        }""")

        def long_reply(route):
            response = route.fetch()
            text = route.request.post_data_json["chat"]["message"] + "。这是一段比第一页更长的合成台词，用来验证仍未翻完时可以继续发送。"
            lines = []
            for line in response.text().splitlines():
                if line.startswith("data: "):
                    payload = json.loads(line[6:])
                    if "content_blocks" in payload:
                        payload["content_blocks"] = [{"type": "speech", "text": text}]
                    line = "data: " + json.dumps(payload, ensure_ascii=False)
                lines.append(line)
            route.fulfill(content_type="text/event-stream", body="\n".join(lines) + "\n\n")

        page.route(self.base_url + "/public/v2/chat/stream", long_reply)
        self.send("合成分页第一轮")
        request_id = self.rows()[-1]["payload"]["chat"]["request_id"]
        page.wait_for_function("id=>Boolean(window.__featureProbe.state.replyTimingByRequest.get(id)?.firstEffectiveAt)", arg=request_id)
        first = page.evaluate("""id=>{const p=window.__featureProbe,h=window.__featureHost,pager=window.__syntheticPager;
          const message=p.currentThread().messages.find(row=>row.requestId===id&&row.role==='assistant');
          const key=p.speechTypewriterKey(message);window.__oldPagingFinished=h.logic.reading.wait(key);
          return {firstEffectiveAt:p.state.replyTimingByRequest.get(id).firstEffectiveAt,stage:h.stage.speechNode().textContent,
            whole:message.contentBlocks[0].text,page:pager.state.index,pages:pager.state.pages.length,
            active:h.logic.reading.active(),inputDisabled:document.querySelector('#message-input').disabled,
            sendDisabled:document.querySelector('#send-message').disabled,
            presented:window.__featureEvents.filter(row=>row.type==='reply-presented'&&row.detail.requestId===id).length};
        }""", request_id)
        self.assertGreater(first["firstEffectiveAt"], 0)
        self.assertEqual(first["stage"], first["whole"][:3])
        self.assertGreater(len(first["whole"]), len(first["stage"]))
        self.assertEqual((first["page"], first["pages"], first["presented"]), (0, 2, 1))
        self.assertTrue(first["active"])
        self.assertFalse(first["inputDisabled"] or first["sendDisabled"])
        self.send("合成分页第二轮")
        second_id = self.rows()[-1]["payload"]["chat"]["request_id"]
        self.assertNotEqual(second_id, request_id)
        result = page.evaluate("""async id=>({oldCompleted:await window.__oldPagingFinished,
          active:window.__featureHost.logic.reading.active(),page:window.__syntheticPager.state.index,
          text:window.__syntheticPager.state.text,disabled:document.querySelector('#message-input').disabled,
          request:window.__featureProbe.currentThread().messages.filter(row=>row.role==='assistant').at(-1).requestId})""", second_id)
        self.assertFalse(result["oldCompleted"])
        self.assertTrue(result["active"])
        self.assertEqual(result["page"], 0)
        self.assertTrue(result["text"].startswith("合成分页第二轮"))
        self.assertEqual(result["request"], second_id)
        self.assertFalse(result["disabled"])

    def test_real_invitation_uuid_binds_only_its_plan_and_cancel_or_join_ends_reminder(self):
        page = self.open_feature_page(configured=True)
        decoy = page.evaluate("""async()=>{const p=window.__featureProbe,now=Date.now(),end=Date.parse(p.featureSnapshot().localDayKey+'T23:59:59.999+08:00');
          window.__reminderAt=new Date(now+Math.min(60000,(end-now)/2)).toISOString();
          return window.__featureHost.logic.reminders.plan({characterId:p.state.selected,locationId:'base_restaurant',
            remindAt:window.__reminderAt,requestId:'00000000-0000-4000-8000-000000000301'});}
        """)
        self.assertTrue(decoy["ok"])
        plans = []

        def plan_for_actual_request(route):
            request_id = route.request.post_data_json["chat"]["request_id"]
            plans.append(page.evaluate("""async requestId=>window.__featureHost.logic.reminders.plan({
              characterId:window.__featureProbe.state.selected,locationId:'base_restaurant',remindAt:window.__reminderAt,requestId})""", request_id))
            route.continue_()

        page.route(self.base_url + "/public/v2/chat/stream", plan_for_actual_request)
        self.send("合成邀请去餐厅")
        first_id = self.rows()[-1]["payload"]["chat"]["request_id"]
        settled = page.evaluate("window.__featureEvents.find(row=>row.type==='rendezvous-settled'&&row.detail.status==='character_waiting')?.detail")
        self.assertIsNotNone(settled)
        self.assertEqual(settled["requestId"], first_id)
        page.wait_for_function("id=>window.__featureHost.logic.reminders.active().some(row=>row.pendingRequestId===id&&row.rendezvousId===id+':waiting')", arg=first_id)
        first = page.evaluate("""async id=>{const p=window.__featureProbe,rows=(await p.storeGet('app_state','ui_preferences')).rendezvousReminders;
          return {bound:rows.find(row=>row.pendingRequestId===id),decoy:rows.find(row=>row.pendingRequestId!==id),
            settled:window.__featureEvents.find(row=>row.type==='rendezvous-settled'&&row.detail.requestId===id)?.detail};}""", first_id)
        self.assertEqual(first["bound"]["rendezvousId"], first_id + ":waiting")
        self.assertIsNone(first["decoy"]["rendezvousId"])
        self.assertEqual(first["decoy"]["status"], "scheduled")
        self.assertEqual(first["settled"]["status"], "character_waiting")
        self.assertEqual(first["settled"]["rendezvous"]["rendezvous_id"], first_id + ":waiting")
        page.locator('[data-current-rendezvous="cancel"]').click()
        page.wait_for_function("document.querySelector('#current-rendezvous').hidden")
        page.wait_for_function("id=>!window.__featureHost.logic.reminders.active().some(row=>row.pendingRequestId===id)", arg=first_id)
        self.send("合成邀请再次去餐厅")
        second_id = self.rows()[-1]["payload"]["chat"]["request_id"]
        page.wait_for_function("id=>window.__featureHost.logic.reminders.active().some(row=>row.pendingRequestId===id&&row.rendezvousId===id+':waiting')", arg=second_id)
        self.send("我过去了")
        page.locator('#chat-app[data-channel="in_person"]').wait_for(state="visible")
        page.wait_for_function("id=>!window.__featureHost.logic.reminders.active().some(row=>row.pendingRequestId===id)", arg=second_id)
        final = page.evaluate("""async()=>({rows:(await window.__featureProbe.storeGet('app_state','ui_preferences')).rendezvousReminders,
          settled:window.__featureEvents.filter(row=>row.type==='rendezvous-settled').map(row=>row.detail)})""")
        self.assertTrue(all(plan["ok"] for plan in plans))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual([row["status"] for row in final["rows"] if row["pendingRequestId"] in {first_id, second_id}], ["done", "done"])
        scene_rows = [row for row in MapHandler.requests if row["path"].endswith("/scene/action")]
        for action, rendezvous_id in (("cancel_rendezvous", first_id), ("join_rendezvous", second_id)):
            row = next(row for row in scene_rows if row["payload"]["action"] == action)
            self.assertEqual(row["payload"]["rendezvous_id"], rendezvous_id + ":waiting")
            self.assertTrue(any(event["requestId"] == row["payload"]["request_id"] for event in final["settled"]))

    def test_reminder_transaction_abort_emits_nothing_and_preserves_scheduled_until_durable_retry(self):
        page = self.open_feature_page(configured=True)
        self.send("合成邀请去餐厅")
        result = page.evaluate("""async()=>{
          const p=window.__featureProbe,h=window.__featureHost,logic=h.logic.reminders;
          await logic.stop();const pending=h.snapshot().rendezvous.items[0],now=Date.now();
          const end=Date.parse(h.snapshot().localDayKey+'T23:59:59.999+08:00');
          const planned=await logic.set({characterId:p.state.selected,rendezvousId:pending.rendezvous_id,
            remindAt:new Date(now+Math.min(60000,(end-now)/2)).toISOString()});
          const rows=p.state.featurePreferences.rendezvousReminders.map(row=>row.id===planned.id?{...row,remindAt:new Date(now-1000).toISOString()}:row);
          await p.saveUiPreferences({rendezvousReminders:rows});
          const original=IDBObjectStore.prototype.put;let aborted=0;
          IDBObjectStore.prototype.put=function(value,...rest){const request=original.call(this,value,...rest);
            if(this.name==='app_state'&&value.key==='ui_preferences'&&value.rendezvousReminders?.some(row=>row.id===planned.id&&row.status==='fired')){
              aborted++;this.transaction.abort();}return request;};
          let failed;try{failed=await logic.check().then(()=>false,()=>true);}finally{IDBObjectStore.prototype.put=original;}
          const before={persisted:(await p.storeGet('app_state','ui_preferences')).rendezvousReminders.find(row=>row.id===planned.id).status,
            memory:logic.active().find(row=>row.id===planned.id)?.status,
            events:window.__featureEvents.filter(row=>row.type==='reminder-due').length};
          await logic.check();return {planned,aborted,failed,before,
            after:(await p.storeGet('app_state','ui_preferences')).rendezvousReminders.find(row=>row.id===planned.id).status,
            events:window.__featureEvents.filter(row=>row.type==='reminder-due').length};
        }""")
        self.assertTrue(result["planned"]["ok"])
        self.assertEqual(result["aborted"], 1)
        self.assertTrue(result["failed"])
        self.assertEqual(result["before"], {"persisted": "scheduled", "memory": "scheduled", "events": 0})
        self.assertEqual((result["after"], result["events"]), ("fired", 1))

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
