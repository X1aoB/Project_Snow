# 使用自己的 Codex 订阅连接小吉终端

本功能通过官方 Codex App Server 使用每位访客自己的 ChatGPT 订阅。
Luna Max 对应 `gpt-5.6-luna`、推理强度 `max`，不是另一个 API 模型 ID。
网站不接收 OpenAI 登录凭据，也不把站长的账号共享给所有访客。

## 使用者教程

1. 在电脑上安装 Node.js 22 或更新版本、官方 Codex CLI 0.153.4 或更新版本：

   ```sh
   npm install -g @openai/codex@latest
   codex login
   ```

   选择 ChatGPT 登录，使用你已有的订阅账号。访问权限与剩余额度以当前账号为准。
   已安装的桌面版也可以提供官方可执行文件，连接器会自动寻找可运行的版本。

2. 打开网站“设置 → 模型 → Codex 订阅”，下载 `snow-codex-connector.mjs`。
   完整教程也直接放在这个界面中。

3. 点击“生成配对码”，在下载目录的终端运行：

   ```sh
   node snow-codex-connector.mjs --site https://snow.xiaob.dev
   ```

   检查终端显示的是你要连接的网站，然后在交互提示中输入配对码。
   配对码五分钟内有效且只能使用一次；不要把它放进 URL、命令参数或发给他人。

4. 网页显示已连接后，点击“使用 Luna Max”。保持电脑、网络和连接器运行。
   手机也可以访问网站并生成配对码，再在你自己的电脑上输入。
   关闭终端或在网站点击“断开连接”即可停止连接。

网站配对凭证最长十二小时有效，只保存在当前标签页的会话存储中。
站点重启、切换发布实例或过期后，需要重新配对。
登录凭据由官方 Codex 在本机管理，不需要复制 Cookie 或 `auth.json`。

## 常见问题

- **模型不支持**：连接器要求当前账号同时支持 Luna 和 max，不会静默替换模型。
- **找不到 Codex / Windows 提示 spawn EFTYPE**：先重新安装完整的官方 CLI。
  也可以用 `--codex "官方 codex.exe 的完整路径"` 指定已安装桌面版的可执行文件。
  不要下载来历不明的二进制。
- **只检查配置，不生成回复**：运行 `node snow-codex-connector.mjs --check`。
- **本地开发**：运行 `node snow-codex-connector.mjs --site http://127.0.0.1:8080 --allow-local`。
  HTTP 仅允许明确启用的回环测试地址，普通网站必须使用 HTTPS。
- **max 等待较久**：它使用较高推理强度，额度与限流取决于订阅计划。
  本站单次连接任务最多等待三分钟，实际还受当前模型请求期限约束。
- **结果未确认**：连接器会停止，不会重新提交同一个任务进行生成。
  先查看网站中该请求的结果和请求编号，不要立即重复发送同一条消息。
  官方 Codex 自身可能进行协议层重连；本站不能承诺所有网络异常都不消耗额度。
- **自动摘要暂停**：订阅模式下暂停后台自动摘要，避免摘要被新消息取消时产生不确定结果。
  原有开关偏好保留，切回 API Key 后恢复。到场反应和明确完成后的受控重写仍可能使用额度。

## 网站维护者部署说明

合并此候选代码并构建包含连接器的前端产物后，在该 API 实例的环境文件启用：

```dotenv
PUBLIC_SUBSCRIPTION_ENABLED=true
WEB_CONCURRENCY=1
```

无需将 OpenAI Key、登录 Cookie 或 Codex 的凭据安装到服务器。
`codex_subscription` 在启用后自动加入公开模型配置，不必改原有 API 厂商列表。
`PUBLIC_SUBSCRIPTION_ENABLED=false` 可关闭新连接入口；现有 API Key 路径保持原合同。

当前中转器只支持**一个活动 API 进程**。不要配置多个 Uvicorn worker，或对多个 API
副本轮询负载均衡。`WEB_CONCURRENCY` 不是 1 时功能会关闭；该检查无法探测独立启动的
其他副本，运维仍须保证单进程。蓝绿切流和进程重启会丢弃内存配对，用户重新配对即可。

连接器以 HTTPS JSON POST 向网站长轮询，不向本地电脑开放端口。
现有 Caddy 反向代理可直接承载 `/public/v1/subscription/*`，保留 Origin、JSON 和请求体
限制，并禁止缓存这些响应。无需放行任意第三方模型网关，也无需暴露 App Server WebSocket。
下载地址 `/downloads/snow-codex-connector.mjs` 随不可变前端包发布。

配对和任务均有容量与期限限制，单个连接只执行一个未完成任务。登录信息不经过本站；
网站本身仍会处理角色指令、消息、检索上下文和回复，不能把此模式称为端到端加密。
本机使用临时会话、空执行环境、只读且禁止网络的工具权限，并逐个关闭继承的 MCP、插件、
命令和通知入口；这里只传文字，不接受文件、图片路径或任意工具。

首次上线按项目既有候选验收与人工切流流程进行。普通 CI 不使用订阅生成，只测试模拟
协议、隔离、超时、配对撤销、跨用户访问和请求重放。

参考：[官方登录说明](https://learn.chatgpt.com/docs/auth)、
[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)。
