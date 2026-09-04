# 副语言语音事件处理约定

Project Snow 将角色语音分成三个互不混用的数据轨道：

1. `lexical/base TTS`：文本和语音可逐字对齐的普通台词。
2. `paralinguistic events`：气声、喘息、笑声、惊呼、非词汇呓语等表演事件。
3. `mixed performance`：台词和副语言事件按时间线组合的成品表现。

当前对第 2、3 段的决定是“保留为副语言事件候选”，不是删除或质量拒绝。
两段继续排除在基础 TTS 训练集之外；表现力训练和事件素材库均保持
`pending_human_event_qa`，没有因此获得训练权、语音权、公开发布权或供应商注册权。

## 当前标签

- `kind`: `paralinguistic_event`
- `event_type`: `nonlexical_murmur`
- `phonetic_surface`: 第 2 段 `啊啊啊`，第 3 段 `啊`
- `base_tts_training`: `excluded`
- `expressive_training`: `pending_human_event_qa`
- `event_bank_eligibility`: `pending_human_event_qa`
- `breathiness`、`emotion`、`intensity`: `unrated`

`nonlexical_murmur` 是中性的声学/表演标签。它不把片段推断成具体成人语义，也不会把未经审阅的情绪强度写入训练元数据。

## 不可变凭据

`scripts/voice_paralinguistic_ops.py` 只新增审阅凭据，不修改原始提交、队列、清单或 WAV。命令会重新校验：

- JSON 无重复键，所有语义哈希和字节哈希一致；
- 第 2、3 段在清单、ASR、人工提交中的 span ID、音频哈希和序号一致；
- WAV 为 24 kHz、单声道、16-bit PCM，帧数和时长一致；
- 原人工提交仍是 `needs_clarification`，基础训练处置仍是排除；
- 权利、公开、供应商、训练和拼接等所有范围开关仍关闭；
- 输入和输出路径不经过符号链接、重解析点或目录逃逸。

不带 `--execute` 时只预演：

```powershell
$py = 'C:\Users\25685\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py App\scripts\voice_paralinguistic_ops.py record `
  --voice-root 'C:\Users\25685\Desktop\Myprojects\Project_Snow\Data\Voice' `
  --submission-id voice-span-asr-review-submission-4e6e7ca1d7a4c3c301b3
```

真正写入时必须同时提供 `--execute` 和
`--confirm-retain-as-paralinguistic-only`。生产操作还应传入已人工核对的四个
`--expect-*-sha256` 字节哈希，使任何源文件漂移都直接失败。

## 后续使用边界

第一阶段建议采用“普通 TTS + 经审阅的角色事件素材库 + 时间线混合”，可控且容易回滚。
事件进入素材库前仍需单独审阅事件起止、呼吸感、情绪、强度、噪声和权利状态。
后续如训练支持内联事件标签的表现力 TTS，也应从这个隔离轨道导入，
不能把事件伪装成普通文字直接混入基础语料。
