# Project Snow 公网四角色语料可行性状态

记录时间：2026-08-31T09:25:47.7177393+08:00

## 固定输入

- 边界裁定：`voice-recording-boundary-adjudication-3c4102db6ee3378a7862`
- 边界裁定 manifest SHA-256：`771450af8423090ca972ba481fab9ff8b0a9c0bb950b275eedf38311b4d52c33`
- 片段目录：`voice-recording-fragment-catalog-2d6ca2e887a2df2a60bd`
- 片段目录 schema：`project-snow-private-local-voice-fragment-catalog-2`
- 片段目录 manifest SHA-256：`ee0bbaae4747d2a94900f4e32722ad42423e9a879efc5370b79d3b1a499cc93d`
- fragment set SHA-256：`2d8a57d8096645fc074ba4a66584225b78818dab25a653d9a960527ec913caa4`
- fragment policy SHA-256：`fd53ad2a17ec9206da59e20a0ace3db4d426ef6dda801f5904b08911d1b42bc3`
- boundary set SHA-256：`a84e0c48d409a265c3773c8f3c19a4390c6b8f8473887aac4f34f86a8d59d0a8`

## 不可降低的 QC 门槛

- 每份样本 10–20 秒。
- 每份样本 1–4 个互不重复的父片段。
- A/B 不得共享父 candidate、音频哈希或文本哈希。
- 静音占比不高于 20%。
- SNR 不低于 25 dB。
- true peak 不高于 -1 dBTP，削波样本数为 0。
- 屏幕文字是逐字稿唯一事实来源；ASR 只可辅助定位，不能改写文本。

## 穷举结果

| 角色 | runtime character ID | catalog 片段数 | 时长可行组合 | 已评估组合 | 完整 QC 通过 | 来源互斥 A/B |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 恩雅 | `43f05917bfa1` | 13 | 68 | 68 | 0 | 否 |
| 芙提雅 | `a2ffc5b44d7f` | 13 | 135 | 135 | 0 | 否 |
| 薇蒂雅 | `5157b8972632` | 11 | 40 | 40 | 0 | 否 |
| 辰星 | `98322bd505f4` | 17 | 419 | 419 | 0 | 否 |

本轮搜索没有截断。完整候选 WAV 的 SNR、峰值与削波此前均合格；当前共同失败原因是长停顿导致静音占比超过 20%。继续重跑相同目录、调换组合顺序或重新加载 GPU 模型不会改变结论。

## 下一步：补录，不再重复计算旧目录

每个角色补充至少两份相互独立的连续录音，供 A/B 各自使用：

- 单份建议录制 12–18 秒连续台词，最好包含 3–4 个完整句子。
- A 与 B 必须来自不同台词内容；重复播放同一段游戏音频不算独立来源。
- 单一说话人、无音乐/SFX/串音；尽量避免超过 250 ms 的句内或句间空白。
- 继续使用游戏屏幕文本原样填写 `displayed_text.txt`，包括角色特有儿化音。
- 文件仍按 `角色名/邮件标题/original.mkv + displayed_text.txt + metadata.txt` 放置。
- 多装甲角色只需选定一种装甲语音；后续发现其他装甲可收录，但不纳入当前处理。

新增语料到位后，只运行边界检查、静音压缩和 A/B QC；除非发现边界歧义，否则不再运行完整 ASR/GPU 对齐。

## 门禁状态

本记录只说明离线语料 QC。训练、声音克隆、Provider 注册、权利、发布和公网启用均未获批准，继续保持关闭。
