#!/usr/bin/env node
/** 小吉终端个人订阅连接器。Node.js 22+；只通过官方 Codex App Server 登录和生成。 */
import { spawn, execFileSync } from 'node:child_process';
import { existsSync, readdirSync, statSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline/promises';

export const MODEL = 'gpt-5.6-luna';
export const EFFORT = 'max';
const MAX_WIRE_BYTES = 1024 * 1024;
const MAX_RESULT_BYTES = 48 * 1024;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class ConnectorError extends Error {
  constructor(code) { super(code); this.code = code; }
}

export function siteOrigin(raw, allowLocal = false) {
  let url;
  try { url = new URL(raw); } catch { throw new ConnectorError('invalid_site'); }
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/' ||
      !(url.protocol === 'https:' || (allowLocal && local && url.protocol === 'http:'))) {
    throw new ConnectorError('invalid_site');
  }
  return url.origin;
}

export function validateJob(job) {
  if (!job || typeof job.job_id !== 'string' || !/^[A-Za-z0-9_-]{12,128}$/.test(job.job_id) ||
      job.model !== MODEL || job.effort !== EFFORT || !Array.isArray(job.messages) ||
      job.messages.length < 1 || job.messages.length > 32 ||
      Buffer.byteLength(JSON.stringify(job), 'utf8') > 256 * 1024) {
    throw new ConnectorError('subscription_invalid_job');
  }
  for (const message of job.messages) {
    if (!['system', 'developer', 'user', 'assistant'].includes(message?.role) ||
        typeof message.content !== 'string') throw new ConnectorError('subscription_invalid_job');
  }
  return job;
}

function commandFor(file) {
  return /\.(?:c?js|mjs)$/i.test(file)
    ? { command: process.execPath, args: [file] }
    : { command: file, args: [] };
}

export function resolveCodex(explicit) {
  const candidates = [];
  if (explicit) candidates.push(path.resolve(explicit));
  else {
    for (const directory of (process.env.PATH || '').split(path.delimiter)) {
      if (!directory) continue;
      candidates.push(path.join(directory, process.platform === 'win32' ? 'codex.exe' : 'codex'));
      if (process.platform === 'win32') candidates.push(path.join(directory, 'node_modules/@openai/codex/bin/codex.js'));
    }
    // Desktop installs keep a versioned official binary here. No account files are opened.
    if (process.platform === 'win32' && process.env.LOCALAPPDATA) {
      const root = path.join(process.env.LOCALAPPDATA, 'OpenAI/Codex/bin');
      if (existsSync(root)) {
        const installed = readdirSync(root, { withFileTypes: true }).filter((item) => item.isDirectory())
          .map((item) => path.join(root, item.name, 'codex.exe')).filter(existsSync)
          .sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs);
        candidates.push(...installed);
      }
    }
  }
  for (const file of [...new Set(candidates)]) {
    if (!existsSync(file)) continue;
    const command = commandFor(file);
    try {
      const version = execFileSync(command.command, [...command.args, '--version'], {
        encoding: 'utf8', timeout: 15000, windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
      if (/^codex-cli \d+\.\d+\.\d+/.test(version)) return { ...command, version };
    } catch { /* Try another installed official launcher; never echo process output. */ }
  }
  throw new ConnectorError('codex_unavailable');
}

export class CodexRpc {
  constructor(child) {
    this.child = child;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Set();
    this.buffer = '';
    this.failed = null;
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => this.receive(chunk));
    // Codex stderr can contain account/environment diagnostics. Keep it out of the site and terminal.
    child.stderr.resume();
    child.on('error', () => this.fail('codex_unavailable'));
    child.on('exit', () => this.fail('subscription_result_unknown'));
    child.stdin.on('error', () => this.fail('subscription_result_unknown'));
  }
  receive(chunk) {
    if (this.failed) return;
    this.buffer += chunk;
    if (Buffer.byteLength(this.buffer, 'utf8') > MAX_WIRE_BYTES) return this.fail('subscription_protocol_error');
    let split;
    while ((split = this.buffer.indexOf('\n')) >= 0) {
      if (this.failed) return;
      const line = this.buffer.slice(0, split);
      this.buffer = this.buffer.slice(split + 1);
      if (!line.trim()) continue;
      let message;
      try { message = JSON.parse(line); } catch { return this.fail('subscription_protocol_error'); }
      if (!message || typeof message !== 'object' || Array.isArray(message)) return this.fail('subscription_protocol_error');
      if (message.method && message.id !== undefined) {
        // This connector never grants tool execution, approvals, or account token requests.
        this.write({ id: message.id, error: { code: -32601, message: 'Unsupported in personal chat connector' } });
        this.notify({ method: 'connector/toolDenied', params: {} });
      } else if (message.id !== undefined) {
        const entry = this.pending.get(message.id);
        if (!entry) continue;
        clearTimeout(entry.timer);
        this.pending.delete(message.id);
        if (message.error) {
          const error = new ConnectorError('subscription_codex_error');
          error.method = entry.method;
          entry.reject(error);
        }
        else entry.resolve(message.result);
      } else if (typeof message.method === 'string') this.notify(message);
    }
  }
  notify(message) { for (const listener of [...this.listeners]) listener(message); }
  write(message) {
    if (this.failed) throw this.failed;
    this.child.stdin.write(JSON.stringify(message) + '\n');
  }
  request(method, params = {}, timeout = 30000) {
    if (this.failed) return Promise.reject(this.failed);
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new ConnectorError('subscription_result_unknown'));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer, method });
      try { this.write({ id, method, params }); }
      catch (error) { clearTimeout(timer); this.pending.delete(id); reject(error); }
    });
  }
  fail(code) {
    if (this.failed) return;
    this.failed = new ConnectorError(code);
    for (const entry of this.pending.values()) { clearTimeout(entry.timer); entry.reject(this.failed); }
    this.pending.clear();
    this.notify({ method: 'connector/closed', params: {} });
    this.child.kill();
  }
  close() { this.fail('subscription_result_unknown'); }
  async shutdown() {
    if (this.child.exitCode !== null && this.child.exitCode !== undefined) return;
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 5000);
      this.child.once('exit', () => { clearTimeout(timer); resolve(); });
      this.close();
    });
  }
}

export async function verifyAccountAndModel(rpc) {
  const account = await rpc.request('account/read', { refreshToken: false });
  if (account?.account?.type !== 'chatgpt') throw new ConnectorError('subscription_login_required');
  let cursor;
  for (let page = 0; page < 20; page++) {
    const response = await rpc.request('model/list', { limit: 100, includeHidden: true, ...(cursor ? { cursor } : {}) });
    const model = response?.data?.find((candidate) => candidate.model === MODEL);
    if (model) {
      if (!model.supportedReasoningEfforts?.some((item) => item.reasoningEffort === EFFORT)) {
        throw new ConnectorError('subscription_model_unavailable');
      }
      return;
    }
    cursor = response?.nextCursor;
    if (!cursor) break;
  }
  throw new ConnectorError('subscription_model_unavailable');
}

export async function postSite(site, route, body, timeout = 30000, fetcher = fetch) {
  let response;
  try {
    response = await fetcher(site + '/public/v1/subscription/' + route, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(timeout),
      headers: { 'Content-Type': 'application/json', Origin: site, Accept: 'application/json' },
      body: JSON.stringify(body),
    });
  } catch { throw new ConnectorError('subscription_network_error'); }
  const reader = response.body?.getReader();
  if (!reader) throw new ConnectorError('subscription_protocol_error');
  let length = 0;
  const parts = [];
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > 256 * 1024) {
        await reader.cancel();
        throw new ConnectorError('subscription_protocol_error');
      }
      parts.push(value);
    }
  } catch (error) {
    if (error instanceof ConnectorError) throw error;
    throw new ConnectorError('subscription_network_error');
  }
  let payload;
  try { payload = JSON.parse(Buffer.concat(parts).toString('utf8')); }
  catch { throw new ConnectorError('subscription_protocol_error'); }
  if (!response.ok) {
    const known = new Set(['credential_invalid', 'subscription_disabled', 'subscription_pairing_expired',
      'subscription_not_connected', 'subscription_result_unknown', 'subscription_model_unavailable',
      'rate_limit_exceeded', 'subscription_busy', 'subscription_pairing_invalid']);
    const code = payload?.detail?.code;
    throw new ConnectorError(known.has(code) ? code : 'subscription_protocol_error');
  }
  return payload;
}

// One cached result per job ID, including unknown outcomes. A lost poll/result response never repeats a turn.
export class JobRunner {
  constructor(generate) { this.generate = generate; this.results = new Map(); }
  async run(job) {
    validateJob(job);
    const existing = this.results.get(job.job_id);
    if (existing) return existing;
    if (this.results.size >= 250) throw new ConnectorError('subscription_session_limit');
    const task = (async () => {
      try {
        const result = await this.generate(job);
        if (typeof result?.content !== 'string' || !result.content.trim() ||
            Buffer.byteLength(result.content, 'utf8') > MAX_RESULT_BYTES ||
            Buffer.byteLength(JSON.stringify(result), 'utf8') > 60 * 1024) {
          throw new ConnectorError('subscription_invalid_response');
        }
        return { job_id: job.job_id, content: result.content, usage: result.usage || {}, error: null };
      } catch (error) {
        const safe = new Set(['subscription_model_unavailable', 'subscription_login_required',
          'subscription_rate_limited', 'subscription_protocol_error']);
        return { job_id: job.job_id, content: null, usage: {},
          error: safe.has(error?.code) ? error.code : error?.code === 'subscription_invalid_response'
            ? 'subscription_protocol_error' : 'subscription_result_unknown' };
      }
    })();
    this.results.set(job.job_id, task);
    return task;
  }
}

// Disable executable capabilities before the first thread is created. Empty environment selection
// also removes shell, apply_patch and local-image tools in the official App Server tool registry.
export const ISOLATED_CONFIG = Object.freeze({
  'features.shell_tool': false, 'features.unified_exec': false, 'features.shell_snapshot': false,
  'features.code_mode': false, 'features.code_mode_host': false, 'features.apps': false,
  'features.plugins': false, 'features.remote_plugin': false, 'features.browser_use': false,
  'features.browser_use_external': false, 'features.computer_use': false,
  'features.image_generation': false, 'features.in_app_browser': false,
  'features.in_app_local_automation': false, 'features.goals': false, 'features.multi_agent': false,
  'features.memories': false, 'features.hooks': false, 'features.skill_search': false,
  'features.skill_mcp_dependency_install': false, 'features.view_image': false,
  'features.workspace_dependencies': false, 'features.tool_suggest': false,
  'features.request_permissions_tool': false, 'features.sleep_tool': false,
  'features.deferred_executor': false, 'features.token_budget': false, 'features.current_time_reminder': false,
  'features.skip_host_skill_discovery': true, 'agents.enabled': false, 'web_search': 'disabled',
  'skills.bundled.enabled': false, 'skills.include_instructions': false,
  'orchestrator.skills.enabled': false, 'orchestrator.mcp.enabled': false,
  'tools.experimental_request_user_input.enabled': false, 'tools.update_plan.enabled': false,
  'project_doc_max_bytes': 0, 'include_environment_context': false,
  notify: [],
  'openai_base_url': 'https://chatgpt.com/backend-api/codex',
  'chatgpt_base_url': 'https://chatgpt.com/backend-api/',
  'otel.exporter': 'none', 'otel.trace_exporter': 'none', 'otel.metrics_exporter': 'none',
  'analytics.enabled': false,
  'history.persistence': 'none', 'otel.log_user_prompt': false, 'model_provider': 'openai',
  'model_reasoning_effort': EFFORT,
});

export function configArguments(config) {
  return Object.entries(config).flatMap(([key, value]) => ['-c', key + '=' + JSON.stringify(value)]);
}

function startRpc(command, cwd, config) {
  const child = spawn(command.command, [...command.args, 'app-server', '--stdio', '--strict-config', ...configArguments(config)], {
    cwd, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, shell: false,
  });
  return new CodexRpc(child);
}

async function initialize(rpc) {
  await rpc.request('initialize', {
    clientInfo: { name: 'snow_personal_connector', title: '小吉终端个人订阅连接器', version: '1.0.0' },
    capabilities: { experimentalApi: true },
  });
  rpc.write({ method: 'initialized', params: {} });
}

export async function openIsolatedCodex(command, cwd) {
  const version = command.version.match(/^codex-cli (\d+)\.(\d+)\.(\d+)/);
  if (!version || (Number(version[1]) === 0 && (Number(version[2]) < 153 ||
      (Number(version[2]) === 153 && Number(version[3]) < 4)))) {
    throw new ConnectorError('codex_update_required');
  }
  // Reading config metadata creates no thread and starts no model call. Never print its contents.
  const rpc = startRpc(command, cwd, ISOLATED_CONFIG);
  try {
    await initialize(rpc);
    const effective = await rpc.request('config/read', { includeLayers: false, cwd });
    const servers = effective?.config?.mcp_servers || {};
    if (typeof servers !== 'object' || Array.isArray(servers)) {
      throw new ConnectorError('subscription_isolation_unavailable');
    }
    const names = Object.keys(servers);
    if (names.length > 64 || names.some((name) => !name || name.length > 200 || /[\x00-\x1f]/.test(name))) {
      throw new ConnectorError('subscription_isolation_unavailable');
    }
    rpc.threadConfig = { mcp_servers: Object.fromEntries(names.map((name) => [name, { enabled: false }])) };
    await verifyAccountAndModel(rpc);
    return rpc;
  } catch (error) { rpc.close(); throw error; }
}

export function threadParameters(job, cwd, isolationConfig = {}) {
  validateJob(job);
  return {
    model: MODEL, modelProvider: 'openai', allowProviderModelFallback: false,
    approvalPolicy: 'never', sandbox: 'read-only', cwd, ephemeral: true,
    environments: [], runtimeWorkspaceRoots: [], dynamicTools: [], selectedCapabilityRoots: [],
    config: { ...isolationConfig, model_reasoning_effort: EFFORT },
    baseInstructions: job.messages.filter((message) => ['system', 'developer'].includes(message.role))
      .map((message) => message.content).join('\n\n'),
    developerInstructions: 'Answer the provided conversation as a text-only assistant. Do not call tools. Return only the requested final response.',
  };
}

export async function generate(rpc, cwd, job) {
  validateJob(job);
  const seconds = job.timeout_seconds === undefined ? 120 : job.timeout_seconds;
  if (!Number.isFinite(seconds) || seconds <= 5) throw new ConnectorError('subscription_result_unknown');
  const deadline = performance.now() + Math.min(175000, (seconds - 5) * 1000);
  const remaining = () => {
    const milliseconds = Math.floor(deadline - performance.now());
    if (milliseconds <= 0) throw new ConnectorError('subscription_result_unknown');
    return milliseconds;
  };
  const started = await rpc.request('thread/start', threadParameters(job, cwd, rpc.threadConfig), Math.min(30000, remaining()));
  const threadId = started?.thread?.id;
  if (!threadId || started.model !== MODEL || started.reasoningEffort !== EFFORT ||
      started.sandbox?.type !== 'readOnly' || started.sandbox?.networkAccess !== false ||
      started.thread.ephemeral !== true || (started.runtimeWorkspaceRoots || []).length ||
      (started.instructionSources || []).length) {
    throw new ConnectorError('subscription_isolation_unavailable');
  }
  let cursor;
  do {
    const mcp = await rpc.request('mcpServerStatus/list', { threadId, limit: 100, ...(cursor ? { cursor } : {}) }, Math.min(30000, remaining()));
    if (mcp?.data?.some((server) => Object.keys(server.tools || {}).length)) {
      throw new ConnectorError('subscription_isolation_unavailable');
    }
    cursor = mcp?.nextCursor;
  } while (cursor);
  let turnId;
  let finalText = '';
  let usage = {};
  let listener;
  let timer;
  const timeout = remaining();
  const completed = new Promise((resolve, reject) => {
    timer = setTimeout(() => { reject(new ConnectorError('subscription_result_unknown')); rpc.close(); }, timeout);
    listener = ({ method, params = {} }) => {
      if (method === 'connector/toolDenied') { reject(new ConnectorError('subscription_tool_denied')); rpc.close(); return; }
      if (method === 'connector/closed') return reject(new ConnectorError('subscription_result_unknown'));
      if (params.threadId !== threadId) return;
      if (method === 'turn/started') turnId = params.turn?.id;
      if (method === 'item/started' && ['commandExecution', 'fileChange', 'mcpToolCall',
        'dynamicToolCall', 'webSearch', 'imageGeneration'].includes(params.item?.type)) {
        reject(new ConnectorError('subscription_tool_denied')); rpc.close(); return;
      }
      if (method === 'item/completed' && params.item?.type === 'agentMessage' &&
          params.item.phase !== 'commentary') finalText = params.item.text || '';
      if (method === 'thread/tokenUsage/updated') {
        const tokens = params.tokenUsage?.last || params.tokenUsage?.total;
        if (tokens) usage = {
          prompt_tokens: Math.max(0, Number(tokens.inputTokens) || 0),
          completion_tokens: Math.max(0, Number(tokens.outputTokens) || 0),
          total_tokens: Math.max(0, Number(tokens.totalTokens) || 0),
        };
      }
      if (method === 'turn/completed') {
        if (params.turn?.status === 'completed') resolve({ content: finalText, usage });
        else reject(new ConnectorError('subscription_result_unknown'));
      }
    };
    rpc.listeners.add(listener);
  });
  // Observe early rejections while the turn/start RPC is pending.
  completed.catch(() => {});
  try {
    const messages = job.messages.filter((message) => !['system', 'developer'].includes(message.role));
    const text = messages.length === 1 && messages[0].role === 'user'
      ? messages[0].content : JSON.stringify(messages);
    const response = await rpc.request('turn/start', {
      threadId, input: [{ type: 'text', text }], model: MODEL, effort: EFFORT,
      approvalPolicy: 'never', environments: [],
      sandboxPolicy: { type: 'readOnly', networkAccess: false },
    }, Math.min(remaining(), 30000));
    turnId = response?.turn?.id || turnId;
    return await completed;
  } catch (error) {
    if (turnId) await rpc.request('turn/interrupt', { threadId, turnId }, 3000).catch(() => {});
    // A canceled/failed generation closes the process: no orphaned turn continues consuming quota.
    rpc.close();
    throw error;
  } finally {
    clearTimeout(timer);
    rpc.listeners.delete(listener);
    if (!rpc.failed) await rpc.request('thread/unsubscribe', { threadId }, 5000).catch(() => {});
  }
}

const FRIENDLY = {
  invalid_site: '请填写完整 HTTPS 网站地址；本地测试需要 --allow-local。',
  codex_unavailable: '找不到可运行的 Codex。请安装或更新官方 Codex CLI，或用 --codex 指定官方 codex.exe 路径。',
  codex_update_required: '请更新官方 Codex 至 0.153.4 或更新版本，再启动连接器。',
  subscription_login_required: '请先运行 codex login，并使用包含 Codex 权限的 ChatGPT 订阅账号登录。',
  subscription_model_unavailable: '当前账号没有 Luna Max 权限；连接器不会自动改用其他模型。',
  subscription_isolation_unavailable: '当前 Codex 配置不能建立隔离会话，请更新 Codex 后重试。',
  subscription_disabled: '本站尚未启用订阅连接，请联系站点维护者。',
  subscription_pairing_expired: '配对码已过期，请在网站生成新配对码。',
  subscription_pairing_invalid: '配对码无效或已经使用，请在网站重新配对。',
  credential_invalid: '连接已失效或被撤销，请在网站重新配对。',
  subscription_not_connected: '连接已断开，请在网站重新配对。',
  subscription_result_unknown: '请求结果未能确认。连接器已停止，不会重新生成；请先在网站查看该次请求的结果。',
  subscription_tool_denied: '模型请求了此连接器不支持的工具，已停止本次连接。',
  subscription_session_limit: '本次连接已达到请求数量上限，请重新配对。',
};

export async function main(argv = process.argv.slice(2)) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (['--allow-local', '--check', '--help'].includes(key)) args[key] = true;
    else if (['--site', '--codex'].includes(key) && argv[i + 1]) args[key] = argv[++i];
    else throw new ConnectorError('invalid_arguments');
  }
  if (args['--help']) {
    console.log('用法：node snow-codex-connector.mjs --site https://你的站点\n可选：--codex 官方可执行文件路径；--check 只检查账号/模型；--allow-local 允许本机 HTTP 测试。\n配对码在启动后交互输入，不作为命令参数或 URL 传递。');
    return;
  }
  if (Number(process.versions.node.split('.')[0]) < 22) throw new ConnectorError('node_update_required');
  const site = args['--check'] && !args['--site'] ? null : siteOrigin(args['--site'], args['--allow-local']);
  const command = resolveCodex(args['--codex']);
  const cwd = mkdtempSync(path.join(tmpdir(), 'snow-codex-chat-'));
  let rpc;
  let interrupted = false;
  const stop = () => { interrupted = true; rpc?.close(); };
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
  try {
    rpc = await openIsolatedCodex(command, cwd);
    console.log('已确认：ChatGPT 订阅登录 · Luna Max 可用。登录凭据留在本机。');
    if (args['--check']) return;
    console.log('即将连接：' + site + '\n仅输入你自己在该网站生成的配对码。连接期间保持本程序运行；Ctrl+C 停止。');
    const prompt = createInterface({ input: process.stdin, output: process.stdout });
    let pairingCode;
    try { pairingCode = (await prompt.question('配对码：')).trim(); } finally { prompt.close(); }
    if (!/^[A-Za-z0-9_-]{32,64}$/.test(pairingCode)) throw new ConnectorError('subscription_pairing_invalid');
    const connection = await postSite(site, 'connector/connect', { pairing_code: pairingCode,
      protocol_version: 1, model: MODEL, effort: EFFORT });
    pairingCode = '';
    if (typeof connection.connector_token !== 'string' || connection.connector_token.length < 32) {
      throw new ConnectorError('subscription_protocol_error');
    }
    const token = connection.connector_token;
    const runner = new JobRunner((job) => generate(rpc, cwd, job));
    console.log('连接成功。返回网站，选择“使用 Luna Max”即可聊天。');
    let failures = 0;
    while (!interrupted) {
      let polled;
      try {
        polled = await postSite(site, 'connector/poll', { connector_token: token }, 30000);
        failures = 0;
      } catch (error) {
        if (error.code !== 'subscription_network_error' || ++failures > 3) throw error;
        console.log('连接暂时中断，正在恢复连接；不会重复生成聊天。');
        await sleep(1500 * failures);
        continue;
      }
      if (!polled.job || interrupted) continue;
      const result = await runner.run(polled.job);
      // Retry only delivery of the same saved result, never model generation.
      for (let attempt = 0; ; attempt++) {
        try {
          await postSite(site, 'connector/result', { connector_token: token, ...result });
          break;
        } catch (error) {
          if (error.code !== 'subscription_network_error' || attempt >= 2) throw error;
          await sleep(1000 * (attempt + 1));
        }
      }
      if (result.error) throw new ConnectorError(result.error);
      console.log('已完成一次请求。');
    }
  } finally {
    await rpc?.shutdown();
    process.removeListener('SIGINT', stop);
    process.removeListener('SIGTERM', stop);
    // Only this connector's freshly created empty directory is removed.
    if (path.dirname(cwd) === path.resolve(tmpdir()) && path.basename(cwd).startsWith('snow-codex-chat-')) {
      rmSync(cwd, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(FRIENDLY[error?.code] || '连接器未完成操作。请确认版本、网络和配对状态后重试；不会自动重发聊天。');
    process.exitCode = 1;
  });
}
