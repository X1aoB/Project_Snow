# 雪境 ADV 实现注记

为控制首屏体积，`App/public_frontend/snow-adv.css` 与 `App/public_frontend/modules/snow-world.js` 里较长的设计说明移到了这里，代码中只保留一句提要。按文件和出现顺序排列；每条的标题是注释后紧跟的那行代码，引用块是代码里保留的提要。

## 基地地图接入

地图模块只接收角色清单、场景包、当前角色和忙碌状态的快照，以及既有前往流程与图标回调；不接收整个应用状态，避免把模型凭证带入新模块。打开地图不调用 `selectCharacter()` 或场景解析；只有用户点击「去见她」才进入原有选择与确认流程。面对面切换角色本来可能已完成到场，因此接入层先核对当前频道与同场状态，不再次到场；错误被既有流程捕获时，以实际确认窗口或同场状态判断结果。

独立样式先于模块写入资源清单的依赖；模块先于 `app.js`，使生产构建可将动态模块和 CSS 的完整引用同时改写为内容指纹 URL。样式加载成功后才挂入口，地图打开、关闭、再次打开均不发新请求。首屏稳定前不导入该模块，关闭雪境时也不导入。

头像占位在绑定事件的同步步骤处理，早于首帧绘制，也早于已绑定图片的快速返回；这样缓存失败和再次渲染仍会显示首字。尚未完成的图片在该卡片内隐藏，成功后沿用原有 load 事件切换；错误沿用原有移除图片的处理。只匹配雪境问候卡，不改变其他头像。

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
