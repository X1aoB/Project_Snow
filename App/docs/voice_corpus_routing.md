# 15 段语音语料路由

这份路由把已试听的 15 段语音完整分到三个隔离轨道：

- 12 段 `lexical_base_candidate`：文字已经人工确认且没有重复，只能作为基础 TTS 候选。
- 2 段 `paralinguistic_event_candidate`：第 2、3 段，链接到独立副语言审阅凭据。
- 1 段 `excluded_duplicate`：第 12 段与第 11 段文字重复，继续排除。

“候选”不等于允许训练。路由凭据保持数据集 QC、训练、声音克隆、事件库、权利、
发布、供应商注册和公网 rollout 等开关全部关闭。

`scripts/voice_corpus_routing_ops.py` 会逐段重读 WAV，并交叉核对 review package、
ASR run、人工提交和副语言凭据。它验证音频哈希、PCM 格式、帧范围、人工文字哈希、
重复引用和 1–15 序号完整性，只新增不可变 JSON，不改任何来源。

不带 `--execute` 时为预演；真正写入还必须提供
`--confirm-candidate-routing-only`，并建议固定所有来源的字节 SHA-256。
