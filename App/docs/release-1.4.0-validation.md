# 小吉终端 1.4.0 发布验证

## 维护者决定

2026-11-23，维护者要求先按旧角色规则发布，保留 22 名角色的审阅材料，真实模型的付费对比另行安排。OpenAI 官方端点的兼容修复默认开启。人工质量结论尚待对比实验。

## 首次配置与模型请求

维护者报告，在尚未选择型号时填写 OpenAI Key 并点击保存就收到拒绝。具体账号的上游响应未取得。本版以模拟端点和真实 Chrome 分别验证首次配置、被拒 Key 的替换、获取账号模型、选择后保存，以及公共对话、摘要、主动消息和私有工作台的请求形状。仅填 Key 时发起模型发现，选定型号后才保存模型配置。

公共 v1 新增 `provider_quota_exhausted`、`provider_model_unavailable`、`provider_parameter_rejected`、`provider_unavailable` 和 `provider_region_unavailable`。接口继续提供既有的凭证拒绝、频率限制、超时等错误码。模型发现拒绝和聊天生成分别处理。结构化参数校验失败可在原调用预算内修正一次。旧前端对未知错误码使用通用错误提示。

## 语料与风格实验

发布包 `2026.11.23.1` 继承 `2026.08.19.1` 的许可、署名、资料、图谱与向量。构建将 Wiki 原文件逐项对照已审阅路径和哈希，并应用排除清单。采用父包的实体图排除人名、地名与组织名。共计 978 个原作页面、22 名角色、22,304 条去重台词和 6,162 个相邻对话对。按页面留出 1,425 对，训练部分中有 4,319 对可检索。原始 Wiki 文件和审阅人的私有工作目录不进入 Git。

包内保存 825 个待审标志性用语、4 个待审高强度表达、741 个待审共同经历话题，以及一条带原作出处的迁移情境提示。运行时读取批准行，迁移提示保留单独的既有规则标识。完整语料、出现率、页内位置和索引清单在不可变数据包的 `style/1.4.0`。用语表和守卫的合成夹具覆盖有批准行、待审行和空结果三种情况。数据包 manifest 的 SHA-256 为 `a081b62590be8dabaa7db0e0e5c0bdfe24c24398799a8b26088299fdd7db58d7`。

| 开关 | 发布值 | 实验值 |
| --- | --- | --- |
| PUBLIC_OPENAI_PARAMETER_ADAPTER | on | off |
| PROJECT_SNOW_STYLE_EXAMPLES | static | retrieved |
| PROJECT_SNOW_STYLE_SIGNATURE_GUARD、SIGNATURE_PROMPT（同一前缀） | bubu_legacy | data_driven、off |
| PROJECT_SNOW_STYLE_REGISTER_GUARD、REGISTER_PROMPT（同一前缀） | fenny_legacy | data_driven、off |
| PROJECT_SNOW_STYLE_SHARED_FACTS | futiya_legacy | data_driven、off |
| PROJECT_SNOW_STYLE_SITUATIONAL_CUES | futiya_legacy | reviewed_table、off |
| PROJECT_SNOW_STYLE_FALLBACK | legacy | minimal_repair |
| PROJECT_SNOW_STYLE_COPY | measure | rewrite |

`PROJECT_SNOW_STYLE_WINDOW=6`、`COEFFICIENT=1.0`、`EXAMPLE_COUNT=4`、`KEEP_REPEATED_LINES=true` 均使用 `PROJECT_SNOW_STYLE_` 前缀。系数有待付费实验校准。每个候选 SHA 的 flags 文件可分别覆盖这些值，生成的环境快照随回退版本保留。

从 `App` 运行以下命令可重建审阅材料，并用同一模型比较三组条件。每次使用独立的空审计目录。默认执行 dry-run，只使用模拟响应。执行真实审计需要维护者另外选择模型、提供环境凭证并显式使用 `--execute`。发生不确定的付费结果时检查该目录的尝试记录，先对账。

```text
python -m pipelines.style_corpus --data-root <Data> --runtime <输出目录> --graph <父包/graph/nodes.jsonl> --public-reviewed
python -m pipelines.style_review --style <语料目录> --out <审阅目录>
python -m pipelines.style_audit --config current --model <型号> --runtime <验证数据包> --style <语料目录> --out <审计目录/current>
python -m pipelines.style_audit --config candidate-style --model <同一型号> --runtime <验证数据包> --style <语料目录> --out <审计目录/style>
python -m pipelines.style_audit --config candidate-all --model <同一型号> --runtime <验证数据包> --style <语料目录> --out <审计目录/all>
python -m pipelines.style_compare <审计目录/current> <审计目录/style> <审计目录/all> --out <对比目录>
```

## 夜间验证

GitHub Actions 的 43332018990、44431034813 和 44962321952 三次运行均记录同一断言在固定延迟后读到进场动画首帧。最后一次还记录了移动端反向转场为 433.3 毫秒。测试改为在变换生效的同一帧采集镜头状态，并通过 DOM 变更回调测量快照生命周期。双向转场 300–430 毫秒、单一可交互表面、零长任务和草稿恢复断言继续执行。修复在 PR #124 的 Chrome、full-validation 和 gate 作业通过。另一次 44214094131 失败于桌面依赖审计，该检查在 #124 通过。

## 候选与回退

本版沿用 public v1、public-state-2、IndexedDB v4 和原请求快照，无数据库迁移。新数据包独立存放；预装只读核对原服务索引，索引身份与完整输入不符会中止。生产发布身份、浏览器冒烟、数据容量、旧版回退锚点及公告校验以实际候选回执为准。晋级后由维护者从生产域名验证本人账号的 OpenAI 赠送额度。

## 已完成的验收记录

1.3.1 前置补丁已从原暂存区复制并按二进制 diff 核对，PR #125 通过 CI 后合并；1.4.0 在合并提交 `3ba70794` 上重放。恒约名单兜底按已确认名单的顺序生成，用替换名单和空名单覆盖更新场景。

在 1.3.0 线上 API 容器的正常出口，对官方模型列表发送无密钥 GET，得到 HTTP 403、`unsupported_country_region_territory`。这是线上服务地区限制的直接证据；当事人当时的上游响应未保存。候选验收应复核此项，报告须注明官方 OpenAI 服务在该出口的实际可用性。

在实际发布包上以六名角色、每组 24 轮 dry-run 验证三组开关。估算输入依次为 511,786、500,103、499,611 token，外部请求均为零。另一次 22 角色工程估算采用本地语料和模拟回复，每组 704 轮，三组依次为 14,802,407、14,456,044、14,452,108 输入 token。以上为提示预算；真实分布指标与费用待另行执行付费对比。
