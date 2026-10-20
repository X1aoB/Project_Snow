# 雪境两批功能合成交接（给 Kiro）

在 `codex/snow-adv-features-2` 的 Kiro 签名提交 `95005fe2efade3e9c3b916e8c47c73fced5b19e0` 上继续；没有 rebase、重签 Kiro 的提交或修改 main。线上仍以已完成发布的 `fff998beddb413406ae477317fd80e23013ca0e3` 为本轮公告比较起点。

小B已在本会话明确批准主动消息方案及两批合成交付，授权 GPT 完成两批逻辑后推送，交 Kiro 接着做第二批界面。小B也明确批准 **手机分页时对白框加高约 60px**；由 Kiro 在分页排版里实现，并验证长台词不会被右下快捷按钮遮挡。此批准不替代 PR 合并前的 Kiro UI 检查或候选专属回执批准。

## 已完成与界面归属

第一批保留 Kiro 的正式记忆档案、台词收藏、搜索、图片卡和每轮用量界面，以及头像与活动扩充。补上真正指纹化的浏览器集成：禁止回退到原 JS/CSS 地址后，入口、逻辑、图标、样式、图片卡及第二批逻辑仍能通过哈希资源加载；关掉雪境旗标时新模块零加载。

第二批已提供阅读控制器、已读、文字速度、提醒调度、主动消息后端和宿主接入。全部新逻辑按需加载。**分页排版、页码/推进/自动进度、提醒卡、对话设置分类、费用确认弹窗尚待 Kiro 实现**；当前入口没有注册 pager，继续使用原对白呈现。实现细节见 [阅读与提醒逻辑](snow-adv-reading-reminders-logic.md) 和 [已批准的主动消息方案与后端合同](snow-proactive-message-proposal.md)。

## Kiro 接口与接入注意

`host.stage.setPager(pager)` 注册交接 11.6 的四个接口：`paginate(text)`、`render(state)`、`onAdvance(listener)`、`onToggleAuto(listener)`。分页区间和 `revealedEnd` 都是 UTF-16 绝对偏移；已读按原始 contentBlocks 的块号、字符偏移保存，不使用 displayBlocks。Kiro 在 resize、字体加载或对白框高度变化后调用 `host.logic.reading.reflow()`，保留当前绝对进度重新排版。

`logic.reading.preferences()/setPreferences(patch)`、`readUpTo`、`markRead`、`autoDelayMs` 依交接合同。`enabled()/owns(key)/active()` 供宿主判断是否已经接管；`wait` 表示最后一页完成，但手动翻页不会占住请求或输入锁。第一页真正出现非空文字才进入原来的首绘计时；文字速度只缩放逐字计划，不改请求和回复节奏。读屏只播报新页文字。标签页隐藏、弹窗、隐藏界面和输入期间暂停自动计时；自动仅本标签页保存，阅读偏好在 localStorage。

提醒使用 `logic.reminders.plan/set/clear/snooze/active/preferences/setPreferences`。邀请提醒必须在实际发送的 UUID 确定后调用 `plan`；宿主仅以同一终态请求的 `rendezvous-settled` 事件绑定约定，不能按地点碰巧相同绑定。`host.snapshot().rendezvous` 保留角色 ID。到点先持久化 fired，再发 `host.on("reminder-due", ({reminder,character})=>...)`；`reminders-changed` 刷新锁屏。锁屏新增 reminder 类型，点击沿用“是否赴约”的确认，不自动移动。提醒卡的“10 分钟后”“关闭”和图标样式由 Kiro 接入。设置说明必须写明仅页面打开时提醒、普通提醒不调用模型。

`host.logic.proactive` 提供：

```js
preferences()                           // enabled/requiresConfirmation/serverEnabled/noticeVersion/busy
enable({ acceptedCostNotice: true })     // Promise；必须由用户确认费用后调用
disable()                              // Promise
setPreferences({ enabled, acceptedCostNotice })
refresh()                              // 模型/凭据改变后清除旧确认
requests()                             // Promise，安全请求状态与实际 usage，不含请求包/凭据
recover(requestId)                     // 用户明确操作；只读恢复同一 UUID、原正文
```

开启前明确告知“每条消息会调用一次模型，产生费用；订阅模式会消耗订阅额度”。服务端与用户开关默认关闭；没有费用确认、配置好的模型、仍有效的当天约定、可写标签页和可见页面时不发生成请求。客户端在落盘后、fetch 前再核对模型与凭据；同意令牌仅在内存，切换模型/会话后重新确认。没有服务器定时器、系统推送或离线补发。

收费调用结果不确定时，不自动补发、不换 UUID；UI 显示“结果未确认”，只提供显式查询原请求。`requests()` 的调用数或 token 可能为 null，不能显示成零。failed/unknown 的安全用量也进入 `logic.usage.today()`；完成消息的 `round` 中 parts.kind 为 `proactive`。收费回执与完成消息按 requestId 去重。请给 proactive 明细加相应中文标签。

旧约定若从未被新后端登记，只有免费提醒；重建约定后才可用主动消息。新服务端记住取消/赴约的关闭墓碑，旧签名包不能重新启用约定。回滚到旧二进制后再前滚，主动消息总开关仍应关闭，重新建立约定后再开放。`proactive_messages_enabled` 仅通过后续独立发布配置开启，当前未更改生产配置。

## 数据和隐私

IndexedDB 继续 v4，不新增 store。已读在 `stage_read_v1:<characterId>`，每角色最多 200 条；提醒在 ui_preferences，最多 50 条。导入只合并今天的提醒；收藏和删除标记并集。messages.get 延迟读取在清历史、导入、代际改变和 dispose 后返回 null，避免旧正文返回。偏好写入按实际持久化顺序合并，失败不能把已触发状态当作落盘成功。

主动请求在 `proactive_request_v1:*`，不含凭据，同意令牌不落盘；原请求包不导出。`proactive_usage_v1:*` 仅存安全用量回执，导出时白名单复制到 ui_preferences.proactiveUsage，导入去重，支持用量备份往返。阅读备份仅带 textSpeed、autoDelay、fastForwardRead；自动翻页开关、已读记录不导出。旧备份缺字段或字段非法时保留本机阅读偏好；普通历史清理不重置速度。失败收费不伪造用户消息，不修改世界、记忆或场景。新增内容不接入统计、反馈或日志。

## 体积、验证及后续门槛

前端末次测试汇总及源码哈希见 [前端验证记录](evidence/snow-adv-features-2-frontend-validation.json)；原始 JUnit 留在本工作树 `.tmp/features-2-validation/`，不含线上用户数据。既有前端完整回归另有 **261 项及 180 个子测试全部通过**（1171.06 秒），覆盖原聊天、呈现计时、订阅、雪境三色调、基地地图、头像、第一批界面、赴约与通讯器节奏。

首屏证据与可重复命令见 [14 文件证据](evidence/snow-adv-features-2-logic-size.json) 和 `python scripts/measure_snow_adv_first_screen.py --ref HEAD --check`。口径保持相对 a1354cc、LF、14 文件分别 gzip -9：**增加 59,276B，剩余 2,164B**，上限 61,440B。第二批 app.js 挂钩相对 `95005fe` 增加 **1,802B**，在小B批准的 3,966B 余量内；其余新逻辑均按需加载。CI 对正式提交逐文件核对哈希与体积。

后端隔离 PostgreSQL 17.11 的 52 项通过，包含每日 3 次/每约定 1 次的 8 路竞争、实际 `95005fe` 旧→新迁移→旧代码写入和清理、原请求恢复、费用确认和关闭墓碑；最终上下文兼容及未知用量修正另复测 6 项通过，实例已停止。服务与入口回归及逐文件证据见 [后端验证记录](evidence/snow-proactive-backend-validation.json)，其中历史整套与末次定向复测分别保留，不重复相加。所有后端及浏览器测试均无真实付费模型调用。

当前字节的新增浏览器层 **89 项通过**：功能逻辑 32、阅读/提醒 30、真实宿主 16、真实指纹集成 3、主动消息客户端 8。覆盖 UUID 精确绑定、短首屏首绘、分页不阻塞下一请求、落盘失败不通知、A→B→A 模型会话重确认和备份往返。静态资源、发布清单、公告与主动消息服务回归 **55 项及 44 个子测试通过**；该静态命令另跳过 2 项未开启 E2E 的浏览器测试及 1 项本机缺少原始角色资料的生日源数据核对。前者随后单独开启 Chrome E2E，**2 项及 2 个子测试通过**，验证完整指纹产物 old/new/old 的 IndexedDB v4、草稿、未完成 UUID 和隐私兼容。TypeScript、Node 23 项、构建、图标检查、迁移离线 SQL 均通过。远端结果以交付 PR 同一 HEAD 的 required-checks 为准，自动 QA 不代表 Kiro UI 批准。

首轮远端 API/full-validation 同有 4 项旧协议测试失败：synthetic service 签名的世界包只有 revision，缺少主体绑定。已将夹具补为完整 public-state-2，另加其他主体的 chat/scene 结果仍被拒绝的两条断言；**生产校验零修改**。同 CI API 范围本地复测 **333 项及 447 个子测试通过**，3 项按环境条件跳过；协议与主动消息定向复测 **52 项及 12 个子测试通过**。初始失败及修正后证据分开保留，最终 CI 以 PR 最新签名 HEAD 为准。

公告已由 Kiro 改为两批合并的 `2026-10-21-snow-memory-reading`（候选验收跨到 10 月 21 日，ID 与 `published_at` 已改为 2026-10-21 01:40，隐私生效日同步改为 2026-10-21），只写已交付内容。本次服务端主动消息开关保持关闭，用户只能看到「服务器暂未开放这项功能」，所以公告不提主动消息；以后用独立发布配置开放时再另写公告。`published_at` 暂填 2026-10-20 09:30（北京时间），若实际上线不在当天，须把 ID 日期和 `published_at` 改为真实上线时间。候选阶段必须对**正式提交**运行 `check_release_announcement.py --base-sha <当时线上完整SHA> --candidate-sha <候选完整SHA>`。

接下来 Kiro 在同分支补第二批界面、测试和截图。检查三色调×1440/390、动效关闭、各文字≥4.5:1；关旗标旧界面逐像素不变。候选验收再检查真正指纹产物、手机字体/分页、主动费用确认与免费提醒。PR 合并前按主交接第 6 节停下等 Kiro UI 检查；不部署候选、不晋级。候选及专属回执仍只接受小B本人在本会话明确批准。
