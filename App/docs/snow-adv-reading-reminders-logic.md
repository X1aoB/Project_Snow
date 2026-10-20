# 第二次上线：阅读控制与约定提醒接口

`snow-feature-logic.js` 静态导入两个纯逻辑子模块；这个根模块本来就在首屏稳定后加载，子模块不进入首屏。它们不访问 DOM、不发网络请求。分页排版、设置和提醒卡片仍由 Kiro 实现。

## 阅读控制器的生命周期

交接 11.6、11.9 的 `preferences / setPreferences / readUpTo / markRead / autoDelayMs` 均由 `host.logic.reading` 提供。偏好与读取结果返回副本；偏移采用原 `contentBlocks` 序号和 UTF-16 单位，禁止切开代理对。

`host.stage.setPager(pager)` 注册 Kiro 的分页器，`setPager(null)` 注销。四个必需方法为 `paginate / render / onAdvance / onToggleAuto`，后两个可以返回取消订阅函数。分页须按 UTF-16 连续覆盖全部文字，至少一页，不切开代理对。模块不自己清空、替换或创建对白节点。

宿主使用以下控制接口：

```js
reading.enabled() // boolean：有效 pager 已注册且未 dispose
reading.active()  // boolean：有当前阅读且最后一页尚未完整显示
reading.owns(key)  // boolean：最近 200 个被控制器接受的呈现 key
reading.present({ message, text, key, animate, onFirstPaint, onComplete }) // boolean
reading.wait(key, signal) // Promise<boolean>
reading.skip()            // boolean：是否处理了跳过
reading.cancel(reason = "cancelled")
reading.reflow()          // boolean：resize / fonts.ready 后重新分页
reading.setAuto(boolean)
reading.autoEnabled()
reading.environment({ hidden, modalOpen, uiHidden, typing, paused })
```

没有 pager、正文与 canonical 文字不符、分页无效时，`present` 返回 false，宿主走旧呈现路径。`buildReadingText(contentBlocks)` 导出 canonical 文字与块内偏移映射；如宿主另传 `spans`，必须提供实际原文字切片映射，控制器会校验。动作块不纳入已读。

`enabled` 让宿主在呈现尚未开始时也能判断是否由分页路径接管。`owns` 在完成或取消后仍为 true，使已进入等待的调用能得到原结果；`dispose` 后为 false。`wait` 只在最后一页完整显示时 resolve true；取消、换消息或注销 pager 时 resolve false；AbortSignal 中断会抛 AbortError。`active` 完成后变 false，当前最后一页仍可保持显示。

新回复 `present` 会结束旧回复的手动分页，并返回旧 wait 的 false。宿主必须让后续请求继续发送，不把手动翻页作为请求门闩。`onComplete({reason})` 是呈现通知，不能用于阻塞下一轮请求。切角色、切频道、历史替换和卸载时取消旧阅读；重新注册 pager 前保留宿主的旧界面回退路径。

`onFirstPaint` 在 `pager.render` 已绘制第一页第一个非空片段后调用一次，不能用空首帧记录回复延迟。宿主沿用 `recordReplyPaint`，只缩放原 `typewriterRevealPlan` 的逐字时长：慢 1.6、标准 1、快 0.55、立即不逐字。请求、回复和网络计时不改。关闭动效仍可自动翻页。

自动计时按当前页 Unicode 字符数计算，档位系数乘 `(900 + 55 × chars)`，限制 1500–9000ms。五个环境标记中任一个为 true 都暂停自动计时，保留已停留时长；暂停不停止逐字。只有页面完整显示后开始自动停留。翻页时宿主 announcer 只收到本页正文。

Kiro 在容器 resize 或字体加载完成后调用 `reflow()`：对同一文字重新 paginate，保留当前已显示的绝对 UTF-16 偏移，并映射到新页继续显示；不重置 firstPaint、请求计时或已读。当前页的自动停留时长重新开始，扩宽后尚未绘制的尾部不会直接被标为已读。新的分页不完整、切代理对或 paginate 抛错时返回 false，旧页和原计时继续有效。普通同 key/text 的 present 只重绘，不重置阅读进度。

文字速度、停留档和快进开关保存在 localStorage 的 `project-snow-public:reading:v1`；自动开关只保存于 sessionStorage 的 `project-snow-public:reading:auto`。已读保存在 app_state `stage_read_v1:<characterId>`：每位角色最近 200 条消息，每块只记最大偏移和更新时间，不存正文。只有成功落盘后更新内存已读；历史代数、writer 所有权或实例失效时丢弃旧读取与写入结果。

导出调用 `portableFeatureState(appStates, localPreferences)`：第二参数传宿主的安全 localStorage adapter，备份只写 `ui_preferences.readingPreferences` 的 textSpeed、autoDelay、fastForwardRead。session 自动开关完全不导出或导入。正式备份导入成功后，由宿主把已验证备份中的 readingPreferences 显式传给 `reading.importPreferences(value)`；如果逻辑尚未安装，调根模块导出的 `importReadingPreferences(localPreferences, value)`，以后实例从 localStorage 读取。缺字段或非法字段保留原本机值；旧备份无该字段时不改偏好。普通 history-changed、删除和清空只刷新已读，不从 ui_preferences 应用速度，避免历史操作重置用户偏好。

## 本地提醒

交接 11.8 的 `plan / set / clear / snooze / active` 和设置 `preferences / setPreferences` 由 `host.logic.reminders` 提供。`preferences()` 默认 `{enabled:true}`。补充宿主接口：

```js
reminders.start()        // 开始调度并立即补检查
reminders.stop()
reminders.check()        // Promise<Array<本次触发的 Reminder>>
reminders.environment()  // 页面重新可见/聚焦后补检查
reminders.reconcile({ authoritative, dayKey, items })
reminders.settle({ requestId, status, rendezvous })
```

`items` 接受带 character_id 的数组或以角色 ID 为键的映射。只有宿主已经接受的、当日权威世界可以确认或结束绑定约定；尚未加载的世界不结束提醒，也不触发提醒。权威世界缺少原角色、约定 ID 或地点时结束提醒；跨香港日期记 expired。所有时间必须是今天且晚于当前时刻。

`plan` 先以 requestId 记录邀请意向，绝不按相同地点猜测绑定旧约定。宿主收到同一请求的 `character_waiting` 和该约定后调 `settle` 绑定；unknown、pending 和结果未知保留未绑定。拒绝、取消、改期等终态结束提醒。`set` 只接受当前权威世界中已有的当日约定。

调度只针对已经确认的绑定约定及香港午夜；未知世界的过期时刻不会造成零延迟循环。到点先持久化 fired，再发事件；同一实例并发检查和重启后都不会重复触发。保存失败、只读标签页、历史替换或失去 writer 所有权时不发 due。

事件 `reminders-changed` 携带 `{reminders}`，只在偏好持久化成功后发，供宿主刷新锁屏；事件 `reminder-due` 携带 `{reminder,character}`，character 从当前角色目录取副本。主动消息只监听 due，不能监听 changed 触发收费请求。提醒本身不调用模型，也不自动赴约。

提醒最多 50 条；满额时只可替换最旧的 ended 记录，不能丢弃 active 提醒。关闭提醒仍会处理跨天过期；重新开启后可补发今天仍有效的到期提醒。异常停止该实例定时器，避免持久化失败热循环。

## 用量账本、备份与清理

主动消息失败和结果未知也可能收费。可选 `adapter.usageReceipts()` 读取本地 `proactive_usage_v1:*` 的安全回执；`usage-saved` 事件实时增补。`usage.today()` 合并这些回执、导入的 `ui_preferences.proactiveUsage` 和已完成的聊天消息，按角色与 requestId 去重，保留已知最大调用数/token 数；晚于午夜完成的消息仍归原请求的香港日期。`usage.round` 的主动消息 part.kind 为 `proactive`，其余原有 kind 不改。未知 token 仍为 null，不估算金额。

`normalizeProactiveUsage` 只保留 requestId、characterId、createdAt、固定 source、model、modelCalled、status，以及六个计数字段的 usage；丢弃 envelope、正文、凭据和 proof。`portableFeatureState(appStates, localPreferences?)` 沿用 drafts/preferences/ui_preferences 白名单，把安全账本并入 ui_preferences.proactiveUsage，并在提供本地偏好 adapter 时带上上述三个阅读设置；不导出主动请求包、同意凭据、session 自动开关或已读记录。

`mergeFeaturePreferences` 继续并集合并收藏和删除标记；提醒只导入香港当天并与当前记录并集，同 ID 当前优先；安全用量按 requestId 合并并保留已知值。未知偏好字段沿用原兼容行为。IndexedDB 保持 v4。

`logic.ready` 现在等待原用量缓存及两个子模块；`logic.dispose` 清子模块、定时器和订阅。`logic.messages.get` 在 getMessage 等待后核查历史代数和 dispose 状态，历史替换后不会把旧正文交给界面。

## 本次验证

真实 Chrome、仅合成记录与本机 GET、可控时钟，无付费调用：`tests.test_snow_feature_logic` 32 项与 `tests.test_snow_reading_reminder_logic` 30 项，共 62 项通过。覆盖 canonical/emoji 偏移、200 条保留、旧异步读写失效、首绘、速度倍率、手动/自动翻页、全部暂停条件、字体/尺寸重排、消息取代、提醒精确绑定、持久化后通知、失败/只读/重启、午夜、节流补触发、50 条上限、阅读设置备份往返、备份安全及收费失败去重。界面排版、色调、真实截图与 Kiro 的 UI 检查由后续界面阶段完成。
