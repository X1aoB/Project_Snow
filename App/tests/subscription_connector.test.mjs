import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { CodexRpc, JobRunner, MODEL, EFFORT, siteOrigin, validateJob, postSite,
  verifyAccountAndModel, generate } from '../public_frontend/downloads/snow-codex-connector.mjs';

const job = (id = 'job_012345678901234567890') => ({ job_id: id, model: MODEL, effort: EFFORT,
  messages: [{ role: 'system', content: '角色规则' }, { role: 'user', content: '你好' }] });

test('site accepts only a root HTTPS origin; local HTTP is an explicit development option', () => {
  assert.equal(siteOrigin('https://snow.example/'), 'https://snow.example');
  for (const value of ['http://snow.example', 'https://user:secret@snow.example',
    'https://snow.example/?token=private', 'https://snow.example/#secret', 'https://snow.example/path']) {
    assert.throws(() => siteOrigin(value), /invalid_site/);
  }
  assert.throws(() => siteOrigin('http://localhost:8080'), /invalid_site/);
  assert.equal(siteOrigin('http://127.0.0.1:8080', true), 'http://127.0.0.1:8080');
  assert.throws(() => siteOrigin('http://192.168.1.1', true), /invalid_site/);
});

test('untrusted jobs cannot change the model, effort, or add tool/image inputs', () => {
  assert.equal(validateJob(job()).model, MODEL);
  for (const value of [{ ...job(), model: 'another-model' }, { ...job(), effort: 'low' },
    { ...job(), messages: [{ role: 'tool', content: 'run code' }] },
    { ...job(), messages: [{ role: 'user', content: [{ type: 'image_url', image_url: 'file:///secret' }] }] },
    { ...job(), messages: [{ role: 'user', content: 'x'.repeat(300000) }] }]) {
    assert.throws(() => validateJob(value), /subscription_invalid_job/);
  }
});

test('lost poll and result responses replay one completed result without consuming another turn', async () => {
  let calls = 0;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const runner = new JobRunner(async () => { calls++; await gate; return { content: '已完成', usage: {} }; });
  const first = runner.run(job());
  const repeated = runner.run(job());
  release();
  assert.deepEqual(await first, await repeated);
  assert.equal((await runner.run(job())).content, '已完成');
  assert.equal(calls, 1);
});

test('unknown or oversized results remain terminal even if the same job is redelivered', async () => {
  for (const generate of [async () => { throw new Error('private provider error or token'); },
    async () => ({ content: 'x'.repeat(100000) })]) {
    let calls = 0;
    const runner = new JobRunner(async () => { calls++; return generate(); });
    const result = await runner.run(job());
    assert.equal(result.content, null);
    assert.match(result.error, /^subscription_(result_unknown|protocol_error)$/);
    assert.deepEqual(await runner.run(job()), result);
    assert.equal(calls, 1);
  }
});

test('metadata requires ChatGPT subscription and the exact supported max effort', async () => {
  const rpc = (type, efforts) => ({ request: async (method) => method === 'account/read'
    ? { account: { type } } : { data: [{ model: MODEL, supportedReasoningEfforts:
      efforts.map((reasoningEffort) => ({ reasoningEffort })) }] } });
  await verifyAccountAndModel(rpc('chatgpt', ['max']));
  await assert.rejects(verifyAccountAndModel(rpc('apiKey', ['max'])), /subscription_login_required/);
  await assert.rejects(verifyAccountAndModel(rpc('chatgpt', ['high'])), /subscription_model_unavailable/);
});

test('site posts never follow redirects and never echo arbitrary server diagnostics', async () => {
  let options;
  const mock = async (_url, value) => { options = value; return new Response('{"job":null}'); };
  assert.deepEqual(await postSite('https://snow.example', 'connector/poll', { connector_token: 'private' }, 1000, mock), { job: null });
  assert.equal(options.redirect, 'error');
  assert.equal(options.headers.Origin, 'https://snow.example');
  await assert.rejects(postSite('https://snow.example', 'connector/poll', {}, 1000,
    async () => new Response('{"detail":{"code":"private diagnostic"}}', { status: 400 })), /subscription_protocol_error/);
  await assert.rejects(postSite('https://snow.example', 'connector/poll', {}, 1000,
    async () => new Response('x'.repeat(300000))), /subscription_protocol_error/);
});

function childMock() {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.stdin = new PassThrough();
  child.kill = () => {};
  return child;
}

test('RPC accepts fragmented notifications, denies tools and rejects unsafe process failures', async () => {
  const child = childMock();
  const rpc = new CodexRpc(child);
  const notices = [];
  let written = '';
  child.stdin.on('data', (chunk) => { written += chunk; });
  rpc.listeners.add((message) => notices.push(message.method));
  const response = rpc.request('model/list');
  child.stdout.write('{"id":1,"result":');
  child.stdout.write('{"data":[]}}\n');
  assert.deepEqual(await response, { data: [] });
  child.stdout.write('{"id":"tool-1","method":"item/commandExecution/requestApproval"}\n');
  assert.ok(notices.includes('connector/toolDenied'));
  assert.match(written, /-32601/);
  assert.doesNotMatch(written, /accept|approved/);
  const pending = rpc.request('thread/start');
  child.emit('exit', 1);
  await assert.rejects(pending, /subscription_result_unknown/);
});

function generationRpc(overrides = {}) {
  const rpc = {
    listeners: new Set(), calls: [], closed: false,
    threadConfig: { mcp_servers: { 'local.server': { enabled: false } } },
    emit(method, params = {}) { for (const listener of [...this.listeners]) listener({ method, params }); },
    close() { this.closed = true; this.failed = new Error('closed'); this.emit('connector/closed'); },
    async request(method, params) {
      this.calls.push({ method, params });
      if (overrides[method]) return overrides[method](params, this);
      if (method === 'thread/start') return {
        thread: { id: 'isolated-thread', ephemeral: true }, model: MODEL, reasoningEffort: EFFORT,
        sandbox: { type: 'readOnly', networkAccess: false }, runtimeWorkspaceRoots: [], instructionSources: [],
      };
      if (method === 'mcpServerStatus/list') return { data: [{ name: 'local.server', tools: {} }], nextCursor: null };
      if (method === 'turn/start') {
        this.emit('turn/started', { threadId: params.threadId, turn: { id: 'isolated-turn' } });
        this.emit('item/completed', { threadId: params.threadId,
          item: { type: 'agentMessage', phase: 'final_answer', text: '合成回复' } });
        this.emit('turn/completed', { threadId: params.threadId, turn: { id: 'isolated-turn', status: 'completed' } });
        return { turn: { id: 'isolated-turn' } };
      }
      return {};
    },
  };
  return rpc;
}

test('generation uses an empty environment, disables named MCPs, and accepts events before the start response', async () => {
  const rpc = generationRpc();
  assert.deepEqual(await generate(rpc, '/synthetic-empty-directory', job()), { content: '合成回复', usage: {} });
  const start = rpc.calls.find(({ method }) => method === 'thread/start').params;
  assert.deepEqual(start.environments, []);
  assert.deepEqual(start.runtimeWorkspaceRoots, []);
  assert.deepEqual(start.dynamicTools, []);
  assert.equal(start.ephemeral, true);
  assert.equal(start.config.mcp_servers['local.server'].enabled, false);
  assert.equal(start.allowProviderModelFallback, false);
  const turn = rpc.calls.find(({ method }) => method === 'turn/start').params;
  assert.equal(turn.model, MODEL);
  assert.equal(turn.effort, EFFORT);
  assert.deepEqual(turn.environments, []);
  assert.deepEqual(turn.sandboxPolicy, { type: 'readOnly', networkAccess: false });
  assert.equal(rpc.calls.at(-1).method, 'thread/unsubscribe');
  assert.equal(rpc.listeners.size, 0);
});

test('a live MCP tool or loaded instruction prevents any model turn', async () => {
  for (const overrides of [
    { 'mcpServerStatus/list': () => ({ data: [{ name: 'unexpected', tools: { execute: {} } }] }) },
    { 'thread/start': () => ({ thread: { id: 'unsafe-thread', ephemeral: true }, model: MODEL,
      reasoningEffort: EFFORT, sandbox: { type: 'readOnly', networkAccess: false },
      instructionSources: [{ path: '/private/AGENTS.md' }], runtimeWorkspaceRoots: [] }) },
  ]) {
    const rpc = generationRpc(overrides);
    await assert.rejects(generate(rpc, '/synthetic-empty-directory', job()), /subscription_isolation_unavailable/);
    assert.equal(rpc.calls.filter(({ method }) => method === 'turn/start').length, 0);
  }
});

test('expired broker jobs never start a model turn', async () => {
  const rpc = generationRpc();
  await assert.rejects(generate(rpc, '/synthetic-empty-directory', { ...job(), timeout_seconds: 0 }));
  assert.equal(rpc.calls.filter(({ method }) => method === 'turn/start').length, 0);
});

test('metadata checks consume the same broker deadline as generation', async (t) => {
  let now = 0;
  t.mock.method(performance, 'now', () => now);
  const rpc = generationRpc({ 'mcpServerStatus/list': () => {
    now += 20000;
    return { data: [], nextCursor: null };
  } });
  await assert.rejects(generate(rpc, '/synthetic-empty-directory', { ...job(), timeout_seconds: 10 }));
  assert.equal(rpc.calls.filter(({ method }) => method === 'turn/start').length, 0);
});

test('tool denial stops an in-flight turn even when turn/start has not replied', async () => {
  let entered;
  const atStart = new Promise((resolve) => { entered = resolve; });
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const rpc = generationRpc({ 'turn/start': async (_params, current) => {
    current.emit('turn/started', { threadId: 'isolated-thread', turn: { id: 'isolated-turn' } });
    entered();
    await held;
    return { turn: { id: 'isolated-turn' } };
  } });
  const pending = generate(rpc, '/synthetic-empty-directory', job());
  const rejected = assert.rejects(pending, /subscription_tool_denied/);
  await atStart;
  rpc.emit('connector/toolDenied');
  await new Promise((resolve) => setImmediate(resolve));
  try {
    assert.equal(rpc.closed, true, 'tool denial must stop the process without waiting for the RPC response');
    assert.equal(rpc.calls.filter(({ method }) => method === 'turn/start').length, 1);
  } finally {
    release();
    await rejected;
  }
});

test('only this thread final messages become the reply; commentary and other threads stay private', async () => {
  const rpc = generationRpc({ 'turn/start': (params, current) => {
    current.emit('turn/started', { threadId: params.threadId, turn: { id: 'isolated-turn' } });
    current.emit('item/completed', { threadId: 'another-thread',
      item: { type: 'agentMessage', phase: 'final_answer', text: 'another user private text' } });
    current.emit('item/completed', { threadId: params.threadId,
      item: { type: 'agentMessage', phase: 'final_answer', text: '可显示的最终回复' } });
    current.emit('item/completed', { threadId: params.threadId,
      item: { type: 'agentMessage', phase: 'commentary', text: 'private implementation commentary' } });
    current.emit('thread/tokenUsage/updated', { threadId: params.threadId,
      tokenUsage: { last: { inputTokens: 12, outputTokens: 3, totalTokens: 15 },
        total: { inputTokens: 99, outputTokens: 50, totalTokens: 149 } } });
    current.emit('turn/completed', { threadId: params.threadId, turn: { id: 'isolated-turn', status: 'completed' } });
    return { turn: { id: 'isolated-turn' } };
  } });
  assert.deepEqual(await generate(rpc, '/synthetic-empty-directory', job()), {
    content: '可显示的最终回复', usage: { prompt_tokens: 12, completion_tokens: 3, total_tokens: 15 },
  });
});

test('a timed out turn closes the process and late success cannot make a redelivered job generate again', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let entered;
  const atStart = new Promise((resolve) => { entered = resolve; });
  const rpc = generationRpc({ 'turn/start': (params, current) => {
    current.emit('turn/started', { threadId: params.threadId, turn: { id: 'isolated-turn' } });
    entered();
    return { turn: { id: 'isolated-turn' } };
  } });
  const runner = new JobRunner((value) => generate(rpc, '/synthetic-empty-directory', value));
  const timedJob = { ...job(), timeout_seconds: 6 };
  const pending = runner.run(timedJob);
  await atStart;
  t.mock.timers.tick(1100);
  const result = await pending;
  assert.equal(result.error, 'subscription_result_unknown');
  assert.equal(result.content, null);
  assert.equal(rpc.closed, true);
  assert.equal(rpc.listeners.size, 0);
  rpc.emit('item/completed', { threadId: 'isolated-thread',
    item: { type: 'agentMessage', phase: 'final_answer', text: 'late result' } });
  rpc.emit('turn/completed', { threadId: 'isolated-thread', turn: { id: 'isolated-turn', status: 'completed' } });
  assert.deepEqual(await runner.run(timedJob), result);
  assert.equal(rpc.calls.filter(({ method }) => method === 'turn/start').length, 1);
});

test('closing RPC while denying a tool ignores other requests already buffered or arriving late', async () => {
  const child = childMock();
  let killed = 0;
  child.kill = () => { killed++; };
  const rpc = new CodexRpc(child);
  rpc.listeners.add(({ method }) => { if (method === 'connector/toolDenied') rpc.close(); });
  const pending = assert.rejects(rpc.request('turn/start'), /subscription_result_unknown/);
  const first = JSON.stringify({ id: 'first-tool', method: 'item/tool/call' });
  const second = JSON.stringify({ id: 'second-tool', method: 'item/tool/call' });
  assert.doesNotThrow(() => child.stdout.write(first + '\n' + second + '\n'));
  assert.doesNotThrow(() => child.stdout.write(second + '\n'));
  await pending;
  assert.equal(killed, 1);
  assert.equal(rpc.pending.size, 0);
});

test('invalid RPC message shapes become a safe protocol failure without an uncaught exception', async () => {
  for (const message of ['null', '[]', 'true', '42']) {
    const child = childMock();
    const rpc = new CodexRpc(child);
    const pending = assert.rejects(rpc.request('model/list'), /subscription_protocol_error/);
    assert.doesNotThrow(() => child.stdout.write(message + '\n'));
    await pending;
    assert.equal(rpc.pending.size, 0);
  }
});
