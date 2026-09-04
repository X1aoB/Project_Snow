# Project Snow 公网 TTS 离线回归状态

记录日期：2026-08-31

## 结论

当前尚未提交、默认关闭的公网 TTS、流式播放器、缓存、舞台嘴型桥接、运维与下架实现已完成离线回归。本轮没有发现需要修改代码的失败项；不得据此跳过真实 DashScope、PostgreSQL、香港网络或语料 A/B 验收。

## 已通过的非重叠测试组

| 范围 | 结果 |
| --- | ---: |
| 公网语音后端、存储基础与公共安全 | 71 passed |
| 部署、CSP、代理与静态指纹契约 | 65 passed |
| Chrome 语音/音频/自动播放/缓存 E2E | 18 passed |
| Qwen 音色运维、盲测结果、紧急下架与审批哈希 | 35 passed, 1 skipped |
| 样本运维、样本审计与本地录音运维 | 57 passed |
| 录音对齐、边界、裁定、片段目录与样本重建 | 83 passed, 1 skipped |
| 公共存储、迁移配置与发布清单 | 10 passed |
| 舞台 API、舞台模型与 Mia Cubism 样本 | 36 passed |

共 375 个非重叠测试通过。两项跳过均为 Windows 当前环境无法创建测试用符号链接或硬链接，不是 TTS、播放器或供应商功能失败。

## 已验证边界

- `POST /public/v1/voice/synthesize` 与 `POST /public/v1/voice/stream` 的 ticket-only 契约。
- 流式 NDJSON、PCM 播放、回退、取消、自动播放阻断恢复和浏览器缓存行为。
- 生产部署中的流式代理例外、CSP、隐私披露与静态资源指纹。
- Qwen 音色 create/list/delete/revoke 运维路径、盲测结果验证和紧急下架路径。
- `project-snow:voice-state` 与 `project-snow:voice-energy` 的有限事件边界。
- 只有当前 `in_person` owner 可以驱动嘴型；错误 owner 不产生嘴型事件，结束/故障路径归零。
- 屏幕文本继续作为逐字稿唯一事实来源；离线 ASR 不获准改写文本。

## 发布门禁现状

- 四角色 profile 均为 `enabled=false`、`rollout_percentage=0`。
- 四角色均没有已登记 voice ID、合格样本哈希、风险接受编号或发布批准编号。
- 审批清单为空。
- 本地 `.env` 未配置公网语音总开关、风险接受、审批清单哈希、DashScope API key/workspace 或生产 PostgreSQL URL。

因此当前不存在误调用供应商或误上线风险。

## 剩余阻塞

1. 每个公网角色补录两份内容不同、相互独立的 12–18 秒连续语音，并重新生成合格 A/B。
2. 用户另行明确批准后，配置新加坡 DashScope workspace/凭证并创建临时音色；未获批准不得产生供应商费用。
3. 在真实 PostgreSQL、蓝绿部署与香港网络环境完成迁移、并发预算、实时合约和延迟验收。
4. 完成风险接受清单、发布批准清单和角色级 rollout 后，才可从 0% 进入 5%。

重复运行现有旧语料的 GPU/ASR 或组合搜索不会解除第 1 项阻塞。
