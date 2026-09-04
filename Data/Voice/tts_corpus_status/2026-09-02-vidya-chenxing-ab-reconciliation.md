# 薇蒂雅 / 辰星 A/B 语料对账状态

记录时间：2026-09-02T13:10:41.0671019+08:00

## 结论

2026-09-02 生成的 15 段试听语料路由与 2026-08-31 的本地长对话语料分析服务于不同目的，不能把前者的槽位缺口解释为角色整体仍需补录。

- 15 段试听包保留为精确台词、角色风格和副语言事件语料：12 段词汇候选、2 段副语言候选、1 段重复排除。
- 仅在该 15 段包内部，薇蒂雅 A 和辰星 B 未达到单槽 10 秒，因此该包不能独立组成两名角色的完整 A/B。
- 更新的本地长对话分析已经为薇蒂雅和辰星各找到一对 10–20 秒、候选/音频/文本/时间范围互斥且信号 QC 全通过的 A/B 主候选。
- 这四个主候选仍在等待人工比较自然版与安全静音压缩版。自动 QC 通过不等于人工批准，更不等于训练、克隆、Provider 注册、权利接受或发布批准。

## 13:25 人工试听后续

上面的“等待人工比较”状态已由后续回执取代。用户明确提交：

> 四个槽位按推荐方案通过：薇蒂雅 A 压缩版、B 通过；辰星 A/B 压缩版。

- 人工回执：`voice-target-ab-review-9afda082d22702b0adb7`
- receipt SHA-256：`8a319737777c49c134cb6cbbba0c3f4cf04191db2eb22bc75fd1054a2f64a4f5`
- decision set SHA-256：`51e9273197405e9a6bfebb9f4dd27528fcd45d781cb6a769fa560a32c551bc11`
- 文件 SHA-256：`40e7af00e16460d48f460290259a7f2f1bc965c065ee069b30b138344c9029ee`

四个槽位现均为 `compacted_accepted_no_issue`。薇蒂雅 B 的自然版与压缩版完全相同，回执已显式记录该事实。该决定只批准本地 A/B 候选选择，未批准训练、克隆、权利、Provider、费用、发布或公网 rollout。

## 15:28 离线 Provider 预检

已从上述人工回执生成本地、不可变的离线注册预检包：

- preflight：`voice-provider-preflight-277b384f4a1451063562`
- manifest SHA-256：`340585bf0dc35eb20a99b9a51cec3eb66431d16ab72a4353572e0a64eb30b71e`
- manifest 文件 SHA-256：`d9633af150a658183933941345524eeb265101a55a0b332dddf4c023529b383a`
- blind-test plan 文件 SHA-256：`6cae1d4b0927816968e24fc5578c22615e4743724870add23ef5babc2f5ed321`
- 状态：`await_explicit_rights_provider_cost_authorization`

预检包只引用四个已批准的压缩版样本，不复制音频；薇蒂雅 A/B、辰星 A/B 均保持为四个独立的临时注册候选，不拼接、不联合加权。盲测计划为每名角色固定 6 类同参台词，使用隐藏映射、0–5 分量表、关键失败项与平局规则。

副语言 ordinal 2/3 仍排除于基础 TTS 注册，并保持 `pending_human_event_qa`。后续只有在单独批准事件库后，才比较“基础 TTS”与“基础 TTS + 精选录音事件库”，用于还原气声、非词汇呓语等表达而不污染基础音色。

本步骤未读取凭据、未联网调用 Provider、未创建 voice ID、未产生费用。重复执行已返回 `existing_valid`，随后从源回执与四组文本/WAV 完整重建验证通过。

## 16:52 Provider 执行合同

用户同意继续任务后，已把上述预检推进为仍然离线、不可变的执行合同：

- run：`voice-provider-enrollment-run-f31ef669cfeb9af52060`
- manifest SHA-256：`1102abd8853f069018954affb54ea3f07cbef134705eda4c27501c5013760536`
- manifest 文件 SHA-256：`629b3cbcbf51ab8b610b896ee16fd08ee6f371b0e76dd62e20af3035c8258200`
- 固定注册模型：`qwen-voice-enrollment`
- 固定目标模型：`qwen3-tts-vc-realtime-2026-01-15`
- 固定区域：新加坡 `ap-southeast-1`
- 保守的四次直接创建费用上限：USD 0.04；后续合成/盲测费用不包含在内
- 状态：`offline_ready_live_provider_execution_blocked`

执行工具只允许一次创建一个候选；每次联网前先落不可变 attempt 回执。若请求结果不确定且没有 result 回执，该槽位自动禁止重试，必须先通过 Provider `list` 对账。工具同时提供逐个删除流程，要求复述精确 voice ID，并确认“删除不会返还免费配额”，避免盲测淘汰项长期保留。

执行合同仍如实记录来源为 `unverified_fanwork_source`、无书面来源授权，不把风险路线伪装成权利证明。所核对的官方资料没有给出上传源样本的精确留存期，因此实际上传还需逐槽显式接受该不确定性。

四个槽位均已完成脱敏离线请求重建：`psvdac7199bd8`、`psvdb1693d0e8`、`pscxabe7b8e90`、`pscxb654d344a`。每项重新验证了 WAV/逐字稿哈希；请求输出只含哈希占位符，不含音频、台词或 API Key。四项结果均为 `offline_inspection_complete`、`credentials_read=false`、`provider_interactions_performed=false`。

本地执行合同已完整重建验证通过，精确重放返回 `existing_valid`。Windows 进程、用户和系统三级环境中没有 `DASHSCOPE_WORKSPACE_ID` 或 `DASHSCOPE_API_KEY`；后续核对发现主项目 `App/.env` 已保存非空 `EVIDENCE_REVIEW_API_KEY`，并把 `EVIDENCE_REVIEW_BASE_URL` / `DASHSCOPE_BASE_URL` 指向中国区 `dashscope.aliyuncs.com/compatible-mode/v1`。密钥值未被输出，`.env` 为 Git 忽略的普通文件。

## 百炼现有凭据只读诊断

针对用户“不需要额外配置”的说明，已复用上述项目内百炼密钥，对中国区全局声音复刻路径执行两次 `qwen-voice-enrollment` / `action=list` / `page_size=1` 只读探测。两次请求均未携带音频、未创建或删除音色、未输出已有 voice ID，也未产生声音创建费用。

服务端能够识别请求并返回结构化 JSON，但响应为 HTTP 400 / `Arrearage`：`Access denied, please make sure your account is in good standing`。当前实际阻塞项因此不是本地 API Key 缺失，而是该百炼账户的欠费/信用状态。官方文档同时确认新加坡与北京使用不同地域 API Key；当前已有密钥及 base URL 属于中国区，而现有不可变执行合同仍固定为新加坡区，不能静默混用。

截至本记录更新：Provider 只读诊断请求 2 次；音频上传 0 次；Provider mutation 0 次；voice ID 创建/删除 0 个；创建费用 0。

## 17:47 执行合同迁移到中国（北京）区

根据用户明确要求，后续实时注册的权威执行区域已由新加坡迁移为中国（北京）`cn-beijing`。不可变合同不允许原地改写，因此旧合同 `voice-provider-enrollment-run-f31ef669cfeb9af52060` 继续保留为哈希可验证的历史审计记录，但已被标记为后续执行层面的 `superseded`，不得再用于新的实时操作。

新建的北京区不可变后继合同如下：

- run：`voice-provider-enrollment-run-955deef1ab01ba619f84`
- manifest SHA-256：`205b9cd630bb4ed149b3f336162a913357488bde67614a0d0aa2ce05fbea14e4`
- manifest 文件 SHA-256：`eb388206cae3b9bae20c53e1ab49b866daa0d93c176b98fbd8422db287bba701`
- 固定注册模型：`qwen-voice-enrollment`
- 固定目标模型：`qwen3-tts-vc-realtime-2026-01-15`
- 固定区域：中国（北京）`cn-beijing`
- 固定端点模板：`https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/customization`
- 保守的四次直接创建费用上限：USD 0.04；北京区合同不套用新加坡区免费额度假设
- 状态：`offline_ready_live_provider_execution_blocked`

四个候选 `psvdac7199bd8`、`psvdb1693d0e8`、`pscxabe7b8e90`、`pscxb654d344a` 已按北京区合同重新完成脱敏离线请求检查，均返回 `offline_inspection_complete`，并再次验证音频与逐字稿哈希。执行工具现在从不可变 run 绑定地域；北京区命令不能误用旧的新加坡上传确认参数。项目 `App/.env` 中已有的 `EVIDENCE_REVIEW_API_KEY` 只有在两个已配置 base URL 均严格匹配中国区 `dashscope.aliyuncs.com` 时才允许作为国区密钥别名被安全发现，密钥值不会写入合同、回执、日志或输出。

北京区合同已完整重建验证通过，精确重放返回 `existing_valid`。当前不需要新增本地 API Key 配置；实时执行的外部阻塞项是现有账户返回 HTTP 400 / `Arrearage`，以及本地尚未发现官方 workspace 专属端点所需的北京区 Workspace ID。两者都没有被猜测或写入不可变合同。

截至本次区域迁移完成：北京区音频上传 0 次；Provider mutation 0 次；voice ID 创建/删除 0 个；创建费用 0。此前仅有的 Provider 交互仍是两次中国区全局 `list` 只读诊断。

## 18:04 充值后账户只读复检

用户确认已充值后，复用项目内现有中国区密钥，对同一中国区全局 `qwen-voice-enrollment` / `action=list` 路径执行一次额外的只读复检。响应已从此前 HTTP 400 / `Arrearage` 恢复为 HTTP 200，包含 request ID，当前页返回音色数量为 0。密钥值未输出，请求不含音频，也没有创建、更新或删除 Provider 资源。

因此 `Arrearage` 不再是当前活动阻塞项。累计 Provider 只读诊断为 3 次；音频上传仍为 0 次；Provider mutation 仍为 0 次；voice ID 创建/删除仍为 0 个；创建费用仍为 0。下一活动阻塞项是北京区不可变合同要求的 Workspace ID 尚未绑定；百炼控制台已打开到华北 2（北京），需用户完成登录后才能只读定位业务空间 ID。

## 18:34 北京区四槽注册完成

用户随后通过控制台截图提供完整 Workspace ID，并明确确认四个候选依次创建、上传到华北 2（北京）、未验证同人来源风险、百炼条款与声音克隆同意要求、源音频留存期未明确，以及四次直接创建总费用上限 USD 0.04。Workspace ID 在本状态摘要中仅记为 `ws-…vd04`；完整值只存在于私有 Provider attempt 回执与实际请求端点中，API Key 始终未输出。

Workspace 专属 `list` 首先返回 HTTP 200，证明截图中的 ID 与唯一非空中国区密钥能够共同访问北京业务空间。项目 `.env` 同时存在无关重复项、相同地域 URL 重复项及“空密钥占位 + 唯一非空密钥”组合；执行工具已收紧为只解析四个 Provider 字段，允许无歧义的相同值或空值占位，但继续拒绝两条不同的非空 Provider 值。专项测试 14 项与修改范围 Ruff 检查通过。

四个候选随后严格逐个执行，每次先写 attempt、收到成功响应后再写 result，并在进入下一候选前通过 Provider `list` 对账：

| 槽位 | Provider 结果 | result 回执 SHA-256 |
| --- | --- | --- |
| `vidya-a` | `voice_created`，列表对账通过 | `1cb83c333ffd1d99f368bc55a01e5f17eb39ac64c6da11b154e5b4e960c28987` |
| `vidya-b` | `voice_created`，列表对账通过 | `27e873311a08028734ea9b1de2d8d0e50124254e4ce4c680ac28832b57e008b5` |
| `chenxing-a` | `voice_created`，列表对账通过 | `45d5bd4d78e89d3e4de1b8b845ed02f0a1cb172c92ca129d250697feddd394fb` |
| `chenxing-b` | `voice_created`，列表对账通过 | `817123462cf04d671cafa5c129e452f5861f284ff007f963f4119c881e2e2485` |

最终审计状态为 4 个 attempt、4 个 result、0 个悬空 attempt；四个 Provider voice ID 相互唯一，并全部存在于北京 Workspace 的只读列表中。累计 Provider 交互为 10 次只读 `list` 与 4 次 `create`；源音频上传 4 次；voice ID 创建 4 个、删除 0 个。实际账单金额需等待 Provider 账单对账，本地只能确认直接创建费用不超过已批准的 USD 0.04。

本步骤没有进行 TTS 合成、盲测、淘汰项删除、副语言事件库处理、发布或 rollout。这些仍属于下一阶段的独立授权与费用范围。

## 19:33 北京区 TTS 盲测包完成

用户随后明确授权继续执行本地 TTS 合成与盲测阶段。该授权只覆盖固定的 12 句 × A/B、共 24 条本地盲测音频；不覆盖 Provider voice 删除、训练/微调、公开发布、rollout 或副语言 ordinal 2/3。

实时协议按北京区 `qwen3-tts-vc-realtime-2026-01-15` 固定为 WebSocket `commit` 模式，输出 PCM S16LE / 24 kHz / 单声道，并在完整接收后于本地封装 WAV，不做响度归一化或其他后处理。每条合成在联网前先写 attempt，WAV 与 result 均成功落盘后才进入下一条；任何悬空 attempt 都会停止整个运行。

初始预算把 594 个 Unicode 代码点误当成 594 个计费字符。第一条握手还发现服务端把 `Chinese` 规范化回传为 `chinese`：该运行在发送文本前即停止，保留 1 个悬空 attempt，计费字符为 0。第二个运行成功生成 1 条后，服务端回执显示 19 个代码点按官方 CJK 双字符规则计为 36 个字符。两个低估预算的运行均保留为不可变历史并被新清单显式 supersede，不做自动重试或原地改写：

- 预提交停止：`voice-provider-blind-test-run-b3a3b6fccd78a20b5d56`；0 个完成输出、1 个悬空 attempt、0 个已提交文本字符。
- 单条计数校准：`voice-provider-blind-test-run-03e185dea3250d531f91`；1 个完成输出、0 个悬空 attempt、Provider 用量 36 字符。

官方规则为 CJK 表意文字每个按 2 字符、其他代码点按 1 字符。固定 24 条输出的静态计数为 1,052；新权威运行把前序 36 字符一并纳入 USD 0.02 整阶段上限：

- run：`voice-provider-blind-test-run-752a4dd81a874a263de7`
- manifest SHA-256：`4985331affd3c3399ea0696b63c575b42cd8e8ded07aaa68c558583ac8a25f25`
- manifest 文件 SHA-256：`45530d8a7f739c87208a6728ea3b41d5eb58efbd72d3de381da7f128b171a766`
- 计划：24 条、594 个 Unicode 代码点、1,052 个静态计费字符
- 前序实际用量：36 字符
- 计划整阶段用量：1,088 字符；估算 USD 0.0155968064
- 固定整阶段合成费用上限：USD 0.02

权威运行最终为 24 个 attempt、24 个 result、0 个悬空 attempt。Provider `response.done` 实际合计 1,056 字符；两条中英混合句各比静态规则多返回 1 字符 × A/B，因此含前序样音的整阶段实际回执用量为 1,092 字符，按公示单价估算 USD 0.0156541476，仍低于 USD 0.02。实际账单金额仍需等待 Provider 账单对账。

24 份 WAV 全部重读验证通过：持续时间 3.36–9.12 秒、24 个唯一 WAV SHA-256、0 个满幅样本、0 个缺失文件、0 个哈希不一致。公开盲测包如下：

- 本地评分页：`Data/Voice/tts_provider_blind_tests/voice-provider-blind-test-run-752a4dd81a874a263de7/review/review.html`
- 公开 manifest SHA-256：`33ae35b8795f87252454da16bbe59ef5c1e79495dc30fda02cb911f9678fd13b`
- 公开内容：12 个同参题组、24 个音频、每角色 2 个随机不透明标签、六维 0–5 评分、关键失败项、单题偏好、本地保存与 JSON 导出
- 隐私核对：四个真实 Provider voice ID 均未出现；候选 A/B 键、operator mapping 与 Workspace ID 均未出现

应用内浏览器的自动化安全策略禁止直接导航到 `file://`，因此没有绕过该策略。替代验收已完成：HTML 可执行脚本通过 Node 语法检查，12 个用例/24 个音频引用全部存在且哈希匹配，保存/导出控件存在。页面仍需用户在本地手动试听并提交盲评；在盲评回执产生前，不解析 A/B 映射、不选 winner、不删除 loser。

截至本阶段：累计只读 `list` 10 次、voice `create` 4 次、源音频上传 4 次；实时合成连接 26 次，其中 1 次在文本前停止、25 次完成文本合成（1 条校准样音 + 24 条权威盲测）；voice 删除 0 次。副语言 ordinal 2/3 继续保持 `base_tts_training=excluded` 与 `pending_human_event_qa`。

## 2026-09-04 相对判断第一轮

根据用户对六维 0–5 评分过于抽象的反馈，后续人工判断已切换为“相对偏好 + 绝对可用性 + 具体否定原因”。数值评分不再作为必填项，也不会被描述成 Provider 的学习信号；百炼不会从本地否定回执中在线学习。

第一轮没有重复调用 Provider，而是从上述权威盲测包中复用每名角色的中性、亲密轻声（有词）和激动三类锚点，共 6 题、12 条既有 WAV：

- round：`voice-preference-round-77f40486985a7f7304bb`
- manifest SHA-256：`f67610e8f9cb1ac9dc6cf8d5ad95fceca0420d5b26fe855b78a56edda8fad669`
- manifest 文件 SHA-256：`bf886b6a130936e12fcdc8235e49a52c1cd87e31e90869ddd675ff2e5fb39a0c`
- 本地判断页：`Data/Voice/tts_preference_tournaments/voice-preference-round-77f40486985a7f7304bb/review/review.html`
- 状态：`awaiting_local_pairwise_rejection_decisions`
- 本轮新增 Provider 输出：0；新增费用：USD 0

每题只需选择“第一个更好 / 第二个更好 / 两个都否”，随后独立确认相对胜者是否已经可以直接用于项目；若不能使用，则至少选择“不像本人、语气或角色感不对、咬字或断句错误、机械感/爆音/接缝”之一。下一轮只有在收到完整判断 JSON 后才按原因定向生成挑战者。现阶段继续不解盲、不删除任何音色，也不处理 ordinal 2/3。

## 2026-09-04 相对判断回执与第二轮挑战者

用户已从第一轮页面导出完整判断 JSON，并完成不可变回执固化：

- decision receipt：`Data/Voice/tts_preference_tournaments/voice-preference-round-77f40486985a7f7304bb/operator/decision-receipt.json`
- receipt SHA-256：`3bff4d6aabc0646a721db06f0a7b6be4ae3283147aa45cef7bf9f3bf071817b0`
- decision set SHA-256：`0e77d7d25c08d481eda66764d5aea6db15702eccb677fe30ed10787c4e0abea3`
- 完整度：6/6；相对选择为“第二个更好”5 组、“两个都否”1 组
- 绝对可用性：0/6 可直接使用，6/6 不可直接使用
- 否定原因：语气或角色感不对 6 组，不像本人 2 组；咬字/断句与合成伪影均为 0 组

官方模型能力表确认当前固定的 `qwen3-tts-vc-realtime-2026-01-15` 支持声音复刻但不支持 instruction control；不能把无效的 `instructions` 字段写入请求，也不能把本地否定回执描述成 Provider 在线学习。因此第二轮保留相对胜者作为锚点，仅使用不改变词汇内容的句号、逗号、感叹号重排来引导停顿与强度。第一轮双拒的薇蒂雅轻声组从两个现有克隆各生成一个新挑战者，其余 5 组各复用 1 条相对胜者并生成 1 条同音色挑战者。

私有挑战者运行：

- run：`voice-preference-challenger-run-e97bb3a954c16e846e1d`
- manifest SHA-256：`21caadbbf4a86cb008980399ed098f4a4079e70c8a55f6bf044436a240658d8a`
- manifest 文件 SHA-256：`1fc6961ffc559bebf235565618e1eed5787220748bdbecc195394027b43cd543`
- 计划与完成：7/7 条，7 个 attempt、7 个 result、0 个悬空 attempt
- Provider 回执用量：248 字符；估算新增费用 USD 0.0035551544，低于固定上限 USD 0.01
- 音频复验：7 条均为 PCM S16LE / 24 kHz / 单声道，0 个满幅样本

第二轮公开本地判断包：

- round：`voice-preference-round-2a1107a19b7d12fa7756`
- manifest SHA-256：`e944bafb2d457d5a33895cd7f1005ea4e563db887847939060ec752df3dc0332`
- manifest 文件 SHA-256：`172a79bb6e9a335764e5e997095e9aef0a3989614b3c50701ab22e6f881f472b`
- 本地判断页：`Data/Voice/tts_preference_tournaments/voice-preference-round-2a1107a19b7d12fa7756/review/review.html`
- 内容：6 组/12 条；7 条新挑战者、5 条复用锚点，全部重新随机化不透明标签
- 自检：12 个唯一 WAV，时长 3.36–5.20 秒，0 个满幅样本；嵌入 JSON 与页面脚本均可解析；无数值评分、A/B 映射、Provider voice ID 或 Workspace ID

本阶段没有创建或删除 Provider 音色，没有训练/微调、发布或 rollout，也没有把 ordinal 2/3 纳入基础 TTS。当前门禁切换为等待第二轮完整判断 JSON；收到前不继续新增 Provider 调用。

## 2026-09-04 第二轮回执与第三轮未见台词验证

用户已导出第二轮完整判断，回执复验结果如下：

- decision receipt：`Data/Voice/tts_preference_tournaments/voice-preference-round-2a1107a19b7d12fa7756/operator/decision-receipt.json`
- receipt SHA-256：`a98321fd47048199faffcb6ffbfe90f0f1d443fc2a9837053883c2c506f203c6`
- decision set SHA-256：`128a024041b99c4f72863fafa4162479e7bee3f2e574503725ea3ed2a9ce2d35`
- 5/6 组被判定为可直接使用：薇蒂雅中性、亲密轻声、激动，以及辰星中性、亲密轻声
- 薇蒂雅与辰星的亲密轻声均选择新挑战者；其余三个可用组保留上一轮相对胜者
- 辰星激动组为“两条都否”，唯一原因是“不像本人”

因为单句可用不能证明任意文本上的稳定性，下一步没有继续对固定台词重复抽样，而是构建全新的未见台词泛化轮：每名角色的中性、亲密轻声和激动各使用一条未在前两轮出现的台词，并让两个现有克隆在完全相同文本上重新盲比。这同时给辰星激动组切换至另一克隆的机会。

私有未见台词运行：

- run：`voice-preference-challenger-run-e33257bc2ac78fb7a8f6`
- manifest SHA-256：`7b6bf766da51b004d3864e45479294a4d38d64940c17b4c46b2330085e709635`
- manifest 文件 SHA-256：`bd0b62d07979f6c97e828dce621f8abac2113ba643202acc5f049464f368eb4a`
- 计划与完成：12/12 条，12 个 attempt、12 个 result、0 个悬空 attempt
- Provider 回执用量：396 字符；估算新增费用 USD 0.0056767788，低于固定上限 USD 0.01
- 仍使用北京区 `qwen3-tts-vc-realtime-2026-01-15`；未发送 instructions，未使用副语言事件素材

第三轮公开本地判断包：

- round：`voice-preference-round-9fcc6ed7447cbfa10728`
- manifest SHA-256：`3b01a1e49e1b8d8dc7358d0d09fbc604e56c7180feaeef440ebbe4c52b90ca78`
- manifest 文件 SHA-256：`ac92d930cb9e2589d04790297fb04ecff1c10ff2d8408f886df467634b5da5e8`
- 本地判断页：`Data/Voice/tts_preference_tournaments/voice-preference-round-9fcc6ed7447cbfa10728/review/review.html`
- 内容：6 组/12 条，全部为新合成的未见台词，两套克隆重新随机化不透明标签
- 自检：12 个唯一 WAV，时长 3.36–5.04 秒，0 个满幅样本；嵌入 JSON 与页面脚本均有效；无数值评分、A/B 映射、Provider voice ID 或 Workspace ID

本阶段仍没有创建或删除 Provider 音色，没有训练/微调、发布或 rollout，也没有把 ordinal 2/3 纳入基础 TTS。第三轮被固定为当前两个现有克隆的终局未见台词验证：收到完整判断 JSON 后，可用的相对胜者直接锁定为对应角色与语态槽位候选；相对胜者仍不可用或两条都否时，该槽位记为暂不合格并暂停，不自动生成第四轮或继续抽样。只有更换源素材、模型，或用户明确要求重开时才建立新实验。当前门禁为等待第三轮完整判断 JSON；收到前不继续新增 Provider 调用。

### 2026-09-04 第三轮终局回执与结案

第三轮完整判断 JSON 已接收并通过 fail-closed 校验：

- decision receipt SHA-256：`08b308dc876a274b7c1fef2b282d944dd466c5a7898b34528d75b23d42c06971`
- decision receipt 文件 SHA-256：`dc78b897764c4fa7ff30ad09ffb7695abf2ab7d55b4788cb0f463382ad4ce31e`
- decision set SHA-256：`6fe83cbc9a1197d0708d697fcb76ee9a5a31de4a33777ef816e7588c64a3fb77`
- 6/6 组完成；第一条胜出 1 组、第二条胜出 5 组、双拒 0 组
- 5 组通过绝对可用门禁；1 组未通过，唯一原因是“语气或角色感不对”

终局槽位结论：

| 角色 | 槽位 | 结论 |
| --- | --- | --- |
| 薇蒂雅 | 中性 | 锁定私有候选 |
| 薇蒂雅 | 亲密轻声（有词） | 暂停；当前候选池无合格结果 |
| 薇蒂雅 | 激动 | 锁定私有候选 |
| 辰星 | 中性 | 锁定私有候选 |
| 辰星 | 亲密轻声（有词） | 锁定私有候选 |
| 辰星 | 激动 | 锁定私有候选 |

私有结案记录：

- conclusion：`voice-preference-terminal-conclusion-9d8f74ef5ed6406a74c3`
- manifest SHA-256：`4a523ff36a6340a250acba8dba0e6b284d93a9e6a5d777c8efc6a93e6f1d4aec`
- 路径：`Data/Voice/tts_preference_tournaments/voice-preference-round-9fcc6ed7447cbfa10728/operator/terminal-conclusion.json`
- 5 个锁定槽位的候选引用仅保存在私有 operator 记录中，未写入公开试听包或本报告
- 自动追加 A/B 轮次已关闭；暂停槽位不会触发重采样
- 本次接收与结案未调用 Provider，新增费用 USD 0

本阶段已从“等待第三轮判断”转为“当前克隆池终局结案”。后续可以进入运行时接入与安全合成验证，但薇蒂雅亲密轻声槽位必须保持关闭；若未来更换源素材、模型，或用户明确要求重开，需另立实验并保留本结案记录。

### 2026-09-04 私有运行时路由接入

终局结论已转换为本地私有、按角色与语态分槽的运行时清单：

- profile：`voice-runtime-profile-e95e7d8e42cdc7c3d241`
- manifest SHA-256：`b3e3af75e61ed8f541ebafb389d649ddec9155b4b61c3275779e2789cca1e31d`
- manifest 文件 SHA-256：`c457cac0f845fe5a09b20e09af260de9a71403cd62725e84feb4d1f872289bf4`
- 路径：`Data/Voice/tts_runtime_profiles/voice-runtime-profile-e95e7d8e42cdc7c3d241/manifest.json`
- 2 个角色、6 个语态槽位；5 个锁定、1 个暂停
- 北京区、目标模型、24 kHz 单声道 PCM 合同均已重新校验
- Workspace 与 Provider voice ID 仅存在于私有 manifest；生成器、校验器及应用响应均不输出这些值
- 语态使用保守词汇规则选择；非词汇呓语与 ordinal 2/3 仍不进入基础 TTS
- 暂停槽位禁止跨语态、跨角色或通用音色回退，并在打开网络连接之前终止

应用适配器与伪 Provider 测试已经就位，但实时调用开关保持关闭。本次生成清单、加载配置与测试均未读取 API 凭据、未连接 Provider，新增费用 USD 0。下一门禁改为一次有明确费用上限的北京区真实冒烟合成；通过后才讨论日常聊天概率与面对面自动播放的产品策略。

### 2026-09-04 北京区运行时真实冒烟

用户在收到“5 个锁定槽位、179 个计费字符、费用硬上限 USD 0.005”的明确范围后要求继续任务，该直接回复被记录为本次有限授权。执行结果：

- run：`voice-runtime-smoke-run-9b5aac516b45bdfcff53`
- run manifest SHA-256：`186e8d844f8933204c75168477791e07a56f362d5ebfae832e52de4f1f4615f5`
- run manifest 文件 SHA-256：`a4429cde4c5dbe763002cd833bee1e34628f9be486db9a4fd67e1187ddefe75c`
- 5/5 条成功；5 个 attempt、5 个 result、0 个悬空 attempt
- Provider 回执与保守计费均为 179 字符
- 按固定单价估算费用 USD 0.0025660187，低于 USD 0.005 硬上限
- 5 个 WAV 均唯一，时长 3.92–5.84 秒，24 kHz 单声道 PCM S16LE，满幅削波样本总数 0
- 薇蒂雅亲密轻声暂停槽位未调用；没有跨语态或通用音色回退
- 未创建新 A/B 轮次，未创建/删除音色，未训练/微调，未发布或 rollout

本地试听页：`Data/Voice/tts_runtime_smoke_tests/voice-runtime-smoke-run-9b5aac516b45bdfcff53/review/review.html`

试听 manifest SHA-256：`ead91cb9111a61d99bb7a2c8e1bc2999fbc37ff2bf1f3615a99ac3b7e3d4d829`。页面与公开试听数据不包含 Provider voice ID、Workspace ID 或私有候选映射。

冒烟通过后已完成离线产品策略接线：用户启用线程语音偏好后，面对面渠道每次请求语音并标记自动播放；文字渠道日常回复采用 25% 稳定概率，保守识别为高情绪时采用 45%，生成后显示为可手动播放的语音消息。概率以逻辑消息身份和规范化回复文本确定，同一消息重试不会重新抽签。持续运行开关尚未启用，因此这部分接线没有新增 Provider 调用或费用。

## 主 A/B 候选

| 角色 | 槽位 | candidate | 自然版 | 压缩版 | 压缩切点 | 压缩版 SNR | 压缩版持续静音超额比 | true peak | 信号 QC |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 薇蒂雅 | A | 143 | 16.397333s | 14.959042s | 2 | 30.610dB | 2.3397% | -9.042dBTP | 通过 |
| 薇蒂雅 | B | 137 | 15.721292s | 15.721292s | 0 | 32.271dB | 9.4140% | -9.063dBTP | 通过 |
| 辰星 | A | 149 | 18.672000s | 15.817208s | 3 | 32.986dB | 4.4888% | -10.645dBTP | 通过 |
| 辰星 | B | 148 | 16.688000s | 14.036083s | 3 | 30.514dB | 0.0000% | -8.757dBTP | 通过 |

四个槽位均为 24kHz、单声道、PCM S16LE；削波样本数均为 0。薇蒂雅 B 的自然版与压缩版字节哈希相同，因为没有发生内部静音压缩。

## A/B 隔离核对

每个角色的 A/B 来自同一份已归档源录音，但满足当前固定策略中的隔离条件：

- candidate ordinal 不同；
- original candidate ID 不同；
- 屏幕逐字稿 SHA-256 不同；
- 输入候选 WAV SHA-256 不同；
- 源帧时间范围不重叠。

因此没有重复使用同一父 candidate、同一音频片段或同一文本。屏幕文本继续是逐字稿唯一事实来源。

## 人工试听入口

- 薇蒂雅：`Data/Voice/recording_dialogue_comparisons/voice-recording-dialogue-comparison-a62af942ac96e7bc059e/review.html`
- 辰星：`Data/Voice/recording_dialogue_comparisons/voice-recording-dialogue-comparison-9eaa4601e56ff9917069/review.html`

两个 comparison manifest 作为不可变来源，原始 `review_status` 仍是 `awaiting_human_natural_vs_compacted_comparison`；其状态已由上面的独立人工回执权威取代，不需要重复试听。试听页继续保留作审计入口。

## 来源与完整性

- 长对话语料分析：`voice-recording-dialogue-corpus-analysis-062491a22ef42237f9d3`
  - manifest SHA-256：`5c8eced4da50638c2f2016f002fa9803528e2c718d00303c39a1a1ee50b2ccce`
  - 文件 SHA-256：`ac95cef3b31d6af1d7e487bc46785af9c2a41df291779da0897adf6a3b5f822b`
- 固定压缩策略人工批准：`voice-recording-dialogue-review-896557593d1f495cc286`
  - manifest SHA-256：`8629032fc7e7688a91fc4354fc16c16378ad1572763be9d6a11235173496916c`
- 待审对比批次：`voice-recording-dialogue-comparison-batch-2b1a6205488b251038a3`
  - manifest SHA-256：`f397560a7b979da7d8f59db364058a81bba142cb668b427ed246973f5eecf684`
- 薇蒂雅对比包：`voice-recording-dialogue-comparison-a62af942ac96e7bc059e`
  - manifest SHA-256：`79c921ffa02b59d1ec792b34be48fe5d46be7da7cfecb58caee004232f220aec`
- 辰星对比包：`voice-recording-dialogue-comparison-9eaa4601e56ff9917069`
  - manifest SHA-256：`725e493488b13d6b5073e889afbc32c2cf251a4c91a05c61578128a31ca3f03c`
- 15 段语料路由：`voice-corpus-routing-14f909c7aab99190ef66`
  - receipt SHA-256：`4b2a74833d41905cfe5f93b8ccb84acb1448fb3a99e46e8b57e53f54f62548c1`
- 15 段包内可行性审计：`voice-composition-viability-bb07c555d186a6dfaf63`
  - receipt SHA-256：`614c460981be375c2f30a9cc5b22a85a74d77d522c2e1b89312cac17577b74ad`

本次对 4 份逐字稿、8 份 WAV 和 2 个试听页逐一复算文件 SHA-256 与字节数，14/14 均与各自 manifest 一致。

## 门禁

本记录已完成本地语料来源对账、四槽人工试听回执、离线 Provider 预检、北京区四槽 voice 创建、单独授权的 24 条本地 TTS 盲测包、三轮完整判断回执、第三轮终局选型、私有运行时路由接入及 5 条真实运行时冒烟。当前克隆池的 A/B 判断流程已经关闭，不生成第四轮：5 个通过槽位锁定私有候选，薇蒂雅亲密轻声槽位暂停。真实冒烟已在 USD 0.005 上限内通过；下一门禁是试听确认和持续使用费用范围，获得授权前运行开关保持关闭。训练/微调、副语言事件库、Provider voice 删除、公开发布与公网 rollout 继续保持关闭。四次 voice 创建与既有合成的实际账单金额均等待 Provider 账单对账。
