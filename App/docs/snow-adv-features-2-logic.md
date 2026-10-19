# 雪境新功能：第一次上线逻辑交接

基线为 `fff998beddb413406ae477317fd80e23013ca0e3`。先快进合入 Kiro 已签名的头像提交 `bbb777fe82ab86369065901b9403b147373ba6f5`，并保留同一分支的菜单可读性修正 `4a40116f79287ebe8912c867678920573d4b5687`；没有 rebase 或重签。当前阶段交付逻辑供 Kiro 接界面，尚无新功能界面、公告、PR 合并或候选发布。

## 本次范围与文件归属

GPT 已接入记忆档案、台词收藏、完整历史搜索、花费透明，以及 22 位角色活动。图片卡、界面样稿、样式、图标、UI 测试与截图均由 Kiro 接手。活动依据见 [snow-character-activities.md](snow-character-activities.md)。

`app.js` 仅接宿主、持久化和请求保护；纯逻辑在 `modules/snow-feature-logic.js`。`modules/snow-features.js` 目前仅有无 DOM 的 `installSnowFeatures(host)` 空入口，明确留给 Kiro 替换。共用入口在首屏稳定后的同一个回调里安装一次，且只在雪境开关开启时加载。不要再逐项向 app 添加动态 import。

分页、已读、文字速度和提醒属于第二批，本次没有 `logic.reading`、`logic.reminders` 或 `stage.setPager`。现有回复呈现和计时路径保持原行为。主动消息仅有 [待小B审阅的方案](snow-proactive-message-proposal.md)，无端点、调度器或模型调用实现。

## Kiro 可使用的宿主

```js
host.version === 1
host.icon(name, options)
host.avatarOf(character)
host.snapshot()
host.logic
host.stage.speechNode()
host.open.presence(characterId)       // 现有地图前往/确认流程；不会自动赴约
host.open.transcript({ messageId })   // Promise<boolean>；可省略参数
host.open.settings(tab)
host.open.info()
host.on(type, listener)              // 返回取消订阅函数
```

`snapshot()` 返回副本：`selected, characters, scene, channel, busy, busyFor(characterId), worldPackage, localDayKey, movementCatalog, modelReady, writerOwned, historyEpoch, tone, toneTime`。`localDayKey` 与应用一致，按香港时间计日。`busyFor` 是实时只读函数；角色有聊天、打字呈现、到场、场景动作或整理任务时返回忙。宿主的 `busy` 也包含正在落盘的记忆修改。不要把含函数的 snapshot 整体 structuredClone。

事件每位监听者收到独立副本，事件异常不影响原请求：

| 类型 | 参数 |
| --- | --- |
| `selected-changed` | `{selected}` |
| `channel-changed` | `{characterId, channel}` |
| `message-saved` | 消息本身，非 `{message}`；保存的新增/变化消息 |
| `reply-presented` | `{characterId, messageId, requestId}`；现有首次有效绘制点 |
| `world-changed` | `{worldPackage}`；通过原世界包接受门槛后 |
| `tone-changed` | `{tone, toneTime}`；暮色 tone 为 dark，toneTime 为 dusk |
| `history-changed` | 无参数；内部用于导入/删除/清理后的缓存重读 |

第一批没有 `reminder-due`。安装前发生的事件不补发，安装后用 snapshot/logic 读取当前状态。

## 记忆接口

`list/count/remove/restore` 与交接 11.4 一致。补充 `logic.memories.clearUndo(characterId?)`：Kiro 关闭弹窗时必须调用，只允许在当前打开周期撤销；历史重读也清撤销。

实际 `kind` 全集为 `user_statement`、`assistant_commitment`、`shared_scene_event`，后端契约在 `public_contracts.py`；界面中文标签由 Kiro 映射。

删除先持久化 `ui_preferences.deletedMemoryIds`（最多 500），再移除原回执；撤销先写回含 proof 的原始回执，再移除删除标记。请求候选与返回合并均过滤删除标记。持久化失败保留保守的删除标记，绝不制造或重签 proof。

记忆修改使用每角色写入门闩，覆盖偏好与回执两次异步保存；已有任务或只读标签页返回 `{ok:false, reason:"busy"}`。新聊天/场景动作在构造候选前等写入完成。恢复旧请求仍原样使用已保存的 request ID、正文和候选快照，不重新构造。

## 收藏、搜索与跳转

`favorites.list/has/toggle` 与交接 11.5 一致，最多 200 条，超限或无效锚点时 toggle 返回 false。收藏只接受 sent 消息，以 contentBlocks 的 UTF-16 字符偏移存锚点，禁止截断代理对；快照最多 240 个 Unicode 字符。收藏不会随原消息清理而删除。

搜索读取 IndexedDB 的 `by_character_created` 索引内该角色全部本地消息，不限已加载页。忽略大小写、兼容全半角 ASCII，返回的 ranges 仍映射回原文字的 UTF-16 偏移。空查询返回空数组，默认/最大 200 条块匹配。只在本机搜索。

`open.transcript` 加载目标附近窗口，保留正在进行的线程消息，并滚动目标至可见位置；消息不存在返回 false。图片卡由 Kiro 实现，按交接使用 canonical anchors、已加载字体、canvas.toDataURL 预览和 a[download]。

## 用量真实语义

`logic.ready` 是首次全历史用量缓存初始化 Promise，宿主等其完成后才安装入口。`usage.round(messageId)`、`usage.today()` 和 `usage.preferences()` 为同步副本读取。`usage.setPreferences({showUsage})` 异步持久化，默认开启；`refresh()` 可重读历史并清理撤销缓存。历史修改事件会自动触发重读，过期异步读取不会恢复已清理记录。

一轮按角色与 requestId 分组；到场自成一轮。按组内最早有效用户发送时间的香港日期归日，无用户记录时用该组最早有效消息时间；跨午夜回放不会使一轮重复归入两天，整理跟随触发轮。助手回放不重复计费，summary 按独立 requestId 去重。`parts` 的 kind 为 reply、arrival、summary。scene_action 显式 0 调用不计费。

返回的 `calls` 和 `tokens` 都可能为 null：未知调用次数不能显示成 0。若仅部分字段可知，返回已知合计并标 `partial:true`；全部缺失则 null。调用次数已知但所有 token 缺失时 tokens 为 null，显示「厂商未返回 token 数」；token 只有部分已知时合计带「约」。明确 0 调用为 0 token；有调用但 total_tokens 为 0 按未知处理。无金额估算，以厂商账单为准。

chat SSE 返回五项非负整数 token 字段：input_tokens、output_tokens、total_tokens、prompt_tokens、completion_tokens；从后端诊断补 provider_calls，但不返回 model。应用保存该请求发起时的 model。v2 到场也返回 provider_calls，旧 v1 到场可能缺失。订阅 connector 未收到用量事件时返回 {}，有事件但字段缺失时可能是 0，不能把这类 0 当真实零用量。

整理端点返回原厂商 usage，不补 model/calls。现有成功整理只做一次模型 POST、无自动重试，所以仅在成功上下文记录 provider_calls:1，并固定请求时 model。整理 usage 按启动时固定的 checkpoint message ID 挂在触发轮的助手消息 extraUsage 上；即使异步结束时有更新消息也不会改归属。

后端没有 token 完整性标志；同一个响应内部若曾合并多次调用且其中一次未给 token，前端无法恢复缺失信息。这里的 partial 准确反映前端可观察到的 parts 缺失，不声称能证明厂商内部多调用统计完整。

## 本地持久化和备份

IndexedDB 仍 v4，未新增 store。新增数据保留在现有 ui_preferences 和消息 extraUsage。旧偏好保存合并未知字段并串行落盘，避免钉选/贴纸设置擦掉收藏或删除标记。整个导入直到历史重读结束都排入同一队列；排队写绑定 historyEpoch 和偏好重读代数，清空或重读后旧快照不能重新落盘，也不会覆盖导入并集。普通连续保存仍逐项等待实际落盘，保证记忆删除标记先于回执删除持久化。

备份继续 schema project-snow-history-1：收藏、删除标记和用量随原导出路径往返。导入收藏按锚点 ID 并集、删除标记并集且最多 500；相同消息只合并 extraUsage，保留原正文、状态、来源和请求快照。同 summary requestId 去重且不让空回放擦掉已有已知数值。原三参数 mergeHistoryDatabase 保持旧行为，额外合并回调用第 4 参数，失败回滚同一事务。原签名会话 token 与记忆 proof 不扩大到可搬移备份中。

收藏、搜索、记忆正文和用量没有接入统计或反馈。用量缓存只留所需元数据，不留正文、凭据或 proof。public v1、public-state-2、请求哈希与恢复载荷兼容；本次不新增模型请求。

## 验证及下一步

最终新增验证：纯逻辑浏览器 27 项、真实宿主接入 12 项、后端活动 7 项（另 455 个子检查）全部通过；TypeScript 检查和 Node 23 项通过。宿主全集在最终导入修复后重跑，12 项均通过；两项竞态反向复现和修后证据保留在 `.tmp/features-2-validation/`。CI 的两个浏览器任务已纳入新测试，尚未对这次逻辑提交声称远端 CI 通过。

浏览器大回归包含既有 UI/网络/到场/节奏集合和当时收集的逻辑项，共 315 项、199 个子检查通过。之后针对最终修改重跑上述宿主全集；补入 Kiro 的 `4a40116` 后，菜单可读性测试另跑 1 项、3 个色调子检查通过。未重复运行无关的完整集合。

另抽查既有 application_layer 活动/世界测试：2 项通过、3 项因缺少 ca0144ccd81b / 1b0a6b35719a 的本地 runtime 资料失败。已在旧发布工作树独立运行相同命令，失败项与 FileNotFoundError 完全相同；未复制真实 runtime 或改变来源数据来让测试通过。首屏证据见 [逐文件哈希与体积](evidence/snow-adv-features-2-logic-size.json)：新增 57,474B，剩 3,966B；app 挂钩相对头像分支 `4a40116` 新增 2,047B gzip。口径为 14 个文件、LF、各自 gzip -9，相对 a1354cc 的新增量上限 61,440B；app.js 挂钩另限约 2KiB。

GPT 推送逻辑后暂停。Kiro 在同一分支接入口/界面、图片卡、公告和 UI 测试，检查 22 人地图活动的换行/重复。随后再由 GPT 补集成和 CI；PR 合并前仍需 Kiro 按交接第 6 节检查。候选和专属回执只有小B在本会话明确批准后才可晋级。
