"""Paid reminder client boundaries in a real browser with synthetic adapters only."""
from __future__ import annotations

import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import TestCase, skipUnless

from tests.test_public_frontend_e2e import _launch_browser


PUBLIC = Path(__file__).resolve().parents[1] / "public_frontend"
HARNESS = r"""async () => {
  const {createProactiveClient}=await import('/modules/snow-proactive-client.js');
  window.deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
  window.harness=(initial={})=>{
    const clone=value=>structuredClone(value),characterId='synthetic-a1',day='2026-10-19';
    const reminder={id:'synthetic-reminder',characterId,rendezvousId:'synthetic-rendezvous',locationId:'archive',dayKey:day,status:'fired'};
    const state={selected:characterId,historyEpoch:0,writerOwned:true,localDayKey:day,
      rendezvous:{authoritative:true,dayKey:day,items:[{character_id:characterId,rendezvous_id:reminder.rendezvousId,
        location_id:reminder.locationId,schedule_date:day}]},...clone(initial.state||{})};
    const context={enabled:true,notice_version:'proactive-cost-1',ready:true,provider:'openai',model:'synthetic-model',
      credential:'synthetic-credential-only',segment_id:'00000000-0000-4000-8000-000000000001',
      state_package:'synthetic-signed-world',session_context_package:'synthetic-signed-dialogue',...clone(initial.context||{})};
    let preferences=clone(initial.preferences||{});
    const records=new Map(Object.entries(clone(initial.records||{}))),requests=[],saves=[],commits=[],events=[],log=[];
    const controls={visible:true,active:[reminder],requestGate:null,requestStarted:null,requestError:false,saveError:'',
      saveHook:null,contextHook:null,contextReads:0,withBusy:false,response:null,consentResponse:null};
    const success=body=>({request_id:body.request_id,character_id:body.character_id,source:'proactive_reminder',
      status:'completed',message_id:'proactive:'+body.request_id,content_blocks:[{type:'message',text:'合成的约定提醒'}],
      usage:{provider_calls:1,prompt_tokens:7,completion_tokens:5,total_tokens:12},model_called:true});
    const adapter={snapshot:()=>clone(state),visible:()=>controls.visible,
      preferences:()=>clone(preferences),async savePreferences(patch){log.push('preferences');preferences={...preferences,...clone(patch)};},
      async context(){controls.contextReads++;await controls.contextHook?.();return clone(context);},activeReminders:()=>clone(controls.active),
      async loadState(key){return clone(records.get(key));},async readRequests(){return clone([...records.values()].filter(record=>record.envelope));},
      async saveState(key,record){saves.push(clone(record));log.push('save:'+record.status);if(controls.saveError===record.status)throw Error('synthetic_disk_failure');
        records.set(key,clone(record));await controls.saveHook?.(record);},
      async withRequest(id,work){return controls.withBusy?{ok:false,reason:'busy'}:work();},
      async request(path,options){const body=JSON.parse(options.body);requests.push({path,body,headers:clone(options.headers||{}),
          persisted:clone([...records.values()])});log.push('request:'+path);controls.requestStarted?.resolve();
        if(path==='/rendezvous/consent')return controls.consentResponse||{consent_token:'synthetic-consent-only',notice_version:'proactive-cost-1',
          model_session_id:body.model_session_id,expires_at:new Date(Date.now()+3600000).toISOString()};
        if(path!=='/rendezvous/message')throw Error('unexpected_synthetic_endpoint');
        if(controls.requestGate)await controls.requestGate.promise;
        if(controls.requestError)throw Error('synthetic_connection_lost');
        return controls.response?{...success(body),...clone(controls.response)}:success(body);},
      async commit(result,record,receipt){commits.push({result:clone(result),record:clone(record),receipt:clone(receipt)});
        log.push('commit');records.set(record.key,clone(record));if(receipt?.key)records.set(receipt.key,clone(receipt));
        if(record.usageReceipt?.key)records.set(record.usageReceipt.key,clone(record.usageReceipt));},
      emit(type,value){events.push([type,clone(value)]);}};
    const h={state,context,reminder,controls,records,requests,saves,commits,events,log,adapter,
      preferences:()=>clone(preferences),newClient(){h.client=createProactiveClient(adapter);return h.client;},
      messageRequests:()=>requests.filter(row=>row.path==='/rendezvous/message'),
      consentRequests:()=>requests.filter(row=>row.path==='/rendezvous/consent')};
    h.newClient();return h;
  };
}"""


class ProactiveClientHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/":
            payload, content_type = b"<!doctype html><title>synthetic proactive client</title>", "text/html"
        elif self.path == "/modules/snow-proactive-client.js":
            payload, content_type = (PUBLIC / self.path.lstrip("/")).read_bytes(), "text/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser paid-client regression tier")
class SnowProactiveClientTests(TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ProactiveClientHandler)
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
        self.errors, self.unexpected = [], []
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

    def test_default_off_requires_exact_confirmation_and_valid_server_model(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({preferences:{proactivePreferences:{enabled:true}}}),initial=h.client.preferences();
          const blocked=[await h.client.send(h.reminder)];
          for(const value of [undefined,false,'true',1])blocked.push(await h.client.enable({acceptedCostNotice:value}));
          const before=h.requests.length,beforeContext=h.controls.contextReads,enabled=await h.client.enable({acceptedCostNotice:true});
          const consent=h.consentRequests()[0];
          const failures=[];for(const context of [{enabled:false},{notice_version:'other'},{ready:false},{credential:''}]){
            const x=harness({context});failures.push({result:await x.client.enable({acceptedCostNotice:true}),requests:x.requests.length});}
          return {initial,before,beforeContext,blocked:blocked.map(row=>row.reason),enabled:enabled.ok,
            consent:{path:consent.path,accepted:consent.body.accepted_cost_notice,notice:consent.body.notice_version},
            failures:failures.map(row=>[row.result.ok,row.requests]),durable:JSON.stringify([...h.records.values(),h.preferences()])};
        }""")
        self.assertFalse(result["initial"]["enabled"])
        self.assertTrue(result["initial"]["requiresConfirmation"])
        self.assertEqual(result["before"], 0)
        self.assertEqual(result["beforeContext"], 0)
        self.assertEqual(result["blocked"], ["cost_confirmation_required"] * 5)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["consent"], {"path": "/rendezvous/consent", "accepted": True, "notice": "proactive-cost-1"})
        self.assertEqual(result["failures"], [[False, 0]] * 4)
        self.assertNotIn("synthetic-credential-only", result["durable"])
        self.assertNotIn("synthetic-consent-only", result["durable"])

    def test_dispatch_is_durable_before_fetch_and_never_generates_twice(self):
        result = self.page.evaluate("""async()=>{
          const h=harness();await h.client.enable({acceptedCostNotice:true});
          const first=await h.client.send(h.reminder),again=await h.client.send(h.reminder),request=h.messageRequests()[0];
          const list=await h.client.requests();list[0].status='mutated';if(list[0].usage)list[0].usage.total_tokens=-1;
          return {first,again,log:h.log,requests:h.messageRequests().length,
            persisted:request.persisted.filter(row=>row.envelope).map(row=>({status:row.status,dispatched:row.dispatched,id:row.envelope.request_id})),
            id:request.body.request_id,commits:h.commits,records:[...h.records.values()],list:await h.client.requests()};
        }""")
        self.assertTrue(result["first"]["ok"])
        self.assertEqual(result["again"]["reason"], "recovery_required")
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["persisted"], [{"status": "pending", "dispatched": True, "id": result["id"]}])
        self.assertLess(result["log"].index("save:prepared"), result["log"].index("save:pending"))
        self.assertLess(result["log"].index("save:pending"), result["log"].index("request:/rendezvous/message"))
        self.assertEqual(result["commits"][0]["result"]["usage"], {"provider_calls": 1, "prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12})
        self.assertEqual(result["list"][0]["status"], "completed")
        self.assertEqual(result["list"][0]["usage"]["total_tokens"], 12)
        self.assertNotIn("envelope", result["list"][0])
        self.assertNotIn("synthetic-credential-only", str(result["records"]))
        self.assertNotIn("synthetic-consent-only", str(result["records"]))

    def test_disk_failure_before_dispatch_prevents_network_and_incomplete_record_cannot_recover(self):
        result = self.page.evaluate("""async()=>{
          const output=[];for(const status of ['prepared','pending']){
            const h=harness();await h.client.enable({acceptedCostNotice:true});h.controls.saveError=status;
            const failure=await h.client.send(h.reminder).then(()=>false,()=>true);
            const record=[...h.records.values()].find(row=>row.envelope);
            const recovery=record?await h.client.recover(record.envelope.request_id):null;
            output.push({failure,network:h.messageRequests().length,recovery});}return output;
        }""")
        self.assertTrue(all(row["failure"] for row in result))
        self.assertEqual([row["network"] for row in result], [0, 0])
        self.assertIsNone(result[0]["recovery"])
        self.assertEqual(result[1]["recovery"]["reason"], "not_dispatched")

    def test_uncertain_outcome_uses_original_envelope_for_read_only_recovery_without_consent(self):
        result = self.page.evaluate("""async()=>{
          const h=harness();await h.client.enable({acceptedCostNotice:true});h.controls.requestError=true;
          const unknown=await h.client.send(h.reminder),again=await h.client.send(h.reminder);
          h.client.dispose();h.newClient();h.controls.requestError=false;
          const recovered=await h.client.recover(unknown.requestId),requests=h.messageRequests();
          const first={...requests[0].body},second={...requests[1].body};
          for(const body of [first,second]){delete body.credential;delete body.consent_token;}
          const done=await h.client.recover(unknown.requestId),missing=await h.client.recover('missing');
          return {unknown,again,recovered,done,missing,requests:requests.length,consents:h.consentRequests().length,
            identical:JSON.stringify(first)===JSON.stringify(second),header:requests[1].headers,
            recoveredCredential:requests[1].body.credential,recoveredConsent:requests[1].body.consent_token,
            committed:h.commits.length};
        }""")
        self.assertEqual(result["unknown"]["reason"], "result_unknown")
        self.assertEqual(result["again"]["reason"], "recovery_required")
        self.assertTrue(result["recovered"]["ok"])
        self.assertTrue(result["done"]["ok"])
        self.assertEqual(result["missing"]["reason"], "missing")
        self.assertEqual((result["requests"], result["consents"], result["committed"]), (2, 1, 1))
        self.assertTrue(result["identical"])
        self.assertEqual(result["header"], {"X-Snow-Recovery-Only": "1"})
        self.assertEqual((result["recoveredCredential"], result["recoveredConsent"]), ("", ""))

    def test_lifecycle_changes_before_fetch_block_paid_calls(self):
        result = self.page.evaluate("""async()=>{
          const output=[];
          for(const status of ['prepared','pending'])for(const mode of ['hidden','writer','history','invalidate','model','credential','server','ready','world']){
            const h=harness();await h.client.enable({acceptedCostNotice:true});
            h.controls.saveHook=record=>{if(record.status!==status)return;
              if(mode==='hidden')h.controls.visible=false;else if(mode==='writer')h.state.writerOwned=false;
              else if(mode==='history')h.state.historyEpoch++;else if(mode==='invalidate')h.client.invalidate();
              else if(mode==='model')h.context.model='synthetic-changed';else if(mode==='credential')h.context.credential='synthetic-changed';
              else if(mode==='server')h.context.enabled=false;else if(mode==='ready')h.context.ready=false;else h.state.rendezvous.items=[];};
            const result=await h.client.send(h.reminder);output.push({status,mode,ok:result.ok,requests:h.messageRequests().length});
          }return output;
        }""")
        self.assertEqual(len(result), 18)
        self.assertEqual([row for row in result if row["ok"] or row["requests"]], [])

    def test_inflight_history_ownership_and_disposal_changes_discard_old_result(self):
        result = self.page.evaluate("""async()=>{
          const output=[];for(const mode of ['history','writer','invalidate','dispose']){
            const h=harness();await h.client.enable({acceptedCostNotice:true});h.controls.requestGate=deferred();h.controls.requestStarted=deferred();
            const pending=h.client.send(h.reminder);await h.controls.requestStarted.promise;
            if(mode==='history')h.state.historyEpoch++;else if(mode==='writer')h.state.writerOwned=false;
            else if(mode==='invalidate')h.client.invalidate();else h.client.dispose();
            h.records.clear();h.controls.requestGate.resolve();const result=await pending;
            output.push({mode,result,commits:h.commits.length,records:h.records.size,requests:h.messageRequests().length});
          }return output;
        }""")
        self.assertEqual([row["result"]["reason"] for row in result], ["inactive"] * 4)
        self.assertTrue(all(row["commits"] == row["records"] == 0 and row["requests"] == 1 for row in result))

    def test_exclusive_send_and_model_session_change_require_new_confirmation(self):
        result = self.page.evaluate("""async()=>{
          const h=harness();await h.client.enable({acceptedCostNotice:true});
          h.controls.requestGate=deferred();h.controls.requestStarted=deferred();
          const first=h.client.send(h.reminder);await h.controls.requestStarted.promise;
          const concurrent=await h.client.send(h.reminder);h.controls.requestGate.resolve();await first;
          const before=h.consentRequests()[0].body.model_session_id;h.context.credential='synthetic-replaced';
          const refreshed=await h.client.refresh(),blocked=await h.client.send(h.reminder);
          await h.client.enable({acceptedCostNotice:true});const after=h.consentRequests()[1].body.model_session_id;
          await h.client.disable();return {concurrent,requests:h.messageRequests().length,refreshed,blocked,
            sessionChanged:before!==after,disabled:h.client.preferences(),saved:h.preferences()};
        }""")
        self.assertEqual(result["concurrent"]["reason"], "busy")
        self.assertEqual(result["requests"], 1)
        self.assertFalse(result["refreshed"]["enabled"])
        self.assertEqual(result["blocked"]["reason"], "cost_confirmation_required")
        self.assertTrue(result["sessionChanged"])
        self.assertFalse(result["disabled"]["enabled"])
        self.assertFalse(result["saved"]["proactivePreferences"]["enabled"])

    def test_failed_and_unknown_outcomes_keep_usage_without_inventing_call_count(self):
        result = self.page.evaluate("""async()=>{
          const output=[];for(const status of ['failed','unknown','network']){
            const h=harness();await h.client.enable({acceptedCostNotice:true});
            h.controls.requestError=status==='network';h.controls.response={status,content_blocks:[],message_id:'',
              usage:{provider_calls:status==='failed'?0:1,prompt_tokens:null,completion_tokens:null,total_tokens:null},model_called:status!=='failed'};
            await h.client.send(h.reminder);const record=[...h.records.values()].find(row=>row.envelope),list=await h.client.requests();
            const receipts=[...h.records.values()].filter(row=>row.key?.startsWith('proactive_usage_v1:'));
            output.push({status,usage:record.usage??null,modelCalled:record.modelCalled??null,public:list[0],receipts,commits:h.commits.length});
          }return output;
        }""")
        self.assertEqual([row["usage"]["provider_calls"] if row["usage"] else None for row in result], [0, 1, None])
        self.assertEqual([row["modelCalled"] for row in result], [False, True, None])
        for row in result:
            self.assertEqual(row["public"]["usage"], row["usage"])
            self.assertEqual(row["public"]["modelCalled"], row["modelCalled"])
            self.assertEqual(row["commits"], 0)
            self.assertEqual(len(row["receipts"]), 1)
            self.assertEqual(row["receipts"][0]["usage"]["provider_calls"], row["usage"]["provider_calls"] if row["usage"] else None)
            self.assertNotIn("envelope", row["receipts"][0])
            self.assertNotIn("synthetic-signed-", str(row["receipts"]))
