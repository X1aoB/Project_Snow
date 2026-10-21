# 雪境 ADV 实现注记

为控制首屏体积，`App/public_frontend/snow-adv.css` 与 `App/public_frontend/modules/snow-world.js` 里较长的设计说明移到了这里，代码中只保留一句提要。按文件和出现顺序排列；每条的标题是注释后紧跟的那行代码，引用块是代码里保留的提要。

## 基地地图接入

地图模块只接收角色清单、场景包、当前角色和忙碌状态的快照，以及既有前往流程与图标回调；不接收整个应用状态，避免把模型凭证带入新模块。打开地图不调用 `selectCharacter()` 或场景解析；只有用户点击「去见她」才进入原有选择与确认流程。面对面切换角色本来可能已完成到场，因此接入层先核对当前频道与同场状态，不再次到场；错误被既有流程捕获时，以实际确认窗口或同场状态判断结果。

独立样式先于模块写入资源清单的依赖；模块先于 `app.js`，使生产构建可将动态模块和 CSS 的完整引用同时改写为内容指纹 URL。样式加载成功后才挂入口，地图打开、关闭、再次打开均不发新请求。首屏稳定前不导入该模块，关闭雪境时也不导入。

头像占位在绑定事件的同步步骤处理，早于首帧绘制，也早于已绑定图片的快速返回；这样缓存失败和再次渲染仍会显示首字。尚未完成的图片在该卡片内隐藏，成功后沿用原有 load 事件切换；错误沿用原有移除图片的处理。只匹配雪境问候卡，不改变其他头像。

## 第一批新功能界面

记忆档案、台词收藏、回看搜索、图片卡和本轮用量的界面都在按需加载的 `modules/snow-features.js`（入口）、`snow-features.css`、`snow-feature-icons.js` 和 `snow-feature-card.js` 里；数据只经宿主对象 `host.logic` 读写，界面不直接访问 IndexedDB。资源清单里样式、图标、图片卡先于入口登记，入口和 `snow-feature-logic.js` 先于 `app.js`，生产构建才能把这些动态引用改写为指纹地址。入口先加载样式，样式失败则不挂任何新界面。

- 挂载位置。面对面的星标和用量放在名牌一行，当前台词以 `#stage-open-feedback` 的 `data-message-id` 为准；两个按钮比名牌高几像素，用负外边距抵消，对白框给正文的行数与未加新功能时相同。通讯器的用量挂在 `.message-body > .meta` 里而不是 `.message-body` 下，因为时间线增量更新会比较 `.message-body` 的子元素个数；挂上后同一条消息原有的 `.message-usage` 小字隐藏，避免同一信息出现两次，新模块没加载时旧小字照常显示。回看条目的星标放在右下角，只给最后一段正文让出右侧位置，标题行保持全宽。
- 用量文案。未知次数写「调用次数未知」，厂商没有返回 token 数时写「厂商未返回 token 数」而不是 0；部分统计在数字前加「约」。浮层挂在 `body` 上，Esc 关闭并把焦点还给触发按钮。设置开关放在「对话」分类（第一批时在「记录」页自动整理之后），关闭后所有用量按钮隐藏，重新载入后保持。
- 记忆档案。删除先在条目内确认，删除后在弹窗关闭前可撤销；关闭时调用 `logic.memories.clearUndo`。逻辑忙时 `remove` 返回 `busy`，界面提示等这一轮回应结束后再删除，列表不变。
- 收藏与图片卡。收藏取消息里第一个台词或消息块，超过 240 字截到最后一个句末标点。图片卡用 canvas 绘制 1080×1350，预览用 `toDataURL`（CSP 的 `img-src` 不允许 `blob:`），保存用 `toBlob` 和 `a[download]`，全程在本机完成，不发请求。
- 对比度。记忆类别标签的文字用角色色与 `--ink-900` 的混合；暮色和夜晚下搜索高亮用较深的暖色底配 `--ink-900` 文字。三种色调下新界面文字对比度均不低于 4.5:1，由 `tests/test_snow_adv_features_ui.py` 检查。

## 第二批新功能界面
长台词分页在 `modules/snow-pager.js`，约定提醒和设置「对话」在 `modules/snow-conversation-ui.js`，都由 `snow-features.js` 引入并在资源清单里登记为它的依赖。逐字节奏、翻页时机、自动计时、已读和提醒调度都在 `host.logic`（`reading`、`reminders`、`proactive`），界面只负责排版、绘制和把用户操作转给逻辑。

- 分页。分页器通过 `host.stage.setPager` 注册 `paginate`、`render`、`onAdvance`、`onToggleAuto`。断点优先句末标点，其次逗号，最后才按字切；断点后的空白并进本页。第一页带动作描写，第二页起收起（`.snow-page-rest`），两种台词区高度分别测量。右下角给 ▼、页码和快捷菜单留位，按最宽的页码「10 / 10 · 已读」预留并对齐到整行，只有最后一行从它左边绕过去。对白框尺寸变化或字体加载后调用 `reading.reflow()`。手机上分页可用时 `#in-person-surface[data-snow-pager]` 让对白框加高约 60px（小B已批准）。点对白框，或在对白框上按 Enter、空格、→ 翻页。
- 「已读」。逻辑在一页显示完时就记为已读，若直接照搬 `state.read`，第一次读的每一页读完也会写「已读」。分页器记下这段台词这次出现时第一处未读的位置，只给在它之前、且已读的页加「已读」。
- 重载恢复。分页器按需加载，晚于重载后的首次舞台渲染，那次渲染走旧路径，长台词会压到快捷菜单下面。`app.js` 的 `setPager` 在注册分页器后，若当前是面对面，就重新调用 `renderStageSpeech()`，把已在舞台上的台词交给分页器（不重放动画、不重新计时）；`snow-features.js` 装好分页器后再刷新一次星标，使其收藏的是当前页。
- 约定提醒。邀请弹窗里加「到点提醒」，约定条上加提醒按钮可补设或修改，到点后在通讯器里出现提醒卡。`#text-surface` 原本是「顶栏 / 约定条 / 时间线」三行，提醒卡不占行时会被自动排到时间线下面，压在浮动提示和「反馈」按钮上；这里改为四行，提醒卡占第三行，没有提醒时这一行高度为 0。锁屏的约定通知由宿主渲染，这里只补角色色。
- 设置「对话」。分类放在「记录」之后，「显示每轮用量」从「记录」移到这里。「角色主动来消息」依赖约定提醒；打开前先弹费用确认，选「暂不开启」不发任何请求。结果未确认的收费请求只提供「查询原请求」，走恢复查询，不会重新发送。
- 测试。`tests/test_snow_adv_features_ui.py` 覆盖两种尺寸的分页（不压菜单、不出框、顺序完整、断点在标点、第二页起收起动作描写、第一次读不标「已读」）、重载后恢复的长台词按页显示并标「已读」、自动翻页与当前页收藏、提醒的绑定、到点卡位置、延后与关闭、设置与费用确认、三种色调对比度，以及关闭雪境时不加载这些模块。

## 凭证保存时长
设置「模型」页 API Key 下方的「凭证保存」。服务端 `/public/v1/config` 的 `credential_policy.retention` 为 `{"enabled": true, "notice_version": "credential-retention-1", "options": [...]}` 时才显示，只保留服务端列出的时长；否则字段隐藏，`/byok/session` 请求也不带新字段（旧版接口是严格模型，多余字段会被拒）。这项是核心功能，不依赖雪境开关。

- 存储。默认「仅当前标签页」，凭证照旧写 sessionStorage。选本机保存并签发成功、且服务端回显同一个 `retention` 时，凭证写 localStorage（同一个键 `project-snow-public:byok`，多存 `retention`），并删掉 sessionStorage 里的那份；反过来也一样，同一时间只有一份。启动时先读 sessionStorage 再读 localStorage，过期就两处都删。订阅模式的凭证始终只在当前标签页。
- 告示。改选本机保存时按需加载 `modules/credential-retention.js` 弹出告示，复用 `.modal`、`.notice-list`、`.notice-choice`，旧界面和雪境界面都能显示；勾选后才能确认，取消就退回上一次的选择。确认只对选中的时长有效（`state.retentionConsent`），签发时再校验一次，没确认就报 `credential_retention_notice_required`，不发请求。
- 改变保存方式。服务器签发需要 Key 原文，浏览器里只有加密后的凭证，所以已有凭证时改了保存方式，要重新输入 API Key 再保存；字段下方会提示。获取模型列表和保存时，保存方式与当前凭证不同就重新签发。
- 清除与作废。服务端 `POST /public/v1/byok/revoke` 不带参数，作废这个匿名身份在此刻之前签发的全部本机保存凭证（v2）；仅当前标签页的凭证（v1）没有签发时间，不受影响。所以：
  - 「清除模型会话」两处都删；本机保存的凭证还会调用撤销。
  - 换掉一张本机凭证（改保存方式、换厂商、到期重签）时，先在本地删掉旧凭证并调用撤销，再签发新的；顺序反过来会连新凭证一起作废。
  - 网络错误、5xx、429 时在 localStorage 记待作废标记 `project-snow-public:byok-revoke`（所有标签页共用），下次打开页面或下次签发前重试；其他 4xx（例如回滚后的旧服务端没有这个接口）直接放弃。标记还在时不签发新的本机凭证，提示先稍后再试或改用「仅当前标签页」；仅当前标签页的凭证不受之后重试影响，可以照常签发。
  - 提示如实区分「已作废」「下次重试」「到期失效」。其他标签页清除本机凭证时，通过 `storage` 事件同步停用。
- 主动消息。运行时 adapter 传 `retention`（当前凭证的保存方式）和 `storageForRetention`（`tab` 用 sessionStorage，其余用 localStorage），主动消息客户端把费用确认存进同一种存储，换保存方式或清除凭证时一起失效。
- 隐私页「API Key 与模型供应商」和设置页开头的说明同步改为「默认只保存在当前标签页」。
- 测试。`tests/test_credential_retention_ui.py`。

## 面对面请求状态
P8 起舞台延伸到输入栏下面：`app.js` 用 ResizeObserver 把 `#composer` 的高度写进 `--snow-composer-h`，舞台网格最后一行和立绘的 `bottom`/高度都按它算。请求状态行 `.request-status-row` 和字数提示 `.composer-foot` 原来都在 `#composer` 的文档流里，一出现输入栏就变高：对白框被顶上去，和输入栏之间多出一条状态行，原本连成一体的对白框和输入栏被拆开；立绘同时变矮（实测「停止等待」时对白框上移 61px、立绘矮 60px，「已完成」一直留着时矮 32px，输入第一个字时矮 19px）。

- 状态行。只在雪境面对面时改为绝对定位：仍在 `#composer` 的 DOM 里（焦点顺序、`aria-live` 和按钮绑定都不变），显示在对白框上沿右侧，宽度不超过对白框。定位用 `--snow-dialogue-h`：同一个 ResizeObserver 也观察 `#stage-dialogue`。`#toast` 在 `body` 下拿不到 `#chat-app` 上的变量，所以对白框可见时脚本直接在它身上写 `--snow-toast-b`（输入栏 + 对白框 + 8px，状态标签显示时再加上标签高度，两者不重叠；ResizeObserver 也观察状态行），面对面时 toast 放在对白框上方，不再盖住对白框里的快捷菜单；对白框不可见时清掉，回到原来的位置。不要改用 `html:has(#chat-app[data-channel="in_person"])` 这类根元素 `:has()`：它会让 `test_daylight_fix_keeps_other_tones_pixel_identical` 的前后截图不一致。
- 隐藏的内容。面对面时「立即显示」与快捷菜单的「跳过」是同一个操作（都调用 `finishReceivedPresentation`），隐藏；回复完成后的单纯「已完成」由 `renderRequestStatus` 标 `data-done`，面对面时隐藏，完成由 ▼ 表示；状态行没有可见内容时整行隐藏。字数提示在面对面时隐藏（手机原本就不显示快捷键提示），动作与对白合计超过 2,000 字时发送前仍有横幅提示。通讯器里的状态行、「立即显示」和字数提示都不变。
- 立绘。立绘按 `min(--snow-composer-h, 80px)` 计算，输入栏静止时（约 75px）与原来完全一样；多行输入撑高输入栏时，对白框跟着上移，立绘最多变动 5px。
- 按钮。面对面失败时的「重试」（`.request-retry`）原来没有任何样式，显示为浏览器默认灰按钮，改为和「重新读取场景」相同；「停止等待」文字改为暖色与墨色混合，三种色调对比度不低于 4.5:1。
- 测试。`tests/test_snow_adv_features_ui.py::test_request_status_and_typing_never_resize_the_stage`：雪白、暮色桌面和深色手机下，输入、多行输入、等待回复、完成、失败重试各状态对白框、立绘、输入栏的位置不变，标签在对白框上方且不超出，toast 不压对白框，文字对比度不低于 4.5:1。

## 宣传片录制中发现的界面问题（2026-10-22）
- 锁屏时钟。`buildSnowLock()` 原来只在建锁屏时写一次时间。现在 `snowTickLock()` 每到整分刷新时间、日期和时段标签，下一次按 `60000 - now % 60000` 续订；锁屏不在了就不再续订，解锁时 `clearTimeout`。
- 锁屏约定提醒卡。`snowLockNotes()` 给提醒卡带上 `remindAt`，卡片右侧显示约好的提醒时间（香港时间 `HH:MM`，`hourCycle: "h23"`）；未读公告和待赴约定卡仍显示锁屏渲染时的时间。功能模块晚于锁屏装好（首屏稳定后才加载），已经到点的提醒不会再发事件，所以 `logic.ready` 后补刷新一次锁屏卡。
- 订阅模型名。`subscriptionModelLabel(id, effort)` 统一写成「显示名 · 推理强度中文标签」：设置卡说明行、推理强度选项、默认强度提示和保存后的提示都用它，不直接显示原始 id 和强度值。显示名取连接器返回的列表；还没连接时默认模型写「GPT-5.6-Luna」，`index.html` 的初始文字和教程第 4 步同步。两个模型显示名相同时，模型下拉仍附 id 区分。
- 订阅说明在深色下看不清。`app.css` 把说明、模型卡、配对框、人机验证框和状态条的底色写死为浅色，暮色和夜色把正文换成浅色字后对比度只有 1.1–1.9:1。这些表面在深色下改用 `--tn-surface`，和设置开关等小表面一致；出错状态并入危险色规则；代码块沿用 `.modal code`，不变。
- 手机雪白下圆形按钮发灰。录制截图是在夜→昼切换的同一刻拍的：`button` 和快捷菜单按钮的背景色有 100–140ms 过渡，截到的是深色到白色的中间色。稳定后按钮底接近白色，与桌面一致，没有改样式；`test_phone_stage_round_buttons_keep_icon_contrast_in_every_tone` 用实际截图像素锁住三种色调下图标与按钮底对比度不低于 3:1、雪白下按钮底接近白色。录制切换色调后至少等 300ms 再截图。
- 测试。`tests/test_snow_adv_ui.py` 的 `test_phone_lock_clock_follows_the_minute_and_stops_after_unlock`、`test_phone_lock_reminder_card_shows_the_agreed_time`、`test_phone_stage_round_buttons_keep_icon_contrast_in_every_tone`；`tests/test_public_subscription_frontend_e2e.py` 的 `test_model_names_read_as_display_name_and_effort_label`、`test_subscription_panel_text_stays_readable_in_every_snow_tone`。

## snow-adv.css

### `html[data-ui="snow-adv"] {`

> 雪境 ADV design system (P1).

雪境 ADV design system (P1).

Every rule is scoped to html[data-ui="snow-adv"], which app.js sets only when
the server reports features.ui_snow_adv === true.  With the flag off this
file matches nothing, so the existing interface renders exactly as before.
Fonts live in /snow-adv-fonts.css and are linked lazily by app.js.
Motion is unchanged in P1; animation work belongs to later phases.

### `--char: var(--ice-500);`

> Current character colour

Current character colour; [data-snow-character] below overrides it.

### `--fs-12: 0.75rem;`

> Type scale

Type scale; nothing in the interface goes below 12px.

### `--ease-snow: cubic-bezier(0.22, 0.61, 0.36, 1);`

> Easing tokens for later phases

Easing tokens for later phases (P1 does not change motion).

### `--snow-bg: var(--snow-50);`

> Re-map the shared design variables

Re-map the shared design variables so every var()-based rule follows.

### `html[data-ui="snow-adv"] [data-snow-character] {`

> 22 character theme colours

22 character theme colours (draft palette from the P0 mockups).
   --char-deep/--char-soft must be re-derived where --char changes: a custom
   property that references another is resolved on the element declaring it.

### `html[data-ui="snow-adv"] :is(`

> Minimum 12px

Minimum 12px: every rule that app.css sets below .75rem.

### `html[data-ui="snow-adv"] :is(.stage-speech, .stage-narration, .stage-speaker-line strong, `

> Story typography

Story typography: dialogue, nameplate and scene titles use LXGW WenKai.

### `html[data-ui="snow-adv"] .brand-mark {`

> Brand

Brand: keep the original 小吉终端 icon, drop the legacy gradient tile.

### `html[data-ui="snow-adv"] .stage-speaker-line strong {`

> Character colour

Character colour, applied where a nameplate already exists.

### `html[data-ui="snow-adv"] #scene-backdrop { display: none; }`

> P2 程序化分层雪景

---------------------------------------------------------------- P2 程序化分层雪景
   仅在开关开启时：隐藏旧的 SVG 背景与 CSS 雪，改用 #scene-stage 内的世界层。
   每条选择器都限定在 html[data-ui="snow-adv"] 下（测试强制）。

### `html[data-ui="snow-adv"] .scene-stage .snow-layer {`

> 世界层：far(0) → snow-far(1) → room(2)

世界层：far(0) → snow-far(1) → room(2) → [立绘 z-index:3] → snow-near(4)

### `app.js 进入面对面时默认加 sidebar-collapsed（抽屉关闭 = 舞台全宽），`

> P3 面对面主屏

================================================================ P3 面对面主屏
   仅在 html[data-ui="snow-adv"] 下、面对面界面：把面对面界面升级为 galgame 主屏。
   每条选择器都限定在 html[data-ui="snow-adv"] 下（测试强制）。开关关闭时本段全部
   匹配不到；▼ 推进标记与快捷菜单由 app.js 仅在开关开启时建到对白框里，关闭时移除，
   因此关闭态不新增任何可见元素。

### `@media (min-width: 821px) {`

> 1) 全屏舞台

1) 全屏舞台：面对面时把永久 304px 的角色栏改成覆盖式抽屉，舞台占满视口宽度。
   app.js 进入面对面时默认加 sidebar-collapsed（抽屉关闭 = 舞台全宽），
   #open-stage-contacts 仍用既有 toggleContacts 开合它，不改旧 UI 的侧栏偏好语义。

### `html[data-ui="snow-adv"] .stage-speaker-line strong {`

> 名牌：角色色，左上，背景 var(--char-deep)

名牌：角色色，左上，背景 var(--char-deep)（角色色由 P1 的 [data-snow-character] 提供）。

### `html[data-ui="snow-adv"] .stage-advance {`

> 3) ▼ 推进标记

3) ▼ 推进标记：对白框右下，回复显示完且空闲时出现；打字/揭示/等待时隐藏。
   仅在动态档位 auto 轻轻上下浮动；light/off 静止。aria-hidden（由 JS 设置）。

### `html[data-ui="snow-adv"] .stage-character-art[data-actor-anchor] {`

> 6) 立绘按身体中轴居中

6) 立绘按身体中轴居中：--actor-anchor-x 为中轴在图里的横向位置（0~1），
   由 app.js 从当前 #stage-character-art 测出并缓存；按它把立绘对齐到舞台中线，
   而不是按图片外框。换表情沿用缓存值，不会左右跳动。底边仍藏在对白框后。
   旧定位用 left: var(--stage-character-focus-x) + translate -50%，这里改为按中轴平移。

### `@media (max-width: 860px) {`

> 7) 移动端

7) 移动端（≤ 860px）：同一对白框；快捷菜单收成仅图标（保留无障碍名）；不与输入重叠；不低于 12px。

### `@keyframes snow-advance-bob { 0%, 100% { transform: translateY(0); } 50% { transform: tran`

> ▼ bob, referenced only by the

▼ bob, referenced only by the scoped .stage-advance rule above (motion level auto).

### `不与 P3 的 translate 中轴定位冲突（它用 translate，这里用 transform）。`

> P4 演出与转场

================================================================ P4 演出与转场
   仅在 html[data-ui="snow-adv"] 下、面对面界面：让立绘有呼吸、入场与带缓冲的动作，
   并把场景/模式转场遮罩重做成样稿的白雪转场。全部只动 transform / opacity，
   档位（auto / light / off）由 app.js 维护的 [data-motion-level] 驱动，页面隐藏
   由 [data-page-hidden] 驱动。每条选择器都限定在 html[data-ui="snow-adv"] 下（测试强制）。
   换表情时外层旧图由 app.js 保持 opacity 1，这里不做交叉淡出，避免半透明空档。

### `html[data-ui="snow-adv"][data-motion-level="auto"]:not([data-page-hidden="true"]) .stage-c`

> 1) 呼吸

1) 呼吸：极轻微的空闲缩放（1 → 1.008），绕身体中轴底部（--actor-anchor-x）进行，
   不与 P3 的 translate 中轴定位冲突（它用 translate，这里用 transform）。
   只在 auto 档位；页面隐藏时暂停；减少动态关闭。

### `html[data-ui="snow-adv"] .scene-transition-layer,`

> 2) 白雪转场遮罩

2) 白雪转场遮罩：把既有的「脚印加载卡」转场层重做成磨砂白雪淡入 + 居中标题卡。
   复用同一批 DOM 元素、data-phase、aria-live 文案与时序钩子，仅改表现。
   既有脚印/圆点装饰在开关开启时隐藏。标题（地点名）用霞鹜文楷故事字体。

### `html[data-ui="snow-adv"] .snow-veil-kicker {`

> 3) P4 新增的仅装饰节点

3) P4 新增的仅装饰节点（kicker 小字），由 app.js 仅在开关开启时建、aria-hidden。

### `@keyframes snow-actor-breath { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.`

> 立绘呼吸关键帧

立绘呼吸关键帧：只被上面 auto 档位的 .stage-character-art 规则引用。

### `html[data-ui="snow-adv"] .snow-scene-veil {`

> P4 后续

---- P4 后续：独立白雪转场覆盖层（生产 motion_v2/scene_actions_v2 开启时呈现地点转场） ----
   由 app.js 仅在开关开启时建、aria-hidden、pointer-events:none；opacity 由 JS 动画驱动，
   idle 态为 0（不可见、不挡交互）。地点仍由既有 aria-live 播报一次，本层不参与无障碍。

### `html[data-ui="snow-adv"][data-motion-level="off"] .snow-scene-veil,`

> off / 减少动态

off / 减少动态：遮罩完全不出现（JS 已不触发；这里兜底确保无任何过渡/动画时长）。

### `html[data-ui="snow-adv"] .chat-panel { background: linear-gradient(180deg, rgba(255, 255, `

> P5 通讯器与其余界面

================================================================ P5 通讯器与其余界面
   仅在 html[data-ui="snow-adv"] 下：把面对面舞台以外的所有界面重做成雪境皮肤——文字
   通讯（联系人卡片、磨砂设备感、角色色气泡、空会话问候卡）、顶栏、输入区、各弹窗与
   抽屉、新手引导、提示与系统横幅，以及隐私页。沿用 P1 令牌与色板，仅改表现：动画只动
   transform / opacity，档位由 [data-motion-level] 驱动。每条选择器都限定在
   html[data-ui="snow-adv"] 下（测试强制）。开关关闭时本段全部匹配不到，旧界面逐字节不变。
   per-row 的角色色由 renderCharacters() 写的 data-snow-character 提供（见 P1 的
   [data-snow-character] 规则派生 --char/--char-deep/--char-soft）。

### `html[data-ui="snow-adv"] .chat-panel { background: linear-gradient(180deg, rgba(255, 255, `

> 1) 文字通讯：联系人卡片

---- 1) 文字通讯：联系人卡片 ----------------------------------------------

### `html[data-ui="snow-adv"] .message-avatar .portrait { border-radius: 50%; }`

> 2) 文字通讯：消息气泡

---- 2) 文字通讯：消息气泡 ------------------------------------------------

### `html[data-ui="snow-adv"][data-motion-level="auto"] .message.is-snow-pop {`

> 气泡 pop-in

气泡 pop-in：auto 轻微上浮 + 淡入（~220ms），light 仅淡入，off 无。
   作用在新消息行上（JS 在 .message 节点加 is-snow-pop，见 app.js）。

### `html[data-ui="snow-adv"] .empty-conversation { color: var(--ink-600); }`

> 3) 文字通讯：空会话问候卡

---- 3) 文字通讯：空会话问候卡 -------------------------------------------

### `html[data-ui="snow-adv"] .empty-conversation { color: var(--ink-600); }`

> 友好的角色色问候卡，取代纯灰文字

友好的角色色问候卡，取代纯灰文字（文案不变，由 app.js 包进 .snow-empty-greeting）。

### `html[data-ui="snow-adv"] .active-character h1 { color: var(--ink-900); }`

> 4) 顶栏：文字通讯头部

---- 4) 顶栏：文字通讯头部 ------------------------------------------------

### `html[data-ui="snow-adv"] .scene-hud {`

> 4b) 顶栏：面对面 HUD

---- 4b) 顶栏：面对面 HUD -------------------------------------------------

### `html[data-ui="snow-adv"] .composer-row textarea {`

> 5) 输入区（文字通讯）

---- 5) 输入区（文字通讯）-------------------------------------------------

### `html[data-ui="snow-adv"] #message-input::placeholder {`

> 修复移动端 placeholder 换行被截断

修复移动端 placeholder 换行被截断：文字通讯输入框 placeholder 单行省略。
   不改文案、不改行为；仅限 ::placeholder 展示。

### `html[data-ui="snow-adv"] .modal h2 { color: var(--ink-900); }`

> 6) 弹窗、抽屉、引导、提示、系统横幅

---- 6) 弹窗、抽屉、引导、提示、系统横幅 ----------------------------------

### `html[data-ui="snow-adv"] .privacy-page {`

> 7) 隐私页（flag 开启时才皮肤化）

---- 7) 隐私页（flag 开启时才皮肤化）-------------------------------------

### `html[data-ui="snow-adv"] .privacy-page {`

> 隐私页也挂 html[data-ui="snow-adv"]

隐私页也挂 html[data-ui="snow-adv"]（由 privacy.js 读 /config 设置），复用 P1 令牌。

### `html[data-ui="snow-adv"] .snow-adv-attribution { color: var(--ink-600); font-size: var(--f`

> 字体与图标署名行

字体与图标署名行：仅在 flag 开启时显示（privacy.js 开启时移除 hidden）。

### `html[data-ui="snow-adv"] :is(`

> 8) ≥12px 兜底：P5 新增的可见小字节点

---- 8) ≥12px 兜底：P5 新增的可见小字节点 --------------------------------

### `@keyframes snow-bubble-pop { from { opacity: 0; transform: translateY(6px); } to { opacity`

> 关键帧（只被上面开关开启的规则引用）

---- 关键帧（只被上面开关开启的规则引用）---------------------------------

### `@media (max-width: 560px) {`

> 9) 移动端（≤560px）：修复 HUD 名字截断与输入占位

---- 9) 移动端（≤560px）：修复 HUD 名字截断与输入占位 ----------------------

### `html[data-ui="snow-adv"] .scene-character-button { max-width: none; min-width: 0; }`

> HUD 角色名原本被 .scene-character-button

HUD 角色名原本被 .scene-character-button max-width:150px + strong 省略号挤成
     「凯…」。开关开启时让按钮用可用宽度、名字正常显示（真正放不下才省略）。

### `html[data-ui="snow-adv"] #message-input { min-height: 44px; }`

> 面对面输入占位同样单行省略，避免「说些什么

面对面输入占位同样单行省略，避免「说些什么，也可只填写动作……」换行被裁切。
     placeholder 规则在上方已对 #message-input 生效，这里只保证单行高度不被撑高。

### `html[data-ui="snow-adv"] .snow-backdrop {`

> P6a 开场与通讯器世界层

============================================================= P6a 开场与通讯器世界层
开机层（桌面「终端开机」）、锁屏层（手机「终端锁屏」）与通讯器背后的世界层。
节点全部由 app.js 仅在开关开启时建出，关闭态不存在；每条规则仍限定在
html[data-ui="snow-adv"] 下。只用 transform/opacity 动画；减少动态与 off 档无动画。
=============================================================

### `html[data-ui="snow-adv"] .snow-backdrop {`

> 通讯器背后的世界层

通讯器背后的世界层：桌面开关开启时，app.js 在 body 级建 #snow-backdrop（内含
#snow-backdrop-stage，世界层挂在其中），fixed 铺满视口、aria-hidden、不拦截指针、
z-index 低于 #chat-app，作为文字通讯界面背后的雪境背景。

### `html[data-ui="snow-adv"] .snow-backdrop-stage > .snow-layer { position: absolute; inset: 0`

> 背后世界的分层/画布铺满容器

背后世界的分层/画布铺满容器（舞台世界的 P2 定位是相对 #scene-stage，这里补足绝对定位）。

### `html[data-ui="snow-adv"] #chat-app[data-snow-world] { position: relative; z-index: 1; back`

> 桌面开关开启时，#chat-app 浮在背后世界之上

桌面开关开启时，#chat-app 浮在背后世界之上；文字通讯的各面做成磨砂设备感，
让雪景清晰透过来而文字对比度仍 ≥ 4.5:1（ink 文本色不变、底色足够但不盖死雪景）。

### `html[data-ui="snow-adv"] #chat-app[data-snow-world] #text-surface,`

> 文字通讯主面与消息区更通透，让背后雪世界清晰可见

文字通讯主面与消息区更通透，让背后雪世界清晰可见；助手/用户气泡自身仍有不透明底色保证可读。

### `background: color-mix(in oklab, var(--snow-0) 62%, transparent);`

> 62% snow + blur keeps the world

62% snow + blur keeps the world visible as a frosted pane; timestamps and
     sender lines sit directly on it, so they need >= 4.5:1 even over night scenes.

### `html[data-ui="snow-adv"] .app-shell { --snow-hud-clear: 84px; }`

> 通讯器背后世界已改为独立的 body 级 #snow-backdrop

通讯器背后世界已改为独立的 body 级 #snow-backdrop 容器（见上），无需把 #scene-stage
提为视口背景；#in-person-surface 在文字模式保持原生 hidden 行为。

### `html[data-ui="snow-adv"] .app-shell { --snow-hud-clear: 84px; }`

> P7 上线后修整

============================================================= P7 上线后修整
依据线上实拍的修整：开机退场与入场、通讯器背后世界、面对面舞台与 HUD、输入工具、
须知与公告弹窗、手机锁屏。仍只在 html[data-ui="snow-adv"] 下生效；只动 transform/opacity。
=============================================================

### `html[data-ui="snow-adv"] .app-shell { --snow-hud-clear: 84px; }`

> HUD 下方留给立绘与提示的净空

HUD 下方留给立绘与提示的净空（舞台延伸到 HUD 之下后，立绘仍按 HUD 以下的高度定尺寸）。

### `html[data-ui="snow-adv"][data-motion-level="auto"] #chat-app[data-snow-entering] .contact-`

> 入场只在完整动效档

入场只在完整动效档：侧栏从左滑入，顶栏、消息区、输入区依次上浮。开机层淡开时它们正好露出。

### `html[data-ui="snow-adv"] #chat-app[data-snow-world] #text-surface {`

> 消息区不再整体磨砂

消息区不再整体磨砂（原来 62% 白 + 模糊把雪景洗成灰影）：上下轻薄白雾，中间透出雪景。

### `html[data-ui="snow-adv"] .stage-dialogue-copy:has(> .stage-narration) { grid-template-rows`

> B1 旁白进对白框

---- B1 旁白进对白框（app.js 在开关开启时把 #stage-narration 挂进对白框，关闭时放回舞台） ----

### `html[data-ui="snow-adv"] .in-person-surface:not(.ui-hidden) .scene-stage { grid-row: 1 / 4`

> B2/B3 面对面 HUD：舞台延伸到顶栏之下，顶栏只剩浮在雪景上的胶囊

---- B2/B3 面对面 HUD：舞台延伸到顶栏之下，顶栏只剩浮在雪景上的胶囊 ----

### `html[data-ui="snow-adv"] .in-person-surface .scene-hud { grid-row: 1; grid-column: 1; alig`

> 舞台占了第 1 行后，HUD 必须显式放回第 1 行

舞台占了第 1 行后，HUD 必须显式放回第 1 行（否则自动排到舞台之后，掉到对白框下面）。

### `html[data-ui="snow-adv"] .in-person-surface:has(#stage-quick-menu) :is(#open-transcript, #`

> 快捷菜单已有「回看」「隐藏」，HUD 不再重复放同样的两个按钮

快捷菜单已有「回看」「隐藏」，HUD 不再重复放同样的两个按钮（H 键和菜单项照常可用）。

### `html[data-ui="snow-adv"] .app-shell[data-channel="in_person"] #system-banner {`

> B4 面对面里的系统提示：HUD 下方的小提示条，不再整宽压住舞台；转场时让开

---- B4 面对面里的系统提示：HUD 下方的小提示条，不再整宽压住舞台；转场时让开 ----

### `right: 0;`

> 用 margin 居中，不用 translate

用 margin 居中，不用 translate：既有的 snow-panel-enter 入场动画会接管 translate/opacity。

### `html[data-ui="snow-adv"] .composer-tools .composer-tool-button {`

> C1 输入工具：表情包 / 动作 / 一起去 统一成同一种浅色胶囊，只有发送是主按钮

---- C1 输入工具：表情包 / 动作 / 一起去 统一成同一种浅色胶囊，只有发送是主按钮 ----

### `html[data-ui="snow-adv"] .snow-lock {`

> D1 手机锁屏：和界面一样保持雪白（只有舞台背景跟随时间），远处是柔和的雪丘

---- D1 手机锁屏：和界面一样保持雪白（只有舞台背景跟随时间），远处是柔和的雪丘 ----

### `html[data-ui="snow-adv"] .app-shell[data-channel="in_person"] .chat-panel > .in-person-sur`

> P8

============================================================= P8
   面对面舞台延伸到输入区下方：对白框外不再露出面板底色。输入区仍在原网格行（键盘、
   焦点顺序不变），只是叠在舞台上；立绘按输入区高度上移，大小和位置与之前一致。
   --snow-composer-h 由 app.js 用 ResizeObserver 写在 #chat-app 上，隐藏界面时为 0。

### `html[data-ui="snow-adv"][data-tone="dark"] {`

> P8 深色状态

---- P8 深色状态（夜间）：html[data-tone="dark"] 由 app.js 按舞台时段设置（夜晚=深色），
   与雪景共用同一时段时钟。做法：先翻转设计变量（大部分界面随之变深），再覆盖少数写死浅色的
   表面。角色色名牌、主按钮的冰蓝底和白字保持不变；舞台背景本身仍由时段雪景决定。

### `html[data-ui="snow-adv"][data-tone="dark"][data-tone-time="dusk"] {`

> 黄昏（跟随时段时）

黄昏（跟随时段时）：暮色。与夜色共用全部深色规则，只把分级变量换成与晚霞相配的偏紫暖色，
   比夜色浅一档；主按钮仍是冰蓝。固定「深色」时始终用夜色。

### `裁切到这个圆并放大到占满，开机、锁屏不再出现方形白角。 */`

> P8 尘白元素（轻量）：基地区域编号、对白框切角与角色色细线；图标圆形裁切

---- P8 尘白元素（轻量）：基地区域编号、对白框切角与角色色细线；图标圆形裁切 ----

### `html[data-ui="snow-adv"] :is(.snow-boot-mark img, .snow-lock-bar img) {`

> 小吉终端图标原图是带白边的方形底，蓝色圆形主体中心

小吉终端图标原图是带白边的方形底，蓝色圆形主体中心 (96, 93.5)、半径 88（192px 图）。
   裁切到这个圆并放大到占满，开机、锁屏不再出现方形白角。

### `html[data-ui="snow-adv"] .stage-dialogue {`

> 对白框：右上切角

对白框：右上切角（clip-path，只动形状不影响点击区域），顶边一条角色色细线，切角处补一条斜边。
   clip-path 会裁掉外阴影，所以把阴影换成更清楚的描边与内发光。

## modules/snow-world.js

### `const SVG_NS = "http://www.w3.org/2000/svg";`

> 雪境 ADV 世界层

雪境 ADV 世界层（P2）：程序化分层雪景 + Canvas 远近雪。
所有图形在浏览器内由 DOM / SVG 字符串生成，不请求任何图片，没有内联样式元素、
没有外链 URL、不动态执行代码，满足生产 CSP。仅在 html[data-ui="snow-adv"] 下由 app.js 挂载。
从样稿 .tmp/snow-adv-mockups/snowfx.js 移植；去掉了样稿控制台、localStorage、
白雪转场、相机推进、立绘中轴等后续阶段拥有的内容。

### `let activeSnowLoopCount = 0;`

> 活动中的 SnowField rAF 循环计数

活动中的 SnowField rAF 循环计数（供测试断言「同一时刻只有一个世界在动画」）。
每个世界实例的 loop 开始/停止时增减。读取见下方导出的 activeSnowLoops()。

### `const PALETTES = {`

> 配色

---------------------------------------------------------------- 配色

### `export function resolveTime(now = new Date()) {`

> 只有舞台背景跟随本机时间

只有舞台背景跟随本机时间；换表情/换角色不改时间。可用 html[data-snow-time] 覆盖（仅测试与截图用）。

### `const snowTimeSubscribers = new Set();`

> P8：共享时段时钟

P8：共享时段时钟。舞台、通讯器背后的世界与界面深色状态都订阅它，时段跨界时在同一拍
一起切换（之前世界层最多每 10 分钟、且只在重绘时才重算时段）。只有订阅者时才计时。

### `function rng(seed) {`

> 工具

---------------------------------------------------------------- 工具

### `function worldTree(p, id, H) {`

> 远景（室外，透过窗看到或 generic 全屏）

---------------------------------------------------------------- 远景（室外，透过窗看到或 generic 全屏）
P8 尘白元素（轻量）：远方禁区中心的晶体「世界树」剪影，和环绕禁区的收容墙与信标。
都画在远景层、低对比，被远山和中景山脊遮住下半部，读作很远的地平线；夜里晶体微微发青光。

### `function roomDefs(p, id) {`

> 室内（墙带窗洞，雪只透过窗显示）

---------------------------------------------------------------- 室内（墙带窗洞，雪只透过窗显示）

### `const ROOM_MARKS = {`

> P8 尘白元素

P8 尘白元素：室内墙面的切角面板线与基地涂装编号（如「B-04 休息区」）。只画在墙面上
（clip 到带窗洞的墙），地板随后覆盖下半部；颜色取墙面的深一档、低不透明度。
位置避开顶部 HUD（约 y<110）、中间站位的角色（x≈560–1040）和底部对白框；
窗间窄墙用竖排（第 6 项为 "v"）。观景区整面是窗，只在地点胶囊上显示编号。

### `let sharedSprite = null;`

> Canvas 雪

---------------------------------------------------------------- Canvas 雪

### `g: Math.random() < 0.12,`

> P8：夜间约 12% 的雪片带冰青闪光

P8：夜间约 12% 的雪片带冰青闪光（泰坦物质），由 glint 颜色决定是否启用。

### `function resolveMotion(level) {`

> 读取 app.js 写在 <html> 上的动态档位

读取 app.js 写在 <html> 上的动态档位：auto=完整，light=静态单帧，off=静态单帧且不循环。

### `export function mountSnowWorld(stageEl, { visualKey = "generic", motionLevel, role = "stag`

> 在 stageEl

在 stageEl（#scene-stage）内建立分层世界：
  layer-far（室外天空/远山或 generic 全屏） → snow-far（远雪）
  → layer-room（室内墙+窗洞） → [保留的 #stage-character-art] → snow-near（室外大雪/室内光尘）
立绘保持在原 DOM 位置；世界层插在它前后，所以不接管立绘。

### `const inPersonVisible = surface && !surface.hidden;`

> 通讯器背后的世界

通讯器背后的世界：面对面可见时暂停（只让一个世界在动），文字界面可见时运行；
其容器自身被隐藏（flag 关 / 手机）时也暂停。

### `const override = document.documentElement.dataset.snowTime;`

> 时间最多每 10 分钟刷新一次

时间最多每 10 分钟刷新一次；但显式 html[data-snow-time] 覆盖（测试/截图用）立即生效。

### `let cancelCamera = null;`

> 相机推进

相机推进（P6a）：从 generic 室外景朝亮灯入口 .push-target 推近，雪片迎面掠过，
在白光掩护里把世界切到室内场景。只动 transform/opacity（Web Animations，满足 CSP），
不改任何逻辑/请求/回复计时。调用方传 onWhiteout（白光峰值时执行场景切换与 veil），
本函数负责图层推近动画，并在推进到接近白光时调用 onWhiteout，再把图层恢复静止。
off/light 档位与减少动态下，调用方不应调用本函数（直接走 veil）。

### `far.dataset.pushing = "1";`

> 用数据属性标记正在推进，供证据截图/测试断言读取

用数据属性标记正在推进，供证据截图/测试断言读取（Web Animations 是否在跑）。

### `let whiteoutTimer, finishTimer, settle;`

> 硬性兜底

硬性兜底：无论动画是否正常 finish，推进总时长 + 余量后强制清除 pushing 状态，
避免被中途 setScene/暂停打断时 pushing 卡死（否则后续入室会误判为仍在推进）。

### `const commit = () => { if (key && key !== world.scene) { world.scene = key; render(); } };`

> 推进完成后由 onWhiteout 回调切场景

推进完成后由 onWhiteout 回调切场景；若调用方未接管切换，这里兜底切到目标场景。

## app.js

2026-10-19：为给后续功能腾出首屏体积，`App/public_frontend/app.js` 里较长的整行注释移到这里，代码中只保留一句提要；acorn 8.14.0 核对过移动前后代码记号完全一致。行号是移动前的行号。

### `const SNOW_LAST_SEEN_KEY = "project-snow-public:snow-adv:last-seen";`

> 雪境 ADV P6a/P7：开关随 /public/v1/config 下发，皮肤只能在 config 解析后才切换。

（原第 8 行）

雪境 ADV P6a/P7：开关随 /public/v1/config 下发，皮肤只能在 config 解析后才切换。
P7：index.html 静态带 data-snow-hold，app.css 据此在 config 解析前只显示品牌底色（小吉终端图标），
不露出未换肤的界面（线上首访实测旧界面会先露出 0.5–0.9 秒再被开机层盖住）。config 解析或
失败时由 snowClearFirstFlashHold() 移除（配置迟迟不来最多保持 2.5 秒）；JS 根本没跑起来时 app.css 的 4 秒动画兜底自动显示。
data-snow-booting 继续标记「上次见过雪境皮肤」的返回用户（兼容既有检查），不再把整页设为不可见。

### `const CHARACTER_AVATAR_OVERRIDES = Object.freeze({`

> Reuse the approved neutral face crop without changing the original media…

（原第 43 行）

Reuse the approved neutral face crop without changing the original media attribution.

### `function TypingIndicatorState({ channel, characterId, requestId, phase = "typing" }) {`

> A request may outlive the character currently visible in the UI.

（原第 171 行）

A request may outlive the character currently visible in the UI.  Keeping
the owner fields together makes every render/cleanup decision explicit and
prevents a late response from borrowing another character's stage.

### `if (announcementRenderSignature() !== announcements.renderSignature) renderAnnouncements();`

> An unchanged background refresh must preserve keyboard focus and the reader's…

（原第 332 行）

An unchanged background refresh must preserve keyboard focus and
the reader's place inside the open announcement or birthday list.

### `const MIA_BUNDLED_EXPRESSION_MANIFEST_HASHES = Object.freeze([`

> The same approved bundled JSON in Git/LF and a Windows/CRLF checkout.

（原第 488 行）

The same approved bundled JSON in Git/LF and a Windows/CRLF checkout.
Advertised release manifests always use their one exact catalogue digest.

### `function snowAdvActive() { return document.documentElement.dataset.ui === "snow-adv"; }`

> 雪境 ADV 演出 (P4)：是否处于雪境皮肤。

（原第 671 行）

雪境 ADV 演出 (P4)：是否处于雪境皮肤。以 <html data-ui> 为唯一真源（syncInterfaceSkin
设置、CSS 与动态档位都读它），这样测试只需切 data-ui 即可驱动，无需等 config。

### `const SNOW_ADV_FONT_STYLESHEET = "/snow-adv-fonts.css";`

> 雪境 ADV skin (P1).

（原第 716 行）

雪境 ADV skin (P1).  Off unless the server reports features.ui_snow_adv.
Turning it on only adds html[data-ui="snow-adv"], links the story font and
swaps the static Unicode control glyphs for SVG icons; turning it off
restores the original glyphs, so the legacy interface is untouched.

### `const SNOW_TONE_KEY = "project-snow-public:tone";`

> P8：界面深色状态。

（原第 775 行）

P8：界面深色状态。设置里三档：跟随舞台时段（默认，黄昏与夜晚变深）/ 雪白 / 深色。
「跟随」与雪景订阅同一个时段时钟（判定相同），跨时段时同一拍切换。

### `let snowComposerObserver = null;`

> P8：面对面舞台延伸到输入区下方。

（原第 805 行）

P8：面对面舞台延伸到输入区下方。输入区高度会随动作框、请求状态行和窗口宽度变化，
用 ResizeObserver 写入 #chat-app 的 --snow-composer-h，样式据此给舞台加一行、上移立绘。

### `let snowWorld = null;`

> 雪境 ADV 世界层 (P2)：开关开启且处于面对面界面时，在 #scene-stage 内挂载程序化分层雪景与 Canvas 远近雪；关闭或离开面对面时销毁。

（原第 824 行）

雪境 ADV 世界层 (P2)：开关开启且处于面对面界面时，在 #scene-stage 内挂载程序化
分层雪景与 Canvas 远近雪；关闭或离开面对面时销毁。立绘仍留在原 DOM 位置。

### `let snowBackdrop = null;`

> P6a：通讯器背后的世界——一个 body 级 fixed 的「舞台」容器，仅桌面开关开启时存在， aria-hidden、pointer-events:non…

（原第 827 行）

P6a：通讯器背后的世界——一个 body 级 fixed 的「舞台」容器，仅桌面开关开启时存在，
aria-hidden、pointer-events:none、z-index 低于 #chat-app；文字界面可见时作为雪境背景，
面对面可见时暂停（只让一个世界在动）。手机与关闭开关时都不建。

### `function snowBackdropScene() {`

> 背后世界应显示的场景：首次入室镜头推进播放前恒为 generic 外景（推进从外景推入）；推进播放后（snowPushPlayed）跟随当前室内地点。

（原第 841 行）

背后世界应显示的场景：首次入室镜头推进播放前恒为 generic 外景（推进从外景推入）；
推进播放后（snowPushPlayed）跟随当前室内地点。关闭推进（light/off）时入室即跟随室内。

### `function suppressArrivalDuplicateNotice() { return featureEnabled("ui_snow_adv"); }`

> P3 面对面主屏全部只在 ui_snow_adv 开启时生效。

（原第 896 行）

---------------------------------------------------------------- P3 面对面主屏
全部只在 ui_snow_adv 开启时生效。▼ 推进标记与快捷菜单由 JS 在开关开启时建到对白
框里、关闭时移除，所以关闭态不新增任何 DOM、图标或可见元素（旧界面逐字节不变）。

### `function suppressArrivalDuplicateNotice() { return featureEnabled("ui_snow_adv"); }`

> 到场提示去重：开关开启时，仅保留舞台通知（旁白 / 对白），抑制重复的横幅与状态行；屏幕阅读播报仍只发生一次（来自 #stage-narration / #st…

（原第 900 行）

到场提示去重：开关开启时，仅保留舞台通知（旁白 / 对白），抑制重复的横幅与状态行；
屏幕阅读播报仍只发生一次（来自 #stage-narration / #stage-speech 的 aria-live）。

### `const stageActorAnchorCache = new Map();`

> 立绘中轴：在缩小的同源副本上取头到腰（高度 12%~55%）的不透明像素重心，返回 0~1。

（原第 904 行）

立绘中轴：在缩小的同源副本上取头到腰（高度 12%~55%）的不透明像素重心，返回 0~1。
每个角色只测一次（按角色 id 缓存），换表情沿用缓存值，避免人物左右跳动。
画布被污染、读不到像素或任何错误都退回 0.5。

### `function syncStageNarrationMount() {`

> 雪境 ADV（P7）：旁白放进对白框，排在名牌与台词之间，不再作为单独的条压在立绘上。

（原第 1036 行）

雪境 ADV（P7）：旁白放进对白框，排在名牌与台词之间，不再作为单独的条压在立绘上。
只移动既有 #stage-narration 节点（id、aria-live、文本写入方式都不变）；关闭开关时放回舞台。

### `function buildSnowVeil() {`

> 雪境 ADV (P4 后续)：白雪转场遮罩。

（原第 1051 行）

雪境 ADV (P4 后续)：白雪转场遮罩。生产环境 motion_v2/scene_actions_v2 开启时，旧的三个
转场层根本不显示，所以用这个独立的、仅装饰的覆盖层来呈现地点转场。节点仅在开关开启时建、
aria-hidden、pointer-events:none；地点播报仍只走既有 aria-live（本层不参与无障碍播报）。

### `function maybePlaySnowVeil(scene, visualKey) {`

> 决定是否在本次 renderScene 播放遮罩，并在遮罩掩护下切换 P2 世界场景（仅视觉延迟，不延迟逻辑）。

（原第 1106 行）

决定是否在本次 renderScene 播放遮罩，并在遮罩掩护下切换 P2 世界场景（仅视觉延迟，不延迟逻辑）。
触发：开关开启、面对面可见、已布防（排除首帧/历史恢复）、地点确实变化。
不触发：初始加载/历史恢复、重开通讯器回到同一地点、地点未变、off/减少动态。

### `const SNOW_BOOT_SEEN_KEY = "project-snow-public:snow-adv:boot-seen";   // 同一日历日跳过开机视觉`

> P6a 开场与通讯器世界层全部仅在 ui_snow_adv 开启时生效，节点由 JS 在开关开启时建出、关闭态不存在，所以 flag-off 的 DOM 与行为…

（原第 1180 行）

================================================================ P6a 开场与通讯器世界层
全部仅在 ui_snow_adv 开启时生效，节点由 JS 在开关开启时建出、关闭态不存在，所以
flag-off 的 DOM 与行为逐字节不变。开机/锁屏二选一在页面加载时按屏幕判定一次
（与手机布局同一 860px 分界），中途改窗口大小不切换。减少动态/off 档无动画。

### `function snowOpeningActive() {`

> 开机层或锁屏层是否正占据屏幕——其间不得弹出体验须知、新手引导或任何对话框（<dialog>.showModal() 走顶层，会盖在开机/锁屏之上），须在消隐/…

（原第 1195 行）

开机层或锁屏层是否正占据屏幕——其间不得弹出体验须知、新手引导或任何对话框
（<dialog>.showModal() 走顶层，会盖在开机/锁屏之上），须在消隐/解锁后按正常顺序出现。

### `function buildSnowBoot() {`

> 开机层「终端开机」（桌面）

（原第 1217 行）

---- 开机层「终端开机」（桌面） ----

### `const app = $("chat-app");`

> 完整动效：标志先收起（≈200ms），底色随后淡开（≈260ms），底下的通讯器各部分依次入场；入场结束后再交还给体验须知等，避免弹窗压在正在入场的界面上。

（原第 1263 行）

完整动效：标志先收起（≈200ms），底色随后淡开（≈260ms），底下的通讯器各部分依次入场；
入场结束后再交还给体验须知等，避免弹窗压在正在入场的界面上。不额外等待网络。
淡开后的开机层已透明且不拦截点击，保留到入场结束才移除，使「开机层移除」始终等于「开场完成」。

### `function snowLockNotes() {`

> 锁屏层「终端锁屏」（手机）

（原第 1287 行）

---- 锁屏层「终端锁屏」（手机） ----

### `const notes = [];`

> 通知卡仅由既有状态构造：当前待赴约定、未读公告。

（原第 1289 行）

通知卡仅由既有状态构造：当前待赴约定、未读公告。各源独立容错——某子系统
尚未就绪时该卡缺省，绝不阻断开场。

### `function snowMaybePushIntoScene(scene, visualKey, commitScene) {`

> 通讯器背后的世界层（桌面） 首次由「外面」进入某室内地点时的镜头推进 → 随后交给 P4 veil（显示目的地）。

（原第 1439 行）

---- 通讯器背后的世界层（桌面） ----
首次由「外面」进入某室内地点时的镜头推进 → 随后交给 P4 veil（显示目的地）。供 renderScene 调用。
返回 true 表示本次已用推进接管场景切换（调用方不再播放自己的 veil / 立即 setScene）。
推进作用于 body 级的「通讯器背后世界」(snowBackdrop)——它在人入室前显示 generic 外景，
其 .push-target 是基地外亮灯入口；推进把外景推近入口 → 白光掩护里切到室内 → veil 显示目的地。

### `if (snowWorld) snowWorld.setMotion(currentMotionLevel());`

> 仅把舞台的视觉层暂时让开，逻辑/回复照常推进。

（原第 1459 行）

仅把舞台的视觉层暂时让开，逻辑/回复照常推进。外景与入口真实可见，且只运行外景雪花。

### `function bodyIsVisible(node) {`

> /P6a

（原第 1475 行）

================================================================ /P6a

### `requestAnimationFrame(() => {`

> First glyph paint measures rendering; a full confirmed speech block measures an…

（原第 1494 行）

First glyph paint measures rendering; a full confirmed speech block measures
an effective reply. Decorative typewriting must not improve the latter.

### `try {`

> Small slices allow a live reduced-motion change or fast-forward to finish…

（原第 1537 行）

Small slices allow a live reduced-motion change or fast-forward to finish promptly.

### `const range = IDBKeyRange.bound([characterId, 0], [characterId, upper], false, typeof before === "nu…`

> Include a composite cursor's timestamp: the primary key breaks ties without…

（原第 1981 行）

Include a composite cursor's timestamp: the primary key breaks ties
without changing the v4 database or requiring a destructive migration.

### `normalizedMessages.forEach(markMessageEntrySeen);`

> Loaded history, including legacy embedded rows, is already established content…

（原第 2109 行）

Loaded history, including legacy embedded rows, is already established
content even when its timestamp is recent or has been estimated.

### `if ((stored?.memories?.length || stored?.sessionContextPackage) && !(await messageCount(characterId)…`

> An older v4 client can delete a character without knowing the sidecar key.

（原第 2279 行）

An older v4 client can delete a character without knowing the sidecar
key. Do not revive experiences or a scene whose transcript was erased.

### `if (!reconcilable) {`

> A newly created empty thread can have its first join in flight.

（原第 2287 行）

A newly created empty thread can have its first join in flight. Its
real metadata distinguishes it from an older client's deletion; query
that immutable UUID before deciding whether a scene was established.

### `if (state.worldPackage && incoming.scheduleDate && current.scheduleDate && incoming.scheduleDate < c…`

> A slow response from a character the user has already left must not roll the…

（原第 2361 行）

A slow response from a character the user has already left must not roll
the shared world back over a newer signed package. A revision identifies
exactly one signed payload; response order must not choose between two
different tokens carrying the same revision.

### `if (!incoming.scheduleDate) return false;`

> A dated 0.9.2 package supersedes an undated legacy package.

（原第 2372 行）

A dated 0.9.2 package supersedes an undated legacy package. An undated
late response must never replace a dated package from the current day.

### `const threadRecords = await storeAll("threads");`

> v3 stored a signed world package inside each thread.

（原第 2401 行）

v3 stored a signed world package inside each thread. The v4 migration
intentionally stopped writing it, but old metadata can still survive an
upgrade. Strip both spellings so a later boot cannot resurrect a token
that was rejected for a different anonymous subject.

### `const write = async () => {`

> Capture each revision now, then write in that same order.

（原第 2534 行）

Capture each revision now, then write in that same order. A slower earlier
openDB must never commit an empty snapshot over a newer action draft.

### `const ownerKey = state.composerDraftKey || draftKey();`

> Persistence can update the thread's channel before its visible composer.

（原第 2547 行）

Persistence can update the thread's channel before its visible composer.
Keep edits with the character and channel that still own these controls.

### `widget = turnstile.render(container, {`

> Native modal dialogs make body-level widgets inert and hide them behind the…

（原第 2651 行）

Native modal dialogs make body-level widgets inert and hide them behind
the backdrop. Keep the interactive challenge inside the model form.

### `dialog.addEventListener("cancel", (event) => event.preventDefault());`

> There is deliberately no silent-dismiss path: an Escape key press must not…

（原第 2768 行）

There is deliberately no silent-dismiss path: an Escape key press must
not leave boot waiting forever or allow the user to enter without the
one-time product notice. Keep the dialog open until it is acknowledged.

### `try {`

> P6a 首帧处理：config 解析后解除中性保持（见模块顶部的 data-snow-booting 守卫）。

（原第 3082 行）

P6a 首帧处理：config 解析后解除中性保持（见模块顶部的 data-snow-booting 守卫）。
并记录「上次见过雪境皮肤」的标记——仅当存在该标记时，下次才在 config 解析前保持中性，
避免首访用户为了防闪而被挡一帧；关闭时清除标记。

### `state.summaryControllers.get(thread.characterId)?.abort();`

> Keep the local transcript and world state, but retire yesterday's model…

（原第 3341 行）

Keep the local transcript and world state, but retire yesterday's
model context, including any summary that has not finished yet.

### `function flushCommunicatorPresentation(characterId = state.selected, { render = true } = {}) {`

> A communicator queue owns only a visibility cursor.

（原第 3567 行）

A communicator queue owns only a visibility cursor. All confirmed content is
saved independently; neither ending a network request nor a visual cancellation
can discard received text or create another generation request.

### `if (start) void Promise.resolve().then(() => driveCommunicatorReply(queue));`

> Let the terminal handler finish its scene/status bookkeeping before a…

（原第 3607 行）

Let the terminal handler finish its scene/status bookkeeping before a
reduced-motion or already-flushed queue clears the request's typing owner.

### `if (plannedVersion !== queue.version) {`

> Replan remaining whole bubbles when more confirmed blocks arrive.

（原第 3637 行）

Replan remaining whole bubbles when more confirmed blocks arrive. No
final-block shortcut: compression applies proportionally to every wait.

### `if (!pending && currentThread()?.channel === "in_person") {`

> The face-to-face surface intentionally hides the text timeline.

（原第 3828 行）

The face-to-face surface intentionally hides the text timeline. Keep the
same retry action reachable from its status row when the last user turn
failed, so the preserved action/speech draft is not the only recovery
path. Text mode already renders this action beside the failed message.

### `const exactMatches = new Set(`

> An exact pinyin/initial token is more intentional than a substring.

（原第 3991 行）

An exact pinyin/initial token is more intentional than a substring. For
example, `kxy` selects 凯茜娅 without also matching 安卡希雅's `akxy`.

### `const rowCharacter = snowAdvActive() ? \` data-snow-character="${escapeHtml(character.character_id)}"…`

> 雪境 ADV (P5)：每行带 data-snow-character，让联系人卡片用各自的角色色（--char）。

（原第 4014 行）

雪境 ADV (P5)：每行带 data-snow-character，让联系人卡片用各自的角色色（--char）。
仅皮肤开启时写出，关闭态 DOM 与旧界面逐字节一致。

### `if (snowOpeningActive()) { $("onboarding-guide").hidden = true; return; }`

> P6a：开机/锁屏层占屏时不显示新手引导，避免盖在开场层之上；消隐/解锁后由 snowRunDeferredOpenings() 再次调用本函数按正常顺序显示。

（原第 4044 行）

P6a：开机/锁屏层占屏时不显示新手引导，避免盖在开场层之上；消隐/解锁后由
snowRunDeferredOpenings() 再次调用本函数按正常顺序显示。

### `if (featureEnabled("ui_snow_adv") && document.querySelector("dialog[open]")) { $("onboarding-guide")…`

> P7：雪境界面下，体验须知、公告等模态弹窗打开时引导卡先让开（否则会在弹窗背后同时出现），弹窗关闭时由下方的 close 监听重新调用本函数。

（原第 4047 行）

P7：雪境界面下，体验须知、公告等模态弹窗打开时引导卡先让开（否则会在弹窗背后同时出现），
弹窗关闭时由下方的 close 监听重新调用本函数。

### `return new Promise((resolve, reject) => {`

> Production CSP permits data: images.

（原第 4215 行）

Production CSP permits data: images. Encode only already verified bytes;
do not fetch the image again or retain another encoded-image cache.

### `await Promise.all(loading);`

> Download and decode independent layers together, retaining manifest order for…

（原第 4473 行）

Download and decode independent layers together, retaining manifest order
for composition. Nothing is displayed until every source is verified.

### `const key = JSON.stringify([presentation?.renderStrategy, presentation?.stageAssetPath,`

> Include every pixel/composition input, so a changed digest or layer position…

（原第 4543 行）

Include every pixel/composition input, so a changed digest or layer position
can never borrow a prepared surface from another presentation.

### `if (value.prepared.renderStrategy !== presentation.renderStrategy) cache.delete(key);`

> A transient layered failure must be retried later, not cached forever as a…

（原第 4560 行）

A transient layered failure must be retried later, not cached forever as
a flattened fallback. The caller still owns that successful fallback.

### `finish(resolve, { renderer: value.renderer, prepared: { ...value.prepared, dispose: () => {} } });`

> The cache owns decoded images; disposing one visible consumer must not clear a…

（原第 4591 行）

The cache owns decoded images; disposing one visible consumer must not
clear a surface being reused by a prewarm or another stage update.

### `const firstScreenWaiters = new Set();`

> P8: first-screen budget.

（原第 4597 行）

P8: first-screen budget. Work registered here before the first screen has settled
(page load event plus one idle period, capped) runs afterwards; the returned
function runs it immediately instead (e.g. when the stage itself is the first screen).

### `const snowAdv = snowAdvActive();`

> 雪境 ADV (P4)：换表情不出现透明空档。

（原第 4691 行）

雪境 ADV (P4)：换表情不出现透明空档。外层（旧图）全程保持 opacity 1，
内层（新图，z-index 更高、叠在上面）从 0 淡入到 1，淡入完成后再移除旧图。
这样任意时刻可见画面都是不透明的，绝不会两层同时半透明。关闭开关时沿用旧的交叉淡出。

### `function playStageEnter(node) {`

> 雪境 ADV (P4)：立绘入场。

（原第 4723 行）

雪境 ADV (P4)：立绘入场。只在开关开启时生效，只动 transform/opacity，绝不阻塞逻辑
（纯视觉，调用方不 await）。auto=上浮+淡入(--ease-settle, ~420ms)；light=仅淡入；
off/减少动态/页面隐藏/无 WAAPI=不动画。transform 不与 P3 的 translate 中轴定位冲突。

### `try { animation.cancel(); } catch {}`

> fill:both leaves the final frame latched; clear it so breathing/motion own…

（原第 4744 行）

fill:both leaves the final frame latched; clear it so breathing/motion own transform again.

### `try {`

> The bundled approved neutral remains available if the optional release fails…

（原第 4824 行）

The bundled approved neutral remains available if the optional release fails verification.

### `const firstAppearance = !outgoingTransition && (node.hidden || !node.getAttribute("src"));`

> 雪境 ADV (P4)：立绘首次为某场景/到场出现时（上一帧无图或已隐藏，且不是换表情交叉淡入），以 --ease-settle 做 ~420ms 的柔和上浮…

（原第 4854 行）

雪境 ADV (P4)：立绘首次为某场景/到场出现时（上一帧无图或已隐藏，且不是换表情交叉淡入），
以 --ease-settle 做 ~420ms 的柔和上浮(≈12px→0)+淡入(0→1)。light 仅淡入，off 不做。

### `if (snowAdvActive()) {`

> 雪境 ADV (P4)：带缓冲、小振幅、会 settle 的曲线，替代旧的 ±10px 生硬抖动。

（原第 4905 行）

雪境 ADV (P4)：带缓冲、小振幅、会 settle 的曲线，替代旧的 ±10px 生硬抖动。
动作名与触发完全不变；只在开关开启时走这一支。只动 transform。

### `if (layer.dataset.owner !== String(owner)) return result;`

> A newer selection may have taken ownership while the operation was awaiting the…

（原第 5129 行）

A newer selection may have taken ownership while the operation was
awaiting the network. Never let an old transition hide or relabel it.

### `const warmNeutral = () => { void warmCharacterStage(characterById(characterId), "neutral", "", contr…`

> Start artwork alongside local history and presence resolution, while the user…

（原第 5245 行）

Start artwork alongside local history and presence resolution, while the
user is still in the contact/transition UI rather than after opening the stage.
P8: during the first screen the communicator is what the visitor sees, so the
(large) neutral layers wait until the page has loaded and gone idle; an
in-person thread flushes the warm immediately below because its stage is the first screen.

### `const connection = navigator.connection;`

> Data-saver or 2G/3G visitors who stay in the communicator never download the…

（原第 5251 行）

Data-saver or 2G/3G visitors who stay in the communicator never download the
layers; entering face-to-face still loads them through the foreground path.

### `const overlay = context?.scene_overlay;`

> Older signed contexts kept an accepted-now invitation after applying its…

（原第 5590 行）

Older signed contexts kept an accepted-now invitation after applying its
movement. Only hide that exact event at its still-confirmed destination.

### `if (featureEnabled("motion_v2") && $("text-surface").hidden) return;`

> Render the saved window when the communicator becomes visible.

（原第 5634 行）

Render the saved window when the communicator becomes visible. Rebuilding
hidden history during a stage transition adds work and loses scroll geometry.

### `timeline.style.scrollBehavior = "auto";`

> Automatic anchoring is immediate.

（原第 5643 行）

Automatic anchoring is immediate. CSS smooth scrolling can otherwise
leave every render reading an intermediate position from the last update.

### `const emptyBody = snowAdvActive() && currentCharacter()`

> 雪境 ADV (P5)：空会话用角色色问候卡取代纯灰文字；文案不变。

（原第 5658 行）

雪境 ADV (P5)：空会话用角色色问候卡取代纯灰文字；文案不变。关闭态保持原样。

### `const clockFormatter = new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", hour12: fal…`

> Resolve the device locale/timezone once per render, keeping live changes while…

（原第 5669 行）

Resolve the device locale/timezone once per render, keeping live changes
while avoiding a formatter allocation for every historical timestamp.

### `node.classList.add("is-snow-pop");`

> 雪境 ADV (P5)：气泡 pop-in 由 CSS 关键帧按动态档位驱动（auto 上浮+淡入、 light 仅淡入、off 无），只动 transform…

（原第 5698 行）

雪境 ADV (P5)：气泡 pop-in 由 CSS 关键帧按动态档位驱动（auto 上浮+淡入、
light 仅淡入、off 无），只动 transform/opacity。加类即可，不再用 WAAPI。

### `const anchor = [...timeline.querySelectorAll("[data-message-id]")].find(node => node.dataset.message…`

> Keep the same historical row at the same viewport position when reply bubbles…

（原第 5712 行）

Keep the same historical row at the same viewport position when reply
bubbles, media or time labels are appended below the reader.

### `if (state.stageSpeechGate?.key !== speechKey) {`

> Repeated renders share one deadline.

（原第 5904 行）

Repeated renders share one deadline. A stalled image can delay the first
spoken text only briefly, and late completions cannot repaint a new scene.

### `if (liveReply && (completeSpeech || action)`

> Confirmed replies take priority over decorative arrival/scene transitions…

（原第 5959 行）

Confirmed replies take priority over decorative arrival/scene transitions,
including recovered replies and legacy arrival reactions. Cancel before
typewriting or measuring paint; renderScene may have just started a veil.

### `if (persist && thread && thread.characterId === state.selected) await saveDraftNow();`

> Selection already saved the previous owner's composer.

（原第 6111 行）

Selection already saved the previous owner's composer. Its stale DOM
must not overwrite the newly selected character's persisted draft.

### `if (thread && thread.characterId !== state.selected) return;`

> A character switch may happen while the persistence write is pending.

（原第 6121 行）

A character switch may happen while the persistence write is pending.
Only the selected thread owns the visible surface and composer controls.

### `if (featureEnabled("ui_snow_adv") && channel === "in_person" && !window.matchMedia("(max-width: 820p…`

> 雪境 ADV P3：开关开启进入面对面时，角色栏改为覆盖式抽屉，默认关闭让舞台占满视口；桌面端用既有 sidebar-collapsed 机制（#open-st…

（原第 6138 行）

雪境 ADV P3：开关开启进入面对面时，角色栏改为覆盖式抽屉，默认关闭让舞台占满视口；
桌面端用既有 sidebar-collapsed 机制（#open-stage-contacts 仍用 toggleContacts 开合），
不持久化、不改旧 UI 的侧栏偏好语义。移动端（≤820px）沿用抽屉逻辑，无需处理。

### `if (composerChanged) restoreDraft();`

> Arrival can confirm the already-visible channel after an IndexedDB write.

（原第 6160 行）

Arrival can confirm the already-visible channel after an IndexedDB write.
Keep edits made during that wait; only a new character/channel restores a
different draft into the composer.

### `if (input.value && input.offsetHeight >= maximum && input.scrollHeight > input.clientHeight) return;`

> While capped text still overflows, leave the live box alone so the browser can…

（原第 6193 行）

While capped text still overflows, leave the live box alone so the browser
can keep scrolling the caret as typing continues. Resetting its height here
would undo that scroll. Deletion/widening that fits re-enters measurement.

### `input.style.height = "auto";`

> Measure from the intrinsic single row so deletion and wider layouts shrink…

（原第 6198 行）

Measure from the intrinsic single row so deletion and wider layouts shrink
again. Only the box changes; drafts, selection and manual scroll stay intact.

### `const accepted = await saveWorldPackage(payload.state_package, { persist: false });`

> Apply the same signed-package ordering gate in memory first.

（原第 6390 行）

Apply the same signed-package ordering gate in memory first. Local
disk failures cannot interrupt reading a server-confirmed done packet.

### `void reader.cancel().catch(() => {});`

> A verified done packet completes the request.

（原第 6447 行）

A verified done packet completes the request. Waiting for a socket EOF
can otherwise turn a saved reply into a timeout and offer a paid resend.

### `if (["public_database_unavailable", "subscription_result_unknown", "stream_commit_invalid", "stream_…`

> retryable=false prevents another generation; uncertain results still need their…

（原第 6477 行）

retryable=false prevents another generation; uncertain results still need
their original UUID so a later reconciliation cannot charge a new request.

### `try {`

> Optional, isolated analytics signal: identifiers only; never inspect…

（原第 6590 行）

Optional, isolated analytics signal: identifiers only; never inspect requestBody.

### `state.streamingPresentationByCharacter.set(characterId, { requestId, receivedCount: committedReply.b…`

> A manual recovery keeps its already delivered prefix on screen even if replay…

（原第 6633 行）

A manual recovery keeps its already delivered prefix on screen even if
replay accepts only duplicate blocks before the terminal reply arrives.

### `materializeCommitted();`

> Receipt and storage never wait for the cosmetic delivery cursor.

（原第 6660 行）

Receipt and storage never wait for the cosmetic delivery cursor. Saving
the whole accepted prefix also makes reload/recovery immediately readable.

### `void commitDisplay.catch(() => {});`

> Transport reads continue while a short presentation pause or disk write…

（原第 6688 行）

Transport reads continue while a short presentation pause or disk write finishes.

### `updateTypingPhase(characterId, requestId, "recovering");`

> Transport recovery is actionable immediately, even while the normal typing…

（原第 6722 行）

Transport recovery is actionable immediately, even while the normal
typing indicator is still inside its natural onset window.

### `window.clearTimeout(requestTimeout);`

> A confirmed terminal reply is a completed business action.

（原第 6754 行）

A confirmed terminal reply is a completed business action. Cosmetic waiting
can no longer cancel it or expose a retry with a fresh paid-request ID.

### `userMessage.status = "sent";`

> UI or storage faults after a verified terminal reply cannot downgrade the…

（原第 6834 行）

UI or storage faults after a verified terminal reply cannot downgrade
the completed business action or create a fresh paid-request retry.

### `if (communicatorPaced && committedReply?.blocks.length) {`

> Expose confirmed content before cancellation renders with the streaming cursor…

（原第 6875 行）

Expose confirmed content before cancellation renders with the streaming
cursor, so a previously visible prefix keeps its original DOM nodes.

### `if (remainingProviderCallBudget(chatUsage) < 1) return;`

> A summary is another provider operation belonging to the current user action.

（原第 7025 行）

A summary is another provider operation belonging to the current user
action. Never start it when validation/rewriting already consumed both
calls, or when an older server omitted the auditable call count.

### `if (thread.summaryRequestId) {`

> A provider may have accepted an interrupted request.

（原第 7043 行）

A provider may have accepted an interrupted request. A later summary
gets a new ID and checkpoint instead of replaying a possibly charged call.

### `if (thread.characterId === state.selected) setRequestStatus(thread.characterId, "对话已完成；连续性整理稍后重试。");`

> Only a future successful user turn may schedule another summary.

（原第 7087 行）

Only a future successful user turn may schedule another summary. Its
turns/checkpoint differ, so it must never reuse this paid-request UUID.

### `if (cleared.arrivalSnapshot) {`

> Persist the immutable arrival with the confirmed join.

（原第 7149 行）

Persist the immutable arrival with the confirmed join. Credentials are
attached only at dispatch and never enter browser request snapshots.

### `return performSceneAction("resolve", characterId, signal, { scopeRecoveryAttempt: scopeRecoveryAttem…`

> Scope rejection is confirmed before generation.

（原第 7187 行）

Scope rejection is confirmed before generation. Re-establish only the
model-free scene read; the immutable chat request is never resubmitted.

### `await sendPresenceArrival(characterId, { recoveryOnly: Boolean(snapshot.dispatched) });`

> A durable undispatched snapshot proves that no HTTP attempt began.

（原第 7212 行）

A durable undispatched snapshot proves that no HTTP attempt began. Resume
that same entry once; any dispatched snapshot is strictly lookup-only.

### `if (!suppressArrivalDuplicateNotice()) {`

> 雪境 ADV：舞台旁白「你已来到X。

（原第 7346 行）

雪境 ADV：舞台旁白「你已来到X。」已是到场通知（aria-live 播报一次），
开关开启时不再重复显示状态行与横幅，只清掉进行中的状态；关闭时保留旧的三处提示。

### `if (ownsVisibleArrival()) {`

> Populate the arrival turn before exposing the stage.

（原第 7450 行）

Populate the arrival turn before exposing the stage. Otherwise
setChannel() can make the scene visible for one paint while the
reaction is still being persisted, producing an empty dialogue box.

### `const stageHasArrivalReaction = Boolean(arrivalMessage) && suppressArrivalDuplicateNotice();`

> 雪境 ADV：到场反应已显示在舞台对白框（旁白 + 对白，aria-live 播报一次），开关开启且确有舞台反应时抑制重复的「她注意到了你的到来。

（原第 7456 行）

雪境 ADV：到场反应已显示在舞台对白框（旁白 + 对白，aria-live 播报一次），
开关开启且确有舞台反应时抑制重复的「她注意到了你的到来。」横幅；错误 / 未注意
（无舞台反应）仍保留横幅，保证至少有一处可见通知。

### `if (button.dataset.closeDialog === "announcements-dialog") acknowledgeAnnouncements();`

> Persist before close(): its event is asynchronous and a quick reload must not…

（原第 7953 行）

Persist before close(): its event is asynchronous and a quick reload
must not lose the receipt or show the same announcement again.

### `if (width > 0) scheduleMessageInputResize();`

> Height-only observations must not loop.

（原第 8001 行）

Height-only observations must not loop. Defer width-driven layout work;
recording zero also makes showing a previously hidden composer remeasure.

### `let snowBoot = false;`

> P6a：开关只在 config 解析后才已知，故开机/锁屏层只能此时才出现。

（原第 8285 行）

P6a：开关只在 config 解析后才已知，故开机/锁屏层只能此时才出现。
桌面 → 终端开机（真实加载步驱动，就绪即消隐，不加延迟）；手机 → 终端锁屏。
开场层永不阻断启动：任何构造异常都降级为「无开场层」。

### `if (!snowBoot && !snowLock) await showExperienceNoticeIfNeeded();`

> 体验须知与新手引导：桌面开机消隐后、手机解锁后按正常顺序出现（绝不盖在开场层之上）；其余情况（flag 关 / 跳过开机锁屏）保持原有顺序——config 之后…

（原第 8293 行）

体验须知与新手引导：桌面开机消隐后、手机解锁后按正常顺序出现（绝不盖在开场层之上）；
其余情况（flag 关 / 跳过开机锁屏）保持原有顺序——config 之后立即弹须知。
