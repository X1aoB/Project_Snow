# Project Snow Application

## Public immersive surface

`backend.snow_app.public_main` is a separate registration-free application for `snow.xiaob.dev`. It exposes only `/public/v1` and serves the public immersive experience: 22-character search and switching, text communication, in-person scenes, structured action/dialogue blocks, cross-channel continuity, local transcript/history controls and anonymous feedback. Version 0.9.6 keeps the 18 approved Mia face and full-body assets, moves the in-person dialogue into a light frosted stage overlay, and adds sparse model-authored `stage_motion` presentation cues that remain independent from visible action prose. Other characters continue to use the independently verified avatar package. It retains the 0.9.5 presence, provider and IndexedDB v4 behavior. Stable assistant `displayBlocks` and valid stage cues remain stored beside messages so a segmented reply survives rendering, switching and reload without replaying historical animation. A subject-bound signed `public-state-2` package carries daily presence and optional rendezvous state. BYOK credentials use fixed, non-renewing 12-hour AES-GCM envelopes bound to an anonymous HttpOnly cookie; the browser keeps the envelope only in the current tab's `sessionStorage`. The internal `/api/v1` workspace is not mounted.

Provider adapters exist for OpenAI, DeepSeek, Alibaba Cloud Model Studio, Zhipu and Moonshot. `PUBLIC_ENABLED_PROVIDERS` is empty by default; enable each adapter only after a real-key smoke test. Custom base URLs are not accepted.

Local entry points:

```powershell
Copy-Item .env.example .env.local
.\scripts\local.ps1 Start
.\scripts\validate_all.ps1
.\scripts\local.ps1 DataLab
```

Production deployment, graph quarantine, data release, backups and the second-approval public gate are documented in [docs/public_deployment.md](docs/public_deployment.md). The `data-lab` profile is a Docker Compose simulation with Kafka KRaft, Spark master plus two workers, Hive Metastore and MinIO; it is not a production multi-host cluster.

`App/` 是 Project Snow 的本地应用层：它读取上级 `Data/` 中已采集的资料，建立
运行时检索索引，并提供 22 名角色的聊天测试客户端、证据工作台和安全的 Electron
桌面壳。`Data/` 保持只读；所有可再生成或私密的产物均写入 `App/runtime/`。

当前本地应用版本为 `v0.5.0`。`/` 是固定入口页，正式聊天界面位于 `/immersive/`；
`/workspace/` 使用固定侧栏承载证据、审核、反馈与对话调试。旧的通用 AgentRuntime、Provider
Registry、多模态附件与审批状态机只保留为内部调试基线，不再拥有单独的正式产品入口。

## 目录

```text
App/
├── backend/       FastAPI API、检索、会话与媒介规则
├── frontend/      入口页、沉浸式客户端和工作台
├── pipelines/     湖仓、检索、人格资料、图谱与审核管线
├── client/        不含凭据的 Electron Windows 桌面壳
├── docs/          数据契约、架构与审核说明
├── tests/         应用层测试
└── runtime/       本地索引、反馈、日志和聊天记录（Git 忽略）
```

## 前置条件与安装

- Python 3.11 或更高版本。
- 可选：Node.js 20 或更高版本（仅桌面客户端）。
- 可选：一个 OpenAI-compatible 模型 API。没有模型密钥时，页面和证据工作台仍可启动，
  但不能生成真实聊天回复。

在 PowerShell 中执行：

```powershell
cd C:\Users\25685\Desktop\Myprojects\Project_Snow\App
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
```

`requirements.txt` 是本地运行的完整 Python 依赖清单：FastAPI/HTTP 客户端、
dotenv 配置加载、资料/头像处理，以及检索与可选图谱组件。测试使用 Python 标准库
`unittest`，无需单独安装 pytest。文档/媒体依赖同时包括 PyPDF、PyMuPDF、python-docx、openpyxl、
python-pptx、Pillow、Mutagen 和 ReportLab；浏览器任务使用 Playwright Chromium，凭据通过 keyring
写入 Windows Credential Manager。

## 模型配置（不要提交密钥）

在 `App/.env` 中填入本地测试所需的模型变量；`.env` 已被 Git 忽略。保留
`.env.example` 中的其他配置，并只修改以下值：

```dotenv
MVP_CHAT_ENABLED=true
MVP_CHAT_PROVIDER=openai-compatible
MVP_CHAT_BASE_URL=https://your-provider.example/v1
MVP_CHAT_API_KEY=replace-with-your-local-key
MVP_CHAT_MODEL=your-model-name
MVP_CHAT_TIMEOUT_SECONDS=120
MVP_CHAT_IMMERSIVE_TEMPERATURE=0.45
MVP_CHAT_ASSISTANT_TEMPERATURE=0.20
# 助手模式可按请求展开更详细的回答；这是可见执行摘要，不是隐藏思维链
MVP_CHAT_ASSISTANT_MAX_TOKENS=8192
MVP_CHAT_WEB_TIMEOUT_SECONDS=15
MVP_CHAT_WEB_MAX_RESULTS=5
```

进程环境变量会覆盖 `.env` 中的同名变量。关系抽取/复核配置与聊天模型配置彼此独立；
请使用 `RELATION_CANDIDATE_*`、`RELATION_REVIEW_*` 和自动 Batch 审核专用的
`EVIDENCE_REVIEW_*` 变量配置它们。任何真实 API
Key、令牌、Cookie、私钥或导出的聊天记录都不应进入 Git。

沉浸式模式默认使用 `0.45` 的较低但非固定采样温度，以避免所有日常回应落成同一种短句；
助手模式默认 `0.20`，以优先保证任务说明的稳定性。可用 `MVP_CHAT_TEMPERATURE` 统一覆盖，
或使用两个按模式区分的变量单独调整；值会被限制在 `0` 到 `1`。

## 建立本地运行时资料

已有 `Data/` 时，首次建立或资料更新后可执行：

```powershell
cd C:\Users\25685\Desktop\Myprojects\Project_Snow\App
python -m pipelines.run_stage --stage b --skip-vector
python -m pipelines.run_stage --stage c
python -m pipelines.build_dialogue_profiles
python -m pipelines.build_mvp_views
```

如需本地语义向量索引，再运行：

```powershell
python -m pipelines.build_vector_index
```

上述命令只会写入 `App/runtime/`，不会改写 `Data/`、正式图谱边或原始资料。

如需把审核后的公开人格资料交给独立集成端，可导出版本化、带 SHA-256 的脱敏 bundle：

```powershell
python -m backend.snow_app.persona_export `
  --output ..\persona-bundles\project-snow.persona-bundle.zip
```

导出器不会打开私聊、用户事实或 Agent 数据库，也不会复制原始 `Data/` 或本机路径。完整边界与
独立导入端见 [Snow Role Assistant](https://github.com/X1aoB/Snow_Role_Assistant)。

## 启动浏览器客户端

先启动 API：

```powershell
cd C:\Users\25685\Desktop\Myprojects\Project_Snow\App
.\.venv\Scripts\Activate.ps1
python -m backend.snow_app.main
```

然后在第二个 PowerShell 窗口启动静态 Web 服务和 API 代理：

```powershell
cd C:\Users\25685\Desktop\Myprojects\Project_Snow\App
.\.venv\Scripts\Activate.ps1
python scripts/dev_server.py
```

- 聊天客户端：<http://127.0.0.1:8080/>
- 证据、审核、反馈收件箱和对话调试工作台：<http://127.0.0.1:8080/workspace/>
- API 健康检查：<http://127.0.0.1:8080/health>

聊天客户端提供沉浸式陪伴，并在面对面/文字通讯之间切换。完整显示历史、当前场景和共享关系前提
仅保存在本机 `runtime/chat/conversations.sqlite3`；生成时仅使用受限历史和明确共享上下文。
正式界面不暴露工具、Agent 状态或内部系统概念。旧的只读工具和 Agent 调试能力仍可在内部工作台
检查，但联网结果不会写入 `Data/`、人格档案或图谱。

在设置中新增 Provider 后，可以为会话选择已探测模型，也可勾选“仅本轮”。图片、文档和录音从
输入区上传到 `runtime/chat/attachments/` 并按 SHA-256 去重；视觉或语音附件只有在 Provider
能力与数据授权都满足时才会离开本机。勾选“作为 Agent 执行”后，客户端显示真实步骤、审批、
停止/重试按钮、实际模型、用量和 Artifact 下载。Agent 默认限制为 20 步、15 分钟和 2 个并发任务。
扫描 PDF 会在本地渲染有限页供视觉模型读取；录音会先生成可编辑转写。Agent 还可以使用授权目录
文件、PowerShell、Git、Playwright 和已配置账号连接器，但最终网页提交、邮件发送、日程/云端修改、
Git push 及删除均需审批，删除要求二次确认。

助手在评价现实事务时会分开标注已核实事实、用户给定前提和条件判断，并在证据允许的范围内
给出明确的角色化观点，不再用“是否需要我继续搜索”替代已经要求的分析。公开行情数据可能延迟，
交易决策仍应复核交易所或券商数据。

面对面会话的输入区同时提供动作/神态和对白两个输入框：可仅发送其中一种，也可在同一轮
组合发送，并会作为分离的内容块写入本地会话历史；文字通讯会完全隐藏动作输入框，只发送
文字消息，不能把实际的面对面动作当作已经发生。角色的面对面回复可以包含简短的自身神态/
动作块，但不会替分析员编造反应或接触。

## 22 名角色与资料边界

选择器的角色来自统一注册表。每名角色都显示直接资料、关联资料和覆盖状态；资料较少
的角色会使用保守表达，不会借用其他角色的私人偏好、口癖或关系。

检索优先使用当前角色的直接资料，然后是已建立的装甲/小队/武器等关联资料、明确提及
角色的剧情与共享世界背景。时装仅在用户明确提及对应时装时作为情境上下文，不会默认
覆盖角色本体设定。未审核关系仍是临时证据，不能自动修改正式人格设定或图谱。

## Electron Windows 测试客户端

桌面客户端是浏览器页面的安全薄壳，不复制 `Data/`、Python 环境或模型凭据。先按上文
启动 API 和 Web 服务，再执行：

```powershell
cd C:\Users\25685\Desktop\Myprojects\Project_Snow\App\client
npm ci
npm start
```

可在本机打包 Windows portable 测试程序：

```powershell
npm run smoke
npm run package:win
```

生成物位于 `client/dist/`，已被 Git 忽略，且仍依赖同一台机器上运行的本地 API 与 Web
服务。

角色头像可在本机通过 `python scripts/build_character_avatars.py` 从已有资料生成。Wiki 图片
属于本地测试媒体，不随仓库推送；当它们不存在时，客户端会自动回退为角色文字头像。

若使用 `docker compose`，还需要在本机 `.env` 设置 `POSTGRES_PASSWORD` 和
`NEO4J_PASSWORD`。示例文件只提供占位值，不能作为真实部署密码。

## 验证与故障排查

```powershell
cd C:\Users\25685\Desktop\Myprojects\Project_Snow\App
python -m unittest discover -s tests -p "test_*.py"
python scripts/validate_architecture.py
```

若页面无法连接，请先确认 API 终端仍在运行，然后访问 `/health`。模型请求失败时，检查
`MVP_CHAT_ENABLED`、endpoint、模型名和本机 `.env` 是否正确；不要把 `.env` 内容复制到
issue、截图或 Git 提交中。反馈会以追加记录写入 `runtime/mvp/`，可在 `/workspace/` 的
反馈收件箱中筛选和标记处理状态。

更多实现约束见 [架构说明](docs/architecture.md)、[数据契约](docs/data_contract.md)、
[关系审核指南](docs/relation_review_guide.md)、[实体审核指南](docs/entity_node_review_guide.md) 和
[Qwen Batch 自动审核指南](docs/qwen_batch_review_guide.md)。

## Git 与隐私

提交前建议执行：

```powershell
git status --short
git diff --check
```

应提交应用源代码、模板、测试、文档、固定前端资源和依赖锁文件；不应提交 `Data/`、
`runtime/`、`.env`、`node_modules/`、Electron 打包产物、SQLite 数据库、反馈/基准输出
或任何凭据文件。
