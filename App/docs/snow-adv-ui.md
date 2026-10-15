# 雪境 ADV 界面（`ui_snow_adv`）

雪境 ADV 是公开前端的新界面皮肤：雪白底、冰蓝强调、每位角色一个主题色，对白与名牌使用霞鹜文楷屏幕阅读版。它按阶段交付，每阶段一个 PR，全部放在功能开关 `ui_snow_adv` 后面。

## 开关

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `PUBLIC_UI_SNOW_ADV` | `false` | 只接受精确的 `true`（不区分大小写）。开启后 `/public/v1/config` 的 `features.ui_snow_adv` 为 `true`，前端给 `<html>` 加 `data-ui="snow-adv"`。 |

- 纯展示开关：不改 public v1、`public-state-2`、IndexedDB v4、请求哈希、快照，也不增加模型调用。
- 开关关闭时，`snow-adv.css` 的所有规则都匹配不到，图标字符与字体都不替换，旧界面不受影响。
- `ops/deploy.sh` 的公开环境白名单**尚未**接受 `PUBLIC_UI_SNOW_ADV`，候选验收也未记录它。所以生产环境目前无法开启；放量前需要单独提交、审查部署白名单与验收改动，并按常规取得该候选的回执批准。`tests/test_snow_adv_ui.py` 会在白名单接入时提醒同步更新本节。
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

- 放量时（P5/P6），隐私与素材页应补充霞鹜文楷（OFL 1.1）与 lucide（ISC）的署名。

## 验证

`tests/test_snow_adv_ui.py`：

- 开关默认关闭、只认 `true`，`/config` 如实下发，部署白名单尚未接入。
- `snow-adv.css` 每条规则都限定在 `html[data-ui="snow-adv"]` 下；`app.css` 中所有低于 12px 的选择器都被覆盖。
- 字体文件名哈希、`SOURCE.json`、OFL 许可、无 emoji 区段；图标模块与生成脚本一致；生产指纹化后 HTML 和 `app.js` 引用的是带哈希的地址。
- 浏览器层（`RUN_PUBLIC_E2E=1`）：关闭时界面字符与资源请求不变；开启时令牌、图标、字体与最小字号生效，无页面错误、无失败请求。
