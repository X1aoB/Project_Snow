# 雪境 ADV 界面（`ui_snow_adv`）

雪境 ADV 是公开前端的新界面皮肤：雪白底、冰蓝强调、每位角色一个主题色，对白与名牌使用霞鹜文楷屏幕阅读版。它按阶段提交，统一通过发布 PR 交付，全部放在功能开关 `ui_snow_adv` 后面。

## 开关

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `PUBLIC_UI_SNOW_ADV` | `false` | 只接受精确的 `true`（不区分大小写）。开启后 `/public/v1/config` 的 `features.ui_snow_adv` 为 `true`，前端给 `<html>` 加 `data-ui="snow-adv"`。 |

- 纯展示开关：不改 public v1、`public-state-2`、IndexedDB v4、请求哈希、快照，也不增加模型调用。
- 开关关闭时，`snow-adv.css` 的所有规则都匹配不到，图标字符与字体都不替换，旧界面不受影响。
- 发布接入（P6）：`ops/deploy.sh` 的公开环境白名单接受 `PUBLIC_UI_SNOW_ADV`，只认精确的 `true`/`false`（其他值拒绝并保留原环境文件），缺省写为 `false`，并固定写进每个发布的环境快照。上线时由 root 在 `/etc/project-snow/candidate-flags/<候选 SHA>.env` 里写 `PUBLIC_UI_SNOW_ADV=true`，只对该候选生效，共享的 `public.env` 不变。候选验收在环境带有该开关时把它记入回执（`interface_features`），证据必须写明确切值；回执生成后再改开关，批准即失效。正式上线仍需该候选的具体回执批准。
- 本地预览：开发环境设置 `PUBLIC_UI_SNOW_ADV=true` 后启动公开服务即可。

## P1 设计基础（本阶段）

| 文件 | 内容 |
|---|---|
| `public_frontend/snow-adv.css` | 设计令牌（雪/冰/墨/暖色、22 位角色色、字号层级、圆角、阴影、缓动），把共享变量 `--snow-*` 映射到新色板；按钮、输入框、面板、弹窗、提示、焦点样式；所有低于 12px 的字号提升到 12px；对白与名牌字体。 |
| `public_frontend/snow-adv-fonts.css` | 霞鹜文楷 `@font-face`，开关开启时由 `app.js` 懒加载，`font-display: swap`。 |
| `public_frontend/assets/fonts/lxgw-wenkai-screen/` | 95 个 woff2 分片（共约 4.5 MB，浏览器只下载用到的分片）、`OFL.txt`、`SOURCE.json`。 |
| `public_frontend/modules/snow-icons.js` | lucide 图标子集（19 个），用 DOM API 生成装饰性 SVG。 |
| `app.js` `syncInterfaceSkin()` | 根据开关设置 `data-ui`、懒加载字体、把静态控件的 Unicode 字符换成 SVG 图标；关闭时还原原字符。切换角色时在 `#chat-app` 上记录 `data-snow-character`，供角色色使用。 |

不受开关控制的一处改动：侧栏和隐私页的小吉终端图标从 1000 KB 的原图改为同一图标的 192px 版本（约 40 KB），显示效果相同。

### 体积

开关关闭时，每次首屏新增约 6.7 KB（gzip）：`snow-adv.css` 3.4 KB、`snow-icons.js` 2.0 KB、`app.js` 增量 1.3 KB；同时少下载约 960 KB 的品牌原图。开关开启后另加载字体样式表约 31 KB（gzip）和实际用到的字体分片。

## P2 程序化雪景与分层雪花（本阶段）

面对面界面的背景从单张 `#scene-backdrop` SVG 升级为 `#scene-stage` 内的程序化分层世界，所有图形在浏览器内由 DOM / SVG 字符串与 Canvas 生成，不请求任何图片，满足生产 CSP（无内联样式元素、无外链 URL、不动态执行代码；唯一出现的 `http://www.w3.org/2000/svg` 是 SVG 命名空间）。

| 文件 | 内容 |
|---|---|
| `public_frontend/modules/snow-world.js` | 新 ES 模块。`mountSnowWorld(stageEl, {visualKey, motionLevel}) → {setScene, setMotion, refresh, destroy, …}`。移植样稿 `snowfx.js` 的 `PALETTES`、`resolveTime`、`rng/mix`、`outdoor()`、九个 `ROOMS` 房间、分层 `mountWorld` 与 `SnowField`（远/近/光尘）。去掉样稿控制台、`localStorage`、白雪转场、相机推进、立绘中轴等后续阶段内容。 |
| `public_frontend/snow-adv.css` | 追加 P2 分层样式：隐藏旧 `#scene-backdrop`、`.snow-atmosphere`、`.scene-decoration-*`；给世界层定 z-index。每条选择器仍限定在 `html[data-ui="snow-adv"]` 下。 |
| `public_frontend/app.js` | 静态 `import mountSnowWorld`；`syncSnowWorld()` 在开关开启且处于面对面界面时挂载世界、离开或关闭时销毁；`renderScene()` 调 `setScene(visualKey)`、`syncMotionState()` 调 `setMotion()`、`setChannel()` 调 `syncSnowWorld()`。控制器句柄挂在 `#scene-stage` 节点属性上（非全局），仅供证据截图/测量读取。 |
| `assets-manifest.json` | 新增 `/modules/snow-world.js` 条目（排在 `app.js` 之前），并加入 `app.js` 的 `dependencies`，使生产指纹化重写其静态导入地址。 |

### 分层

`#scene-stage` 内由后到前：程序化远景（室外天空/远山，或 `generic` 全屏室外）→ 远雪 Canvas → 室内层（带窗洞的墙，使室内雪只透过窗显示）→ 现有 `#stage-character-art` 立绘 → 近景层（室外：大颗近雪；室内：缓慢飘动的光尘）→ 对白。场景键沿用 `app.js` 既有的 `sceneVisualKey`/`SCENE_KEYS`，未知键回退 `generic`，随场景切换更新。只有舞台背景跟随本机时间（`resolveTime`：清晨 5–8、白天 8–17、黄昏 17–19，其余夜晚），最多每 10 分钟刷新；`html[data-snow-time]` 可覆盖（仅测试/截图用）。装饰层全部 `aria-hidden="true"`、`pointer-events: none`。

### 动态档位

读 `document.documentElement.dataset.motionLevel`（`auto`=完整、`light`、`off`；`off` 也覆盖 `prefers-reduced-motion`，由 `syncMotionState()` 维护）：

- `full`：远近雪都动画。
- `light`：静态画一帧（远雪密度降到 0.45，近雪关闭），不启动 rAF 循环。
- `off`：静态画一帧，不循环。

`document.hidden` 或 `#in-person-surface` 隐藏时暂停 rAF 循环，可见时恢复。DPR 上限 2。

### 实测

- **每帧绘制耗时**：headless Chromium、390×844、`generic` 夜景（远近雪都动画）下约 **0.08–0.09 ms/帧**（EMA），远低于 2 ms/帧预算。
- **体积**：`snow-world.js` 约 **11.6 KB（gzip）**，低于 12 KB 预算（原始约 36 KB）。开关关闭时不加载该模块、不改任何 DOM 或请求。

## P3 面对面主屏（本阶段）

把面对面界面（`#in-person-surface`，`in_person` 频道）升级为 galgame 主屏，仅在 `html[data-ui="snow-adv"]` 下生效。开关关闭时行为与 DOM 逐字节不变：▼ 推进标记与快捷菜单由 `app.js` 仅在开关开启时建到对白框里、关闭时移除，不新增任何可见元素或图标。

| 文件 | 内容 |
|---|---|
| `public_frontend/snow-adv.css` | 追加 P3 段：① 全屏舞台——面对面时把永久 304px 的角色栏改成覆盖式抽屉（桌面 `@media (min-width: 821px)` 下 `.app-shell[data-channel="in_person"]` 用单列网格，`.contact-panel` 改 `position: fixed` 滑入/滑出）；② 磨砂雪白宽对白框（渐变 ≥ 0.96 不透明、顶部圆角）、角色色名牌（`.stage-speaker-line strong` 背景 `var(--char-deep)`）、面对面输入区与对白框连成一体；③ ▼ 推进标记样式（仅 `data-motion-level="auto"` 轻微上下浮动，`light`/`off` 与减少动态时静止）；④ 快捷菜单胶囊按钮；⑥ 立绘按身体中轴 `--actor-anchor-x` 对齐舞台中线；⑦ 移动端（≤ 860px）快捷菜单收成仅图标。每条选择器都限定在 `html[data-ui="snow-adv"]` 下（测试强制）。 |
| `public_frontend/snow-adv.css`（末尾） | 关键帧 `@keyframes snow-advance-bob`（▼ 的浮动，只被开关开启时的 `.stage-advance` 规则引用）。作用域测试会跳过 `@keyframes` 块，其余规则仍须限定前缀。`app.css` 不变。 |
| `public_frontend/app.js` | `syncInterfaceSkin()` 调 `syncStageQuickMenuMount()`：开关开启且面对面时，把 `#stage-advance`（▼，`aria-hidden`，图标 `chevron-down`）与 `#stage-quick-menu`（跳过/回看/隐藏/设置四个小胶囊按钮，lucide 图标，各有中文无障碍名、键盘可达、P1 焦点环）建到 `#stage-dialogue` 里，关闭时移除。四个按钮复用既有处理：**跳过**→`finishReceivedPresentation()`（无内容揭示时禁用）、**回看**→`openDrawer("transcript-panel")`、**隐藏**→`#toggle-stage-ui` 行为（经 `#restore-stage-ui`/`H` 恢复）、**设置**→`openSettings("models")`，不新增行为、不新增偏好键，刻意没有「自动」键（自然节奏已是默认）。`syncStageQuickMenu()` 在 `syncFastForward()` 与 `renderStageSpeech()` 中调用：当前回复显示完且空闲（非打字/揭示/等待/排队）时给 ▼ 加 `is-ready`。`applyStageActorAnchor()`/`measureStageActorAnchor()`：开关开启时从当前 `#stage-character-art` 同源图在缩小副本上取头到腰（高度 12%–55%）的不透明像素重心（alpha > 128）得横向中轴 0..1，按角色 id 缓存（换表情沿用、不横跳），画布被污染或出错退回 0.5；并设 `--actor-anchor-x` 让立绘中轴落在舞台中线。`setChannel()`：开关开启进入面对面时默认折叠侧栏（抽屉关闭=舞台全宽，`#open-stage-contacts` 仍用既有 `toggleContacts` 开合），离开面对面时还原自动折叠，不持久化、不改旧 UI 侧栏偏好语义。到场提示去重：`arriveSceneAction()` 与旧版 `arriveInPerson()` 中，开关开启且舞台已有到场反应时抑制重复的横幅与状态行（`suppressArrivalDuplicateNotice()`），只保留舞台旁白/对白这一处可见通知；错误/未注意（无舞台反应）仍保留横幅，屏幕阅读播报仍只发生一次。 |

### 到场提示

今天到场原本有三处：旁白「你已来到X。」、横幅「已来到X。」、状态行「已来到X，可以继续聊天。」。开关开启时只保留舞台通知（旁白 / 对白框），抑制重复的横幅与状态行；无障碍播报由 `#stage-narration` / `#stage-speech` 的 `aria-live` 发出，仍恰好一次。开关关闭时三处保持原样。

### 实测

- **体积（eager，开关关闭也随包下发）**：相对 P2，`snow-adv.css` 约 **+2.4 KB（gzip）**、`app.js` 约 **+3.1 KB（gzip）**，合计约 **+5.5 KB（gzip）**。开关开启不新增网络请求（▼ 与快捷菜单用既有 `snow-icons.js` 的 `chevron-down`/`fast-forward`/`history`/`eye-off`/`settings` 图标，无需重建图标模块）。
- **立绘中轴**：headless Chromium、1280×820、观景区场景下，立绘身体中轴（测得 anchor≈0.597）落在舞台中线 ±4px 内；换表情沿用缓存值不横跳。
- **CSP**：无内联 `<style>` 元素、无外链 URL；▼ 与快捷菜单图标用 DOM API 生成（CSP 安全）。
- 不改 public v1 / `public-state-2` / IndexedDB v4 / 请求负载 / 快照 / 回复计时，不新增模型调用。

## P4 演出与转场（本阶段）

把面对面舞台做成有「演出感」的 galgame 画面，仅在 `html[data-ui="snow-adv"]` 下生效。开关关闭时行为、DOM 与时序逐字节不变（本阶段只改既有节点的表现与 `transform`/`opacity` 动画，不新增任何可见元素，脚本只在开关开启时读 `data-ui`）。所有动画只动 `transform` 与 `opacity`，档位由 `app.js` 维护的 `data-motion-level` 驱动。

| 文件 | 内容 |
|---|---|
| `public_frontend/app.js` | ① 换表情无透明空档：`crossfadeStageCharacterArt()` 在开关开启时让外层旧图全程 `opacity:1`，仅内层新图（`data-stage-art-transition="incoming"`，z-index 更高）从 0 淡入到 1，淡入完成后再移除旧图——任意时刻可见画面都不透明，绝不会两层同时半透明；关闭开关沿用旧的交叉淡出。② 入场：立绘首次为某场景/到场出现（上一帧无图/隐藏且非换表情）时，`playStageEnter()` 以 `--ease-settle` 做 ~420ms 的上浮(≈12px→0)+淡入；`light` 仅淡入；`off`/减少动态/页面隐藏不做。纯视觉，调用方不 `await`，不阻塞到场/回复逻辑。③ 动作：`stageMotionFrames()` 在开关开启时返回带缓冲、小振幅、会 settle 的 `lean_in`/`tremble`/`recoil`/`startle`（曲线取自样稿 `--ease-snow`/`--ease-settle`），替代旧的 ±10px 生硬抖动；动作名、触发、去重键完全不变；`light` 档位不做任何位移/缩放（标记 `data-stage-motion="suppressed"`），`off`/减少动态同既有抑制。新增 `snowAdvActive()`（以 `<html data-ui>` 为唯一真源）与 `stageMotionTier()`（auto/light/off）两个辅助。④ 独立白雪转场遮罩：生产环境 `motion_v2`/`scene_actions_v2` 开启时旧的三个转场层根本不显示，故在 `renderScene()` 中新增 `maybePlaySnowVeil(scene, visualKey)`——仅在开关开启、面对面可见、已布防（`armSnowVeilSoon()` 用一次 `requestAnimationFrame` 把布防推到下一帧，排除首帧/历史恢复的同步突发）、且 `scene.character_location` 相对上次确实变化时，在独立覆盖层 `#snow-scene-veil`（`syncSnowVeilMount()` 仅在开关开启时建、`aria-hidden`、`pointer-events:none`）上播放：快速磨砂白雪淡入 → 在掩护下才调用 `snowWorld.setScene(visualKey)`（仅延迟 P2 世界的视觉切换，不延迟任何逻辑/请求/回复计时）→ 显示地点标题卡（标题=`character_location`、副标题=`character_activity`）→ 保持 ~600ms → 淡出。初始加载/历史恢复、重开通讯器回到同一地点、地点未变均不触发；`auto`=淡入+标题轻微上浮，`light`=纯短淡入，`off`/减少动态=完全不播放。调用方从不 `await`，地点仍仅由既有 `aria-live` 播报一次（本层不参与无障碍播报）。 |
| `public_frontend/snow-adv.css` | 追加 P4 段：① 呼吸——`auto` 且页面可见时，`.stage-character-art[data-actor-anchor]` 以 `@keyframes snow-actor-breath` 做 1→1.008 的极轻缩放，`transform-origin` 落在身体中轴底部 `calc(100% * var(--actor-anchor-x))`（用 `transform`，不与 P3 的 `translate` 中轴定位冲突）；`light`/`off`/减少动态/`data-page-hidden="true"` 时 `animation:none`。② 白雪转场遮罩——把既有的脚印加载卡（`#scene-transition-layer .transition-card`、`#presence-arrival-loading`、`#mode-transition-layer`）重做成磨砂白雪淡入 + 居中标题卡：标题（地点名）用霞鹜文楷故事字体加细线描边，副标题为既有 detail 文案；隐藏脚印 `.transition-footsteps` 与加载圆点 `.transition-dots`。复用同一批 DOM、`data-phase`、`aria-live` 文案与时序钩子，纯表现改动，不延迟/重排任何逻辑、请求或回复计时。`off`/减少动态时这些层无任何 `transition`/`animation` 时长（瞬时显隐）。③ 独立白雪转场覆盖层 `.snow-scene-veil`（`idle` 态 `opacity:0`、`pointer-events:none`）与地点标题卡 `.snow-scene-veil-title`——霞鹜文楷、桌面 `--fs-32`(32px)/移动 `--fs-24`(24px)、带字距与两侧冰蓝细线（`::before`/`::after`），`.snow-scene-veil-kicker`/`.snow-scene-veil-sub` ≥ 12px；`off`/减少动态时无任何 `transition`/`animation`。新增字号令牌 `--fs-28`/`--fs-32`（旧的脚印层标题也改用故事字体放大）。每条选择器都限定在 `html[data-ui="snow-adv"]` 下；关键帧 `snow-actor-breath` 只被开关开启的呼吸规则引用。 |

### 动态档位（P4）

读 `document.documentElement.dataset.motionLevel`（由 `syncMotionState()` 维护，`off` 覆盖 `prefers-reduced-motion`）：

- `auto`：换表情叠加淡入、入场上浮+淡入、呼吸、带缓冲的小振幅动作、白雪遮罩淡入+标题轻微上浮全部开启。
- `light`：仅淡入淡出——换表情仍只淡入内层（不出现透明空档）、入场仅淡入、无呼吸、无位移/缩放动作、遮罩为纯短淡入。
- `off`（含减少动态）：无动画——瞬时换帧、无呼吸、无动作、完全不播放白雪遮罩。

### 生产态白雪遮罩（本阶段修订）

评审发现：生产环境 `motion_v2=true`、`scene_actions_v2=true`，旧的三个转场层（`#scene-transition-layer`/`#presence-arrival-loading`/`#mode-transition-layer`）在该配置下根本不显示，用户看不到白雪转场。修订用一个**独立的、仅装饰的**覆盖层 `#snow-scene-veil` 承载地点转场，在面对面地点真正变化时播放（见上 app.js ④）。旧三层的雪境重做样式保留（兼容 `motion_v2` 关闭的旧路径），不冲突。触发矩阵：从文字进入已知地点 / 面对面可见时地点变化 → 播放；初始加载·历史恢复 / 重开通讯器回到同一地点 / 地点未变 / `off`·减少动态 → 不播放。

### 实测

- **换表情无透明空档**：headless Chromium、1280×820、开关开启，`updateStageCharacterArt(neutral→happy)` 过程中逐帧采样：外层旧图 `opacity` 全程 ≥ 0.95，两层从不同时半透明；内层从 < 0.5 升到 ≥ 0.99；settle 后舞台仅剩 1 个立绘节点。开关关闭时旧图照常淡出到 < 0.5（旧行为不变）。
- **呼吸**：`auto` 下 `#stage-character-art` 的 `animation-name` 含 `snow-actor-breath`；`light`/`off` 下为 `none`。
- **生产态白雪遮罩**（`motion_v2`/`scene_actions_v2` 开启，复用 `test_dialogue_v2_frontend.py` 夹具）：进入面对面（观景区）后 `#snow-scene-veil` 升起、`opacity > 0.5`、标题「观景区」以霞鹜文楷 ≥ 28px 显示、`aria-hidden="true"`、`pointer-events:none`，随后淡回 `idle`（`opacity ≈ 0`）；重开通讯器回到同一地点不重播；`off`/减少动态下从不升起；开关关闭时无 `#snow-scene-veil` 元素。
- **体积（eager，开关关闭也随包下发）**：相对 P3，`snow-adv.css` 约 **+1.8 KB（gzip）**、`app.js` 约 **+3.6 KB（gzip）**，合计约 **+5.5 KB（gzip）**。开关开启不新增网络请求（白雪遮罩复用既有 DOM 与现有令牌/字体，无新图标/字体/模块）。
- **CSP**：无内联 `<style>` 元素、无外链 URL；仅用 `transform`/`opacity` 动画。
- 不改 public v1 / `public-state-2` / IndexedDB v4 / 请求负载 / 快照 / 回复计时，不新增模型调用。证据（录像 .webm/.gif 与定格 .png，桌面 1280×820 + 移动 390×844）见 `.tmp/snow-adv-p4/`。

## P5 通讯器与其余界面（本阶段）

把面对面舞台以外的所有界面重做成雪境皮肤：文字通讯（联系人卡片、磨砂设备感、角色色气泡、空会话问候卡）、顶栏（文字通讯头部与面对面 HUD）、输入区、各弹窗与抽屉、新手引导、提示与系统横幅，以及隐私页。仅在 `html[data-ui="snow-adv"]` 下生效；开关关闭时行为与 DOM 逐字节不变——本阶段只改既有节点的表现，JS 新增的属性/类都以 `snowAdvActive()`（读 `<html data-ui>`）为前提，关闭态不写出。动画只动 `transform`/`opacity`，档位由 `app.js` 维护的 `[data-motion-level]` 驱动。

| 文件 | 内容 |
|---|---|
| `public_frontend/snow-adv.css` | 追加 P5 段：① 联系人卡片——`.character` 做成卡片，圆形头像带角色色描边环（`box-shadow` 双环），选中态左侧 `inset` 角色色条；置顶星用暖色高亮；每行的角色色由 `renderCharacters()` 写的 `data-snow-character` 经 P1 的 `[data-snow-character]` 规则派生 `--char`。② 消息气泡——助手气泡白色磨砂 + 角色色细左描边与圆角尾巴，用户气泡冰蓝；动作/表情/用量 chip 协调；文字通讯打字气泡随新色板。③ 气泡 pop-in——`.message.is-snow-pop` 由关键帧 `snow-bubble-pop`（`auto`：上浮 6px + 淡入 ~220ms）/ `snow-bubble-fade`（`light`：仅淡入）驱动，`off`/减少动态无动画。④ 空会话问候卡 `.snow-empty-greeting`——角色色头像首字 + 原文案，取代纯灰文字。⑤ 顶栏——文字通讯头部、当前约定条、新回复气泡、约定卡随新色板；面对面 HUD 工具/地点随新色板，`≥44px` 触控目标不缩小。⑥ 输入区——胶囊输入框、工具/发送按钮与 P1 一致；`#message-input::placeholder` 单行省略，修复移动端占位文案换行被裁切。⑦ 弹窗/抽屉/引导/提示/横幅——设置、公告与生日、去见她、一起去、反馈、信息与回看抽屉、新手引导、系统横幅统一半径、磨砂、字体与角色色。⑧ 隐私页——`.privacy-page` 在开关开启时皮肤化（标题用霞鹜文楷、卡片磨砂圆角），字体与图标署名行 `.snow-adv-attribution` 仅开关开启时显示。⑨ 移动端（≤560px）——放开 HUD 角色名的 `max-width:150px` 与省略号，让 3–4 字名字正常显示（修复「凯…」截断）；输入占位保持单行。⑩ `≥12px` 兜底覆盖 P5 新增的小字节点。每条选择器都限定在 `html[data-ui="snow-adv"]` 下（测试强制）；关键帧 `snow-bubble-pop`/`snow-bubble-fade` 只被开关开启的规则引用。 |
| `public_frontend/app.js` | `renderCharacters()`：开关开启时给每个 `.character-row` 写 `data-snow-character`（关闭态不写，DOM 逐字节不变）。`renderTimeline()`：空会话在开关开启且已选角色时用 `.snow-empty-greeting`（角色色头像首字 + 原文案）渲染，否则沿用原 `<p>`；新消息行在开关开启时加 `is-snow-pop` 类交给 CSS 按档位做 pop-in（关闭态沿用既有 `transientMotion` WAAPI 行为）。既有 `#chat-app` 上的 `data-snow-character`（P1，选中角色时写）为整个聊天面板派生 `--char`，助手气泡/问候卡/约定卡/头像均据此取色。 |
| `public_frontend/privacy/index.html` | 链接 `/snow-adv.css?v=1.0.0`；在「许可边界」区加一条默认 `hidden` 的字体与图标署名 `<p class="snow-adv-attribution">`（霞鹜文楷 SIL OFL 1.1；Lucide 图标 ISC）。 |
| `public_frontend/privacy/privacy.js` | 读 `/public/v1/config` 后，`features.ui_snow_adv` 为真时给 `<html>` 加 `data-ui="snow-adv"` 并移除署名行的 `hidden`；为假时清除（因 `app.css` 的 `[hidden]{display:none !important}`，可见性由 `hidden` 属性而非 CSS `display` 控制）。 |

### 动态档位（P5）

- `auto`：气泡 pop-in 上浮 + 淡入、空会话问候卡轻微入场。
- `light`：气泡仅淡入（无位移），问候卡无入场动画。
- `off`（含减少动态）：无任何气泡/问候卡动画。

### 实测

- **联系人卡片 per-row 角色色**：headless Chromium、开关开启，`.character-row[data-snow-character]` 各自解析出不同的 `--char`（≥2 行颜色不同）；关闭态无该属性。
- **气泡 pop-in**：`auto` 下 `.message.is-snow-pop` 的 `animation-name` 含 `snow-bubble-pop`，`off` 下为 `none`。
- **移动端（390×844）**：面对面占位「说些什么，也可只填写动作……」在单行输入框内不溢出（`scrollHeight ≤ clientHeight + 2`）；HUD 角色名（3–4 字，夹具角色「凯茜娅」）完整显示不截断（`scrollWidth ≤ clientWidth + 1`）。
- **最小字号**：文字通讯界面、联系人栏、设置弹窗、公告弹窗中所有可见文字 `≥ 12px`。
- **隐私页**：开关开启时挂 `data-ui="snow-adv"`、标题用霞鹜文楷、署名行可见；关闭时不挂皮肤、署名行隐藏。
- **体积（eager，开关关闭也随包下发）**：相对 P4，`snow-adv.css` 约 **+3.6 KB（gzip）**、`app.js` 约 **+0.5 KB（gzip）**，合计约 **+4.2 KB（gzip）**（起始工作树 107,007 B → 111,255 B）。开关开启不新增网络请求（复用 P1 的字体/图标/令牌，无新模块）。隐私页新增一处 `/snow-adv.css` 引用（该资源已在清单与指纹化中）。
- **CSP**：无内联 `<style>` 元素、无外链 URL（隐私页署名行的 `<a>` 为可点击链接，非被动获取资源）；仅用 `transform`/`opacity` 动画。
- 不改 public v1 / `public-state-2` / IndexedDB v4 / 请求负载 / 快照 / 回复计时，不新增模型调用。证据（桌面 1280×820 + 移动 390×844 定格 .png）见 `.tmp/snow-adv-p5/`。

## 资源来源与重新生成

- 字体：npm `lxgw-wenkai-screen-webfont@1.7.0`（包 MIT，字体 SIL OFL 1.1），使用其中的 `lxgwwenkaigbscreen.css` 分片。字体文件逐字节复制，未修改字形；只从 `unicode-range` 中去掉 emoji 与图形符号区段（U+200D、U+2600–27BF、U+2B00–2BFF、U+FE00–FE0F、U+1F000–1FAFF、U+E0000–E007F），让 emoji 继续使用系统彩色字体，并丢弃只含这些字符的 2 个分片。文件名带内容哈希前 16 位，沿用 Caddy 现有的一年 immutable 缓存规则。

  ```
  npm install --prefix <临时目录> lxgw-wenkai-screen-webfont@1.7.0
  python App/scripts/build_snow_adv_fonts.py --package <临时目录>/node_modules/lxgw-wenkai-screen-webfont
  ```

- 图标：仓库内 `frontend/vendor/lucide.min.js`（lucide 1.28.0，ISC）。公开镜像不包含 `frontend/vendor/`，所以生成一个只含所需图标的模块：

  ```
  node App/scripts/build_snow_adv_icons.mjs          # 生成
  node App/scripts/build_snow_adv_icons.mjs --check  # 测试使用
  ```

- 放量时（P5/P6），隐私与素材页应补充霞鹜文楷（OFL 1.1）与 lucide（ISC）的署名。P5 已在隐私页加入该署名行（`.snow-adv-attribution`），仅在 `ui_snow_adv` 开启时显示。

## 验证

`tests/test_snow_adv_ui.py`：

- 开关默认关闭、只认 `true`，`/config` 如实下发；部署白名单、候选覆盖文件与验收回执见 `tests/test_snow_adv_deployment.py`。
- `snow-adv.css` 每条规则都限定在 `html[data-ui="snow-adv"]` 下；`app.css` 中所有低于 12px 的选择器都被覆盖。
- 字体文件名哈希、`SOURCE.json`、OFL 许可、无 emoji 区段；图标模块与生成脚本一致；生产指纹化后 HTML 和 `app.js` 引用的是带哈希的地址。
- 浏览器层（`RUN_PUBLIC_E2E=1`）：关闭时界面字符与资源请求不变；开启时令牌、图标、字体与最小字号生效，无页面错误、无失败请求。


## P6 开场与通讯器世界层

已确认的 `auto` 开场按首次加载时的视口选择：宽于 860px 使用桌面开机，手机使用锁屏。改变窗口大小不会中途切换开场方式。新节点仅在 `ui_snow_adv` 开启时创建。

### 开场顺序

- 桌面开机显示小吉终端图标与真实加载阶段，就绪后淡出，不添加人为加载等待。同一日历日再次打开跳过开机画面。
- 手机锁屏显示本地时钟、日期和时段。通知卡只使用已有待赴约定和未读公告；点击后调用既有处理，赴约仍需确认。点击空白或按 Enter/Space/Escape 解锁，同一浏览器标签会话再次打开跳过锁屏。
- 开机或锁屏期间推迟体验须知、新手引导和自动公告；消隐或解锁后恢复原顺序。
- 等待配置时只显示品牌底色（P7 起对首访与返回用户、开关开或关都生效，见下文 P7），配置失败立即恢复显示。

### 雪景与首次入室

桌面文字通讯的背景使用独立的 body 级 `#snow-backdrop`，不挂在隐藏的面对面区域内。联系人栏、消息区和输入区使用半透明磨砂底色，消息气泡保留可读的底色。初次亲身入室前背景固定为基地外；进入室内后跟随场景。面对面仍使用 `#scene-stage` 内的舞台世界，同一时间最多一个雪花动画循环。

首次由外景进入具体室内地点时，完整动效播放入口镜头推进，再显示目的地白雪标题卡。后续移动只播放地点转场，重开通讯器返回同一地点不重播。展示动画不等待网络、不发消息、不增加模型调用、不改变到场和回复计时。

`light` 只保留淡入淡出，`off` 或系统减少动态时无推进和持续雪花动画。运行时修改偏好立即作用于背景和舞台；页面隐藏时暂停动画。

### 回归与证据

`tests/test_snow_adv_ui.py` 使用合成浏览器夹具覆盖开关启闭、手机锁屏与通知、体验须知顺序、配置失败恢复、可见雪景、真实到场入口、目的地标题与动效档位。`tests/test_dialogue_v2_frontend.py` 保留已有生产功能组合的场景回归。部署环境与回执绑定由 `tests/test_snow_adv_deployment.py` 验证。

测试截图和录屏保存于本次工作区 `.tmp/`；它们属于自动化验证，不表示人工批准。发布还需精确候选 SHA 的公告预检、CI、候选运行身份和回退检查，以及对应回执。

不修改 public v1、`public-state-2`、IndexedDB v4、请求负载、快照或模型调用逻辑；公开语音继续关闭。


## P7 上线后修整

对已上线的 `d69449b` 做桌面 1440×900 与手机 390×844 实拍审查（不配置模型、不发消息）后的修整，只改表现层。

### 开场

- 首帧：`index.html` 静态带 `data-snow-hold`，`app.css` 据此在配置解析前隐藏 `.app-shell` 与统计提示，只显示与开机层相同的底色和小吉终端图标。配置解析或失败即移除；配置迟迟不来最多保持 2.5 秒，脚本完全没有运行时 4 秒后由 CSS 动画兜底显示。线上首访原先会先露出 0.5–0.9 秒未换肤的旧界面。
- 桌面开机退场分两段：标志先收起，底色随后淡开且不再拦截点击；通讯器的侧栏、顶栏、消息区和输入区依次入场（仅完整动效档）。入场结束（约 720ms）后才移除开机节点并弹出体验须知，`#snow-boot` 被移除即表示开场完成。`light` 只做 200ms 淡出，`off` 立即移除。
- 手机锁屏改为雪白底色和静态雪丘，没有通知时显示「没有新的通知」。

### 面对面

- 舞台铺满面对面区域，顶栏改为透明底上的浮动磨砂小按钮；立绘高度扣除顶栏占位（`--snow-hud-clear`）。
- 旁白移入对白框（开关关闭时放回舞台原位）；回看、隐藏只保留在对白框快捷菜单里。
- 场景提示条改为顶栏下方居中的小提示，镜头推进和白雪转场期间不显示。
- 休息区两侧的绿色椭圆改为盆栽；食堂出餐台、训练区器械与沙袋调浅。

### 通讯器与弹窗

- 外景只加轻雾，不再整体发灰；generic 外景水平镜像，使大楼入口位于右侧、不被联系人栏遮住，首次入室的镜头推进仍对准入口。
- 空会话的开场建议紧贴欢迎卡；输入区工具按钮统一为 40px 浅色胶囊。
- 体验须知复选框紧贴文字；公告面板内部滚动，「知道了」固定在底部、处于首屏内。
- 任一模态弹窗打开时，新手引导和统计提示暂时隐藏，关闭后恢复。

### 体积与回归

首屏文本 gzip（`index.html`、本仓库样式和 `app.js` 静态导入闭包，按 LF 计）相对 `a1354cc` 增加约 57.7KB，在 60KB 预算内；`d69449b` 为 52.7KB。

`tests/test_snow_adv_ui.py::SnowAdvPolishTests` 覆盖首帧保持（开关开/关、配置失败）、开机交接顺序、全屏舞台与旁白位置、开关关闭时的旧布局、弹窗与引导叠放、公告按钮位置、空会话间距、外景镜像和锁屏底色。不修改 public v1、`public-state-2`、IndexedDB v4、请求负载、快照、模型调用或回复计时。
