# Project Snow 长对话静音策略验证

日期：2026-08-31

## 结论

现有邮件录音应按完整、自然表演的长对话处理。旧版 `20 ms frame RMS <= -45 dBFS` 的逐帧静音比例把角色的自然停顿、气口和表演性留白一并计为失败，不能单独作为补录依据。

本次以恩雅已审核录音为固定验证对象，在不更改屏幕文本、不删除任何检测到的有声区域、不重新录音的前提下，成功得到互不重叠并通过信号门槛的 A/B 对照样本。因此当前没有证据要求恩雅补录。

## 固定输入

- boundary adjudication：`voice-recording-boundary-adjudication-3c4102db6ee3378a7862`
- receipt manifest SHA-256：`771450af8423090ca972ba481fab9ff8b0a9c0bb950b275eedf38311b4d52c33`
- decision set SHA-256：`8016e4b63b84f593230f847cb0b2f01135a00110230913327876a4f009288db0`
- 恩雅 runtime character ID：`43f05917bfa1`
- 首轮排除项仍保持排除，没有重新引入 candidate 52。

## 新的对照规则

- 屏幕文本为唯一逐字稿真值；ASR 不改写文本。
- 自然版只在低能量零交叉点安全移除首尾录制余量。
- 只有连续低能量间隔至少 `1000 ms` 时才允许内部压缩。
- 每个低能量连续段先容许 `750 ms` 的自然停顿；只有超出部分计入持续静音门禁。
- 内部切点在低能量零交叉处，源音频两侧各保留约 `120 ms`，中间插入 `240 ms` 静音，目标保留停顿约 `480 ms`。
- 旧逐帧静音比例继续记录，但只作可观察指标，不再作为门禁。
- 父候选数量与内部静音切点数量分别记录；内部切点不伪装成多个语料父片段。
- 其他门槛未降低：压缩版 10–20 秒、SNR >=25 dB、true peak <=-1 dBTP、削波样本数为 0、持续静音超额比 <=20%。

## 恩雅真实验证结果

共有 7 个已接受且未排除的候选，6 个通过新规则。最佳互斥 A/B 为：

| 槽位 | candidate | 自然版 | 压缩版 | 内部切点 | 逐帧静音比（观察） | 持续静音超额比 | SNR | true peak | 结论 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A | 53 | 16.899708 s | 14.411708 s | 2 | 47.0180% | 7.9102% | 31.900 dB | -9.592 dBTP | 通过 |
| B | 55 | 15.575417 s | 13.717208 s | 3 | 40.5248% | 8.1649% | 29.958 dB | -11.313 dBTP | 通过 |

唯一未通过者 candidate 56 的原因是压缩后 `9.848208 s`，低于既有 10 秒门槛；不是静音、SNR、峰值或削波失败。

## 不可变试听包

- package ID：`voice-recording-dialogue-comparison-e7fb590cbf6634457e5c`
- manifest SHA-256：`0ba301c0fbed18f78d2bbe2220d9892ed12b30bb4a0180e6dbfdb080220b9aa7`
- package path：`Data/Voice/recording_dialogue_comparisons/voice-recording-dialogue-comparison-e7fb590cbf6634457e5c`
- A natural WAV SHA-256：`b70c6b70115fc0632458cac20d2f5548b931ccac2ab8e3e1517f7afa64fcb82c`
- A compacted WAV SHA-256：`52e576ec0560821ab4a6a4cea65d37a033908df07a2f9111fb7b0b663a3f495b`
- B natural WAV SHA-256：`6379c5a8846fcd3d0d4d296b0e6a14268bc47fcf454641e296a2286f89e02c8a`
- B compacted WAV SHA-256：`53abeef6e69bff1457cb4b4d4ca7a9ea168f1e0d38b69be3bdd8bd3513c8907a`

包内所有文件为只读；逐字文本 SHA-256 与固定边界候选中的屏幕文本哈希一致。manifest 记录了源帧范围、每个删除区间、零交叉证据、WAV/PCM 哈希、全部指标和关闭状态。

## 门禁与下一步

本产物只允许本地“自然节奏版 vs 安全压缩版”试听。以下状态全部保持 `false`：试听结论批准、训练、声音克隆、权利接受、Provider 注册、发布和公网 rollout。

下一步是人工比较恩雅 A/B 的自然版与压缩版，重点听是否吞字、切气口、破坏语义停顿或造成节奏突兀。只有该规则获批后，才按同一固定策略批量分析其他角色；不得针对不同角色临时改变门槛。
