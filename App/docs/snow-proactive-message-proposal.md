# 角色主动来消息：已批准方案与后端实现

状态：小B在本会话明确同意本方案及两批合并交付，已开始并完成后端实现。服务端开关和浏览器主动消息偏好仍默认关闭；此次批准是实施授权，发布仍需独立候选及具体回执批准。普通聊天、免费约定提醒及历史流程不新增模型调用。

## 用户行为与费用边界

默认关闭，服务端总开关也默认关闭。用户主动开启时明确确认：「每条消息会调用一次模型并产生费用；订阅模式消耗订阅额度。」确认关联匿名主体、当前模型会话及告知版本；切换模型会话后重新确认。可以随时关闭。

只有页面打开时，已存在的当天约定提醒到点才有资格触发。先生成免费的本地提醒，再尝试主动消息；没有服务器定时器、系统推送、离线补发或自动赴约。页面恢复可见、focus 和启动时重查提醒；过期约定不触发。

## 端点及验证

新增独立 `POST /public/v2/rendezvous/consent` 和 `POST /public/v2/rendezvous/message`，下文列出实际字段。沿用现有同源、匿名 cookie、体积、IP 限流、容量及模型会话限制。服务端验证签名、主体、香港当天日期、角色、约定 authority 仍有效及费用确认，不信任客户端自行宣称的约定状态。排队结束、原子占额之前再次校验，防止排队跨午夜或凭据过期。

角色的签名上下文可以早于全局世界包的 revision：其他角色的聊天或活动会推进全局版本，但不会重签当前等待角色的上下文。接受时仍要求主体、角色、会话段、当天日期、文字通讯通道及完整 pending_rendezvous 精确匹配，并查询未关闭的约定 authority；上下文 revision 晚于世界包则拒绝。取消或赴约后的旧签名包不会因为这个兼容规则恢复效力。

每次生成最多一次模型调用，不做修复生成、兼容重试或自动补发。BYOK 沿用原凭据，订阅沿用原连接及 preflight，不增加新的收费通道。未配置模型或订阅未就绪时只保留普通提醒。

## 幂等和日额度账本

每约定最多一条、每匿名用户每天最多三条，服务器原子占额，多标签页共享。现有 public_request_cache 完成结果仅保留十分钟（public_store.py 的现有回收契约），不能承担这个日内约束。

已增加独立日内账本：`public_proactive_day` 的 `(subject_hash, schedule_date)` 行在 PostgreSQL 内加事务行锁，`reserved` 数据库约束为 0–3；`public_proactive_attempt` 持有固定 request ID、正文哈希及安全结果状态，并以 `(subject_hash, rendezvous_id)` 唯一，避免换 UUID 或跨天复用同一约定。两表不外键依赖短缓存，旧版本清理缓存也不会恢复额度。账本和 authority 保留至香港约定日结束后 48 小时，正文只存在现有十分钟结果缓存。正文过期后返回不可恢复的未知结果，不重新调用模型。

`public_rendezvous_authority` 单独记录已提交的约定创建和永久关闭 tombstone。创建/关闭与原场景或聊天结果在同一数据库事务提交；取消、赴约和改期后，旧签名 pending 包不能重新打开同一 ID。只读 resolve、缓存恢复和客户端上送的签名包不补登记。升级前没有新服务端 authority 的旧约定只保留免费提醒；重新建立约定后才能生成主动消息。这是有意的安全退化，不能仅凭“曾签名等待”的旧快照证明仍然有效。

迁移 `20261019_0009` 只新增三张表，不改变已有表和 public v1 字段。普通应用回滚保留新表；只有停止所有新 worker 且保留期到期后才允许行政 contract/downgrade。测试使用独立真实 PostgreSQL，并加载实际旧提交 `95005fe` 完成 old/new/old：旧代码写入、升级、新代码占额/完成、旧代码继续写入及清理短缓存，日额度仍然保留。

请求结果不确定或响应丢失时，只按原 requestId 和原正文哈希进行只读结果恢复；不得换 ID 重发。结果未确认保留占额并显示「结果未确认」。订阅的 subscription_result_unknown 继续阻断再次付费生成。失败不影响已有免费提醒。

## 消息与数据边界

成功消息来源为 proactive_reminder，只有角色文字；不伪造用户台词、不移动场景、不创建约定或共同经历，不改变原回复计时。保留真实 usage 并纳入花费透明；厂商未返回 token 时保留 null。

服务端仅记录安全状态和不可逆主体标识；统计、日志和反馈附带内容不放聊天/记忆正文、模型凭据或私人 Provider ID。

## 已批准的产品决定

- 仅对当天的已有约定提醒触发，页面打开才生效。
- 默认关闭，开启有费用确认；切换模型会话重新确认。
- 每约定最多一条、每天最多三条、每条最多一次调用。
- 不确定结果保留额度且只读恢复，失败不自动补发。

## 客户端接口

`GET /public/v1/config` 增加：

```json
{"proactive_messages":{"enabled":false,"notice_version":"proactive-cost-1","max_per_day":3,"max_per_rendezvous":1,"recovery_only":true}}
```

服务端环境变量 `PUBLIC_PROACTIVE_MESSAGES_ENABLED` 缺省为 `false`；同时需要已有 `scene_actions_v2` 开启。关闭只阻止新付费生成，保留按原 request ID 和业务正文恢复结果的能力。没有服务器定时器、推送或离线补发。关闭浏览器偏好即停止本地调度，不需额外模型请求。

费用确认请求：

```json
{"provider":"openai","model":"用户当前模型","reasoning_effort":null,"credential":"会话凭据","model_session_id":"当前模型会话UUID","notice_version":"proactive-cost-1","accepted_cost_notice":true}
```

返回 `consent_token`、`notice_version`、`model_session_id`、`expires_at`。费用 token 使用独立 HMAC domain，绑定匿名主体、原凭据 SHA256、provider/model/effort、实际订阅 effort、模型会话 UUID 和告知版本。到期不晚于原凭据或 12 小时；切换模型、模型会话、凭据或订阅默认 effort 后必须重新确认。`accepted_cost_notice` 必须是 JSON 布尔 `true`。

消息请求：

```json
{"request_id":"固定UUID","character_id":"公开角色ID","rendezvous_id":"签名约定ID","segment_id":"原会话段UUID","state_package":"原始签名世界包","session_context_package":"原始签名上下文包","provider":"openai","model":"用户当前模型","reasoning_effort":null,"model_session_id":"费用确认时的UUID","notice_version":"proactive-cost-1","credential":"会话凭据","consent_token":"费用确认token"}
```

返回 `request_id`、`character_id`、`source: "proactive_reminder"`、`status: "completed" | "unknown" | "failed"`、`message_id`、仅含 `message` 类型的 `content_blocks`、`usage`、`model_called`、`terminal_error` 和 `idempotent_replay`。没有用户消息，没有世界包、上下文更新、移动或共同经历写入。usage 保留一次调用和厂商实际 token 数，缺失字段为 `null`。

恢复使用同一端点、原 request ID、原业务正文，加 `X-Snow-Recovery-Only: 1`；允许清空 `credential` 和 `consent_token`，这两个 bearer token 不进入业务哈希。其他字段不可改变。恢复只 SELECT 账本/缓存，不 claim、不归还额度、不调用模型。公共 HTTP 边界仍执行原有安全限流。

`request_in_progress` 表示原请求仍在处理；完成回执重放原结果；缓存过期或 worker 丢失后返回 `status: "unknown"` 和 `proactive_result_unavailable`。原请求不存在时只读恢复返回 410。已有约定换 UUID 返回 `rendezvous_already_used`；当日三额已占满返回 `proactive_daily_limit`；约定关闭或未登记返回 `rendezvous_not_active`。以上都不自动补发。

结果正文缓存过期或 worker 失联时，日内账本只证明原尝试占额，不能证明模型已接单。此时 `usage.provider_calls`、token 数和 `model_called` 均为 `null`，不会把原本未提交、0 次调用的失败变成 1 次收费。已取得实际结果时保留真实的 0/1 调用和 `model_called`；未知尝试仍保留日额度。

## 验证与交付边界

测试全部使用合成约定、合成聊天和可观测模型边界，真实付费调用为零。覆盖费用确认绑定/到期、默认关闭、同源 cookie HTTP 流程、实际 chat v2 提交和 scene 取消挂钩、同约定幂等、多标签页并发占额、结果未知保额、丢失提交 ACK、缓存过期、过期租约及实际旧代码回滚兼容。

Kiro 继续负责开启确认、提醒卡、未知结果及用量的界面与截图；后端完成不代表 UI 人审通过，也不代表发布批准。
