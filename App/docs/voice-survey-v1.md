# 功能调研（voice-survey-1）

用来判断要不要做角色语音、「1 比 1 付费」有没有人愿意用，以及要不要做模型托管和多端同步（不用自带 API，由站点提供多厂商模型和思考深度，聊天记录托管在服务器上，为打包 App 和桌面应用做准备）。这是调研，不代表这些功能会上线；公网语音仍然关闭，字段名和开关沿用 voice_survey。

## 题目与要回答的决策问题

文案在 `public_frontend/voice-survey.json`，改字不用动代码；选项的 `value` 是接口约定，不要改。

| 字段 | 题目 | 要回答的决策问题 |
| --- | --- | --- |
| `use_voice`（必答） | 会不会用语音 | 这个功能有多少人需要 |
| `use_scenes` | 单聊、群聊、多人聚会（多选） | 先做哪个场景 |
| `pass_through_acceptance`（必答） | 「1 比 1 付费」接受度 1–5 | 付费方案有没有基础 |
| `payment_method`（必答） | 网站充值、自带密钥、只用免费 | 需不需要做站内余额 |
| `monthly_budget`（必答） | 0、1～5、6～20、21～50、50 元以上 | 收入能否覆盖前置成本 |
| `priorities` | 价格、像不像、延迟、隐私、费用透明（多选） | 取舍时优先保什么 |
| `characters` | 最想先有声音的角色（多选） | 先做哪些角色 |
| `hosted_interest`（必答） | 不自带 API、由站点提供模型，想不想用 | 托管方案有没有需求 |
| `hosted_models` | 不同厂商、思考深度、默认配好（多选） | 模型选择要做多细 |
| `hosted_budget`（必答） | 0、1～10、11～30、31～60、60 元以上 | 托管收入能否覆盖模型和存储成本 |
| `history_hosting`（必答） | 可以接受、能导出和删除才接受、加密到运营者看不到才接受、只存本机 | 聊天记录托管的隐私方案 |
| `sync_importance`（必答） | 多端同步的重要程度 1–5 | 账号体系和同步值不值得先做 |
| `app_platforms` | 安卓、iPhone、Windows、Mac、网页就够（多选） | 先打包哪个平台 |
| `adult`（可不答） | 是否已满 18 岁 | 未成年人付费问题有多大 |
| `comment`（可不填） | 其他建议，最多 200 字 | 没想到的问题 |

## 文案约束

- 「1 比 1」写具体：按厂商实际单价收费，费用全部用于用户自己的语音请求，网站不赚差价；算例按 `qwen-audio-3.1-tts-plus` 1.4 元/万计费字符、汉字算 2 个字符，一句 50 字约 1.4 分钱。
- 写明支付手续费和失败请求的处理方式还没定。
- 写明这是调研，不代表一定上线；贴近原角色的声线需要授权，不一定能做到。
- 托管方向写明需要注册账号并付费、方案和价格未定，并在题目里写明聊天记录托管后运营者在技术上可以接触到内容。

## 展示规则

- 开关 `PUBLIC_VOICE_SURVEY_V1`：应用默认关闭；`deploy.sh` 生成生产环境时默认开启（2026-11-08 小B要求直接上线），在 `/etc/project-snow/public.env` 写 `false` 即可撤下。
- 开关关闭时不显示入口，不加载模块，也不请求接口。
- 入口是公告「更新公告」页顶部的「功能调研」卡片，不弹窗、不打断聊天。可以「去填写」、关掉公告稍后再填，或「不再提示」。
- `modules/voice-survey.js` 和文案只在点「去填写」时加载，加载失败只提示一条消息，不影响启动和聊天。
- 答完或选择不再提示的记录放在 `localStorage`（`project-snow-public:voice-survey:v1`），IndexedDB v4 不动。

## 隐私与保留

- 只保存答案和可选建议，主键是匿名 Cookie 的单向哈希，同一主体再次提交是更新，不新增一行。
- 不保存原始 IP 和 User-Agent；建议内容不进日志，提交前过一遍密钥脱敏，不发给模型或第三方。
- 自最后一次提交起保存 180 天，到期由 `PublicStore.cleanup` 删除。隐私页同步说明，隐私版本升到 1.2.0。

## 接口

只增不改 public v1：

- `GET /public/v1/voice-survey`：`{survey_version, answered}`。
- `POST /public/v1/voice-survey`：提交答案，匿名主体每小时 10 次、每天 20 次，每个 IP 指纹每小时 30 次、每天 100 次。
- 响应 `no-store`。开关关闭时两个接口都返回 404。

数据库只新增表 `public_voice_survey_response`（迁移 `20261108_0011`），旧版代码不读这张表，回滚不需要改库。真实 PostgreSQL 的并发、清理和 old/new/old 验证在 `tests/test_voice_survey_postgres.py`。

## 汇总

在能连接数据库的环境运行，只输出计数和比例，不输出建议原文和主体：

```powershell
python scripts/voice_survey_report.py --database-url "$env:PUBLIC_DATABASE_URL"
```
