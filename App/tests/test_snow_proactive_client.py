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
    const context={enabled:true,notice_version:'proactive-cost-2',ready:true,provider:'openai',model:'synthetic-model',
      credential:'synthetic-credential-only',segment_id:'00000000-0000-4000-8000-000000000001',
      state_package:'synthetic-signed-world',session_context_package:'synthetic-signed-dialogue',...clone(initial.context||{})};
    let preferences=clone(initial.preferences||{});
    const records=new Map(Object.entries(clone(initial.records||{}))),requests=[],saves=[],commits=[],events=[],log=[];
    const storageMaps={tab:new Map(Object.entries(clone(initial.stores?.tab||{}))),local:new Map(Object.entries(clone(initial.stores?.local||{})))},storageLog=[];
    const storageApi=initial.storageApi||'load';
    const makeStore=kind=>storageApi==='get'?{get:key=>{storageLog.push(['get',kind,key]);return clone(storageMaps[kind].get(key));},set:(key,value)=>{storageLog.push(['set',kind,key]);storageMaps[kind].set(key,clone(value));},delete:key=>{storageLog.push(['delete',kind,key]);return storageMaps[kind].delete(key);}}:
      {load:key=>{storageLog.push(['load',kind,key]);return clone(storageMaps[kind].get(key));},save:(key,value)=>{storageLog.push(['save',kind,key]);storageMaps[kind].set(key,clone(value));},clear:key=>{storageLog.push(['clear',kind,key]);storageMaps[kind].delete(key);}};
    const controls={visible:true,active:[reminder],requestGate:null,requestStarted:null,requestError:false,saveError:'',
      retention:Object.hasOwn(initial,'retention')?initial.retention:undefined,
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
        if(path==='/rendezvous/consent'){const response=controls.consentResponse||{};return {consent_token:'synthetic-consent-only',notice_version:'proactive-cost-2',
          model_session_id:body.model_session_id,duration:body.duration||'tab',expires_at:new Date(Date.now()+3600000).toISOString(),...clone(response),
          model_session_id:response.model_session_id||body.model_session_id};}
        if(path!=='/rendezvous/message')throw Error('unexpected_synthetic_endpoint');
        if(controls.requestGate)await controls.requestGate.promise;
        if(controls.requestError)throw Error('synthetic_connection_lost');
        return controls.response?{...success(body),...clone(controls.response)}:success(body);},
      async commit(result,record,receipt){commits.push({result:clone(result),record:clone(record),receipt:clone(receipt)});
        log.push('commit');records.set(record.key,clone(record));if(receipt?.key)records.set(receipt.key,clone(receipt));
        if(record.usageReceipt?.key)records.set(record.usageReceipt.key,clone(record.usageReceipt));},
      emit(type,value){events.push([type,clone(value)]);}};
    if(initial.storage){
      if(initial.storage==='native'){
        sessionStorage.removeItem('project-snow-public:proactive-consent:v1');localStorage.removeItem('project-snow-public:proactive-consent:v1');
        adapter.retention=()=>controls.retention;adapter.storageForRetention=kind=>kind==='tab'?sessionStorage:localStorage;
      }else{
        const stores={tab:makeStore('tab'),local:makeStore('local')};
        adapter.retention=()=>controls.retention;adapter.storageForRetention=kind=>stores[kind];
      }
    }
    const h={state,context,reminder,controls,records,requests,saves,commits,events,log,adapter,storageMaps,storageLog,
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
        try:
            cls.browser.close()
        finally:
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
            consent:{path:consent.path,accepted:consent.body.accepted_cost_notice,notice:consent.body.notice_version,duration:consent.body.duration},
            failures:failures.map(row=>[row.result.ok,row.requests]),durable:JSON.stringify([...h.records.values(),h.preferences()])};
        }""")
        self.assertFalse(result["initial"]["enabled"])
        self.assertTrue(result["initial"]["requiresConfirmation"])
        self.assertEqual(result["before"], 0)
        self.assertEqual(result["beforeContext"], 0)
        self.assertEqual(result["blocked"], ["cost_confirmation_required"] * 5)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["consent"], {"path": "/rendezvous/consent", "accepted": True, "notice": "proactive-cost-2", "duration": "tab"})
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

    def test_retention_selects_tab_or_local_storage_and_restores_session_and_consent(self):
        result = self.page.evaluate("""async()=>{
          const tab=harness({storage:true,retention:'tab'}),tabEnabled=await tab.client.enable({acceptedCostNotice:true});
          const tabStored=tab.storageMaps.tab.get('project-snow-public:proactive-consent:v1'),tabLocal=tab.storageMaps.local.size;
          const tabSession=tabStored.model_session_id;tab.newClient();const tabRestored=await tab.client.refresh();
          const local=harness({storage:true,retention:'24h'});await local.client.enable({acceptedCostNotice:true});
          return {tabEnabled:tabEnabled.ok,tabStored,tabLocal,tabRestored,tabSession,
            tabConsentRequests:tab.consentRequests().length,
            tabRequestSession:tab.consentRequests()[0].body.model_session_id,
            localTab:local.storageMaps.tab.size,localStored:local.storageMaps.local.get('project-snow-public:proactive-consent:v1')};
        }""")
        self.assertTrue(result["tabEnabled"])
        self.assertEqual(result["tabLocal"], 0)
        self.assertEqual(result["tabConsentRequests"], 1)
        self.assertEqual(result["tabSession"], result["tabRequestSession"])
        self.assertTrue(result["tabRestored"]["enabled"])
        self.assertEqual(result["localTab"], 0)
        self.assertIn("consent", result["localStored"])
        self.assertNotIn("synthetic-credential-only", str(result))
        self.assertNotIn("credential", result["tabStored"])

    def test_retention_accepts_only_exact_values_and_defaults_invalid_values_to_tab(self):
        result = self.page.evaluate("""async()=>{
          const values=['tab','2h','24h','3d','7d',undefined,null,'TAB','local','session','forever',42];
          const rows=[];
          for(const retention of values){
            const options={storage:true};if(retention!==undefined)options.retention=retention;
            const h=harness(options);await h.client.refresh();
            rows.push({retention,tab:h.storageMaps.tab.size,local:h.storageMaps.local.size});
          }
          return rows;
        }""")
        self.assertEqual([(row["tab"], row["local"]) for row in result],
                         [(1, 0), (0, 1), (0, 1), (0, 1), (0, 1),
                          (1, 0), (1, 0), (1, 0), (1, 0), (1, 0), (1, 0), (1, 0)])

    def test_session_only_auth_restores_model_session_without_consent(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({storage:true,retention:'tab'});await h.client.refresh();
          const first=h.storageMaps.tab.get('project-snow-public:proactive-consent:v1'),firstSession=first.model_session_id;
          h.newClient();await h.client.refresh();await h.client.enable({acceptedCostNotice:true});
          return {first,firstSession,consent:h.consentRequests()[0]};
        }""")
        self.assertIsNone(result["first"]["consent"])
        self.assertEqual(result["firstSession"], result["consent"]["body"]["model_session_id"])

    def test_expiry_clears_persisted_consent_and_confirmation_is_bounded_to_seven_days(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({storage:true,retention:'24h'});h.controls.consentResponse={consent_token:'synthetic-consent-only',
            notice_version:'proactive-cost-2',duration:'24h',model_session_id:'',expires_at:new Date(Date.now()+60).toISOString()};
          const enabled=await h.client.enable({acceptedCostNotice:true,duration:'24h'});const stored=h.storageMaps.local.get('project-snow-public:proactive-consent:v1');
          await new Promise(resolve=>setTimeout(resolve,120));
          const after=h.storageMaps.local.get('project-snow-public:proactive-consent:v1');
          const long=harness({storage:true,retention:'7d'});long.controls.consentResponse={consent_token:'long-lived',
            notice_version:'proactive-cost-2',duration:'7d',model_session_id:'',expires_at:new Date(Date.now()+8*24*60*60*1000).toISOString()};
          const longEnabled=await long.client.enable({acceptedCostNotice:true,duration:'7d'});const bounded=long.storageMaps.local.get('project-snow-public:proactive-consent:v1');
          return {enabled,stored,after,bounded,prefs:long.client.preferences(),duration:longEnabled.duration,remaining:Date.parse(bounded.consent.expires_at)-bounded.confirmed_at};
        }""")
        self.assertTrue(result["enabled"]["ok"])
        self.assertIsNone(result["after"])
        self.assertEqual(result["bounded"]["consent"]["duration"], "7d")
        self.assertEqual(result["duration"], "7d")
        self.assertEqual(result["prefs"]["duration"], "7d")
        self.assertTrue(isinstance(result["prefs"]["expiresAt"], str))
        self.assertLessEqual(result["remaining"], 7 * 24 * 60 * 60 * 1000)
        self.assertGreater(result["remaining"], 24 * 60 * 60 * 1000)
        self.assertNotIn("synthetic-credential-only", str(result))
    def test_duration_is_forwarded_persisted_and_restored(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({storage:true,retention:'7d'});
          const enabled=await h.client.enable({acceptedCostNotice:true,duration:'3d'});
          const body=h.consentRequests()[0].body,prefs=h.client.preferences();
          const stored=h.storageMaps.local.get('project-snow-public:proactive-consent:v1');
          h.newClient();await h.client.refresh();const restored=h.client.preferences();
          return {ok:enabled.ok,sent:body.duration,enabledDuration:enabled.duration,
            prefsDuration:prefs.duration,storedDuration:stored.consent.duration,restoredDuration:restored.duration,
            restoredEnabled:restored.enabled,restoredExpiresAt:restored.expiresAt};
        }""")
        self.assertTrue(result["ok"])
        self.assertEqual(result["sent"], "3d")
        self.assertEqual(result["enabledDuration"], "3d")
        self.assertEqual(result["prefsDuration"], "3d")
        self.assertEqual(result["storedDuration"], "3d")
        self.assertEqual(result["restoredDuration"], "3d")
        self.assertTrue(result["restoredEnabled"])
        self.assertTrue(isinstance(result["restoredExpiresAt"], str))



    def test_invalidate_clear_model_change_and_retention_change_remove_old_authorization(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({storage:true,retention:'24h'});await h.client.enable({acceptedCostNotice:true});
          const before=h.storageMaps.local.get('project-snow-public:proactive-consent:v1');await h.client.clear();
          const cleared=h.storageMaps.local.get('project-snow-public:proactive-consent:v1');h.newClient();await h.client.refresh();
          const fresh=h.storageMaps.local.get('project-snow-public:proactive-consent:v1');
          await h.client.enable({acceptedCostNotice:true});h.context.model='changed-model';await h.client.refresh();
          const changed=h.storageMaps.local.get('project-snow-public:proactive-consent:v1');
          h.controls.retention='tab';await h.client.refresh();
          return {before,cleared,fresh,changed,tab:h.storageMaps.tab.get('project-snow-public:proactive-consent:v1'),prefs:h.client.preferences()};
        }""")
        self.assertIn("consent", result["before"])
        self.assertIsNone(result["cleared"])
        self.assertIsNone(result["fresh"]["consent"])
        self.assertIsNone(result["changed"]["consent"])
        self.assertIsNone(result["tab"]["consent"])
        self.assertFalse(result["prefs"]["enabled"])

    def test_retention_policy_change_within_local_storage_clears_consent_and_rotates_session(self):
        result = self.page.evaluate("""async()=>{
          const key='project-snow-public:proactive-consent:v1';
          const h=harness({storage:true,retention:'24h'});await h.client.enable({acceptedCostNotice:true});
          const before=h.storageMaps.local.get(key),beforeSession=before.model_session_id;
          h.controls.retention='7d';const changed=await h.client.refresh();
          const after=h.storageMaps.local.get(key),afterSession=after.model_session_id;
          h.newClient();const restored=await h.client.refresh();
          return {before,after,beforeSession,afterSession,changed,restored,
            persisted:h.storageMaps.local.get(key),ops:h.storageLog.map(row=>row[0])};
        }""")
        self.assertIn("consent", result["before"])
        self.assertIsNone(result["after"]["consent"])
        self.assertNotEqual(result["beforeSession"], result["afterSession"])
        self.assertFalse(result["changed"]["enabled"])
        self.assertFalse(result["restored"]["enabled"])
        # refresh() returns preferences only; the restored client must reuse the rotated session, not rotate again.
        self.assertEqual(result["persisted"]["model_session_id"], result["afterSession"])
        self.assertIsNone(result["persisted"]["consent"])
        self.assertIn("clear", result["ops"])

    def test_native_storage_clear_removes_only_consent_key_and_missing_hook_keeps_memory_only(self):
        result = self.page.evaluate("""async()=>{
          const native=harness({storage:'native',retention:'tab'});sessionStorage.setItem('synthetic-keep','keep');
          await native.client.enable({acceptedCostNotice:true});await native.client.invalidate();
          const memory=harness();await memory.client.enable({acceptedCostNotice:true});const before=memory.client.preferences();
          memory.newClient();const after=await memory.client.refresh();
          return {nativeAuth:sessionStorage.getItem('project-snow-public:proactive-consent:v1'),keep:sessionStorage.getItem('synthetic-keep'),before,after};
        }""")
        self.assertIsNone(result["nativeAuth"])
        self.assertEqual(result["keep"], "keep")
        self.assertTrue(result["before"]["enabled"])
        self.assertFalse(result["after"]["enabled"])

    def test_get_set_delete_storage_adapter_restores_and_clears_authorization(self):
        result = self.page.evaluate("""async()=>{
          const h=harness({storage:true,storageApi:'get',retention:'24h'});
          await h.client.enable({acceptedCostNotice:true});const stored=h.storageMaps.local.get('project-snow-public:proactive-consent:v1');
          h.newClient();const restored=await h.client.refresh();await h.client.clear();
          return {stored,restored,remaining:h.storageMaps.local.size,ops:h.storageLog.map(row=>row[0])};
        }""")
        self.assertIn("consent", result["stored"])
        self.assertTrue(result["restored"]["enabled"])
        self.assertEqual(result["remaining"], 0)
        self.assertIn("get", result["ops"])
        self.assertIn("set", result["ops"])
        self.assertIn("delete", result["ops"])
