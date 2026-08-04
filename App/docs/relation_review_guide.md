# 叙事关系二次审核与人工审批指南

本指南用于处理 `App/runtime/review/narrative_relation_candidates.jsonl` 中已有的叙事关系候选。候选由第一阶段抽取模型生成；二次审核模型（例如 DeepSeek）只负责独立核验证据，**不会修改候选状态、不会自动映射图谱节点、不会写入图谱**。

正式图谱目前仍只有一条入口：在网页中由人类审核员明确批准单条候选，并填写合法的源/目标图谱节点 ID。

## 一、先配置独立的二审供应商

在 `App/.env` 中配置 `RELATION_REVIEW_*` 变量。不要复用 `RELATION_CANDIDATE_*`：抽取与二审应使用相互独立的模型配置，避免二审只是重复第一阶段的偏差。

```dotenv
RELATION_REVIEW_PROVIDER=openai-compatible
RELATION_REVIEW_BASE_URL=<DeepSeek 或其他兼容供应商的 base URL>
RELATION_REVIEW_API_KEY=<你的私有 API Key>
RELATION_REVIEW_MODEL=<供应商模型名>

# 对不认识 enable_thinking 字段的供应商，保持为空。
RELATION_REVIEW_REQUEST_ENABLE_THINKING=
RELATION_REVIEW_JSON_MODE=true
RELATION_REVIEW_MAX_ATTEMPTS=3
RELATION_REVIEW_RETRY_BACKOFF_SECONDS=2
RELATION_REVIEW_TIMEOUT_SECONDS=120
RELATION_REVIEW_LONG_PROMPT_TIMEOUT_SECONDS=180
RELATION_REVIEW_LONG_PROMPT_CHARS=8000
RELATION_REVIEW_MAX_EVIDENCE_CHARS=9000
```

不要把真实 Key 写进 README、命令历史、截图、Git 或聊天消息。`.env` 已被忽略，不会被纳入仓库。

也可以只在当前 PowerShell 会话临时配置：

```powershell
$env:RELATION_REVIEW_PROVIDER = "openai-compatible"
$env:RELATION_REVIEW_BASE_URL = "<base URL>"
$env:RELATION_REVIEW_API_KEY = "<API Key>"
$env:RELATION_REVIEW_MODEL = "<model name>"
```

从 `App/` 路径运行命令。PowerShell 临时变量只在当前窗口有效；关闭窗口后需重新设置。

## 二、先做无调用检查，再做小样本校准

先确认即将处理的数量。下面命令不调用 API、不写任何审核报告、不改候选或图谱：

```powershell
python -m pipelines.review_relation_candidates --dry-run --limit 10
```

第一次真实调用只审核十条未缓存候选：

```powershell
python -m pipelines.review_relation_candidates `
  --limit 10 `
  --workers 1 `
  --run-name deepseek-calibration-10
```

确认 JSON 输出中 `failed` 为 `0`，再逐步扩大到 50 条：

```powershell
python -m pipelines.review_relation_candidates `
  --limit 50 `
  --workers 1 `
  --run-name deepseek-calibration-50
```

模型接口稳定且供应商允许时，后续可把 `--workers` 提高到 `2`，再谨慎提高到 `4`。这里使用的是普通 OpenAI-compatible 请求加有限并发，**不是供应商专用 Batch API**；遇到 429、超时或限流时应先降低并发。

每次运行都会原子写入以下独立工件：

```text
App/runtime/review/relation_model_review_reports.jsonl
App/runtime/reports/review_relation_candidates.json
```

下一次使用相同模型、相同 Prompt 版本和相同证据运行时，已完成的候选会自动跳过；失败项会重试。不要手工修改 `narrative_relation_candidates.jsonl` 或模型报告 JSONL。

## 三、模型结论的含义

二审模型只能给出建议，不是事实裁决：

| 建议 | 含义 | 人工动作 |
|---|---|---|
| `recommend_approve` | 原文直接支持、关系类型和字面身份看似明确 | 进入抽检池；仍需人工核对原文和图谱映射 |
| `recommend_reject` | 证据不足、关系低价值、或本地规则已排除 | 默认不优先处理；不要因为模型建议就自动驳回 |
| `abstain` | 身份、时间线、关系强度、语境或证据不够明确 | 必须由人工处理，或暂时保留 |
| `local_policy` | 例如纯 `MENTIONS`、缺少证据、引文找不到 | 未调用模型；只是本地风险提示 |

即使 `recommend_approve`，若关系来自时装、生日、邮件、活动或随机事件，也必须检查它是否只是特定情境。时装语气、临时互动、一次性任务和活动剧情不能默认写成角色本体的长期人格或稳定关系。

当前二审策略还会在本地执行两层高精确度门槛：建议批准的引文必须逐字包含候选的主体、客体，以及能表达该关系类型的谓词（例如“喜欢”“上司”“来到”“她的”）；仅有人名共现或相互称呼会降为 `abstain`。此外，来自特殊邮件、随机事件、时装、生日或活动页面的 `ALLY_OF`、`OPPOSES`、`HAS_RELATIONSHIP_CONTEXT` 一律保留给人工确认，不自动进入建议批准池。

## 四、网页中的人工审核操作

先启动本地预览服务（若尚未启动）：

```powershell
python -m backend.snow_app.main
```

在另一个 PowerShell 窗口启动静态预览：

```powershell
python scripts/dev_server.py
```

打开 `http://127.0.0.1:8080/workspace/`，进入“叙事关系人工审核”。运行二次审核后刷新页面；若 API 长时间未显示新报告，可重启 API 或调用 `POST /api/v1/admin/reload-artifacts`。

推荐的人工流程不是浏览全部 6,000 多条，而是按以下顺序执行。

1. 在“独立二审建议”筛选中选择“模型建议批准”。
2. 设置固定抽样种子，例如 `deepseek-approve-audit-01`，抽取 50 个分层样本组。
3. 展开每个组。逐条查看二审建议、模型支持引文、完整证据片段、来源类型和图谱节点建议。
4. 只在下列检查全部通过时点击“批准并写入图谱”。
5. 若原文不能直接支持、实体身份不明确、映射不正确、或仅是临时情境，点击“驳回”。
6. 用不同种子重复抽样；建议在建议批准池中累计人工审核至少约 150 条候选后，再评估模型精确度。

网页会显示组级摘要，但**批准按钮永远作用于一条候选**。同组三元组可能有不同的证据质量，因此不要把同组数量当成自动批量批准依据。

### 每条候选的批准检查表

批准前必须同时满足：

1. **原文直接性**：模型显示的支持引文能在展开的完整证据中逐字找到，并且直接表达该三元组。
2. **实体身份**：主体和客体不是代词、称谓、页面名、章节名、邮件名或未经确认的别名。
3. **关系类型**：
   - `HAS_PREFERENCE` 必须有明确喜欢、讨厌、偏好或意愿；
   - `OWNS_ITEM` 必须有明确拥有、携带、收藏、佩戴或个人使用；
   - `PARTICIPATES_IN_EVENT` 必须是实际发生的叙事行动，不是卡池、奖励或活动公告；
   - `ALLY_OF`、`OPPOSES`、`HAS_RELATIONSHIP_CONTEXT` 不能只由同场出现、一次争执或称呼推出；
   - `VISITS_LOCATION` 必须明确前往、到达或访问地点；
   - `MENTIONS` 通常不写入长期图谱。
4. **时间和语境**：时装、生日、邮件、活动、随机事件的临时内容不能污染角色本体设定。
5. **图谱映射**：填写存在的节点 ID；不要把 `page`、`story`、`mail`、`voice` 或 `random_event` 作为端点。

填写审核人标识和审核备注。建议备注说明“原文直接陈述”“时装限定，不入图”“别名未确认”“事件只为一次性情境”等原因，便于后续追溯。

## 五、如何判断二审模型是否足够可靠

模型建议批准池的人工抽检结果才是质量证据。建议记录每个抽样种子、抽样组数、实际审核候选数、人工批准数、人工驳回数和主要错误类别。

一个保守的初始门槛：

- 先审核至少 150 条 `recommend_approve` 候选，覆盖主线、个人故事、好感故事、邮件、随机事件等来源；
- 对 `ALLY_OF`、`OPPOSES`、`HAS_RELATIONSHIP_CONTEXT` 等高影响关系单独留足样本；
- 若发现身份混淆、关系强度夸大、时装语气污染或机制信息误入，先收紧 Prompt/规则并重新测试；
- 只有当抽检的错误率达到你可接受的门槛后，才讨论“经审计后的批量入图”策略。

当前实现**故意没有自动批量写图谱功能**。这是为了先用真实 DeepSeek 输出建立质量基线；否则错误边会直接污染后续角色人格和对话检索。抽检结果稳定后，再根据你的可接受错误率设计可回滚的批量提升规则。

## 六、常见情况

- `status: skipped_provider_disabled`：尚未设置 `RELATION_REVIEW_PROVIDER=openai-compatible`，或没有可调用的独立供应商。
- 缺少 `BASE_URL / API_KEY / MODEL`：二审变量不完整；它们不会回退到 Qwen 抽取变量。
- `failed > 0`：直接重复运行同一命令即可重试失败候选；先检查供应商限流、模型名、Base URL 和超时设置。
- 供应商报 `response_format` 不支持：设置 `RELATION_REVIEW_JSON_MODE=false` 后先重新跑 10 条校准样本。
- 报告显示 `abstain`：这是保守行为，不代表关系错误；它意味着不应由模型替代人工判断。
- 网页没显示报告：确认报告存在于 `App/runtime/review/`，然后刷新队列、重启 API 或调用重载接口。

## 七、不可绕过的安全边界

- 二审模型不拥有批准权限；
- 人工批准前必须提供关系兼容的现有图谱节点 ID；
- 未经批准的候选和模型报告不会进入正常图谱检索；
- 聊天生成仍处于阶段锁定，二审不会开启角色对话功能；
- 所有报告保留候选 ID、证据文档 ID、输入哈希、Prompt 版本、供应商/模型名和时间，方便比较、复跑和回滚。
