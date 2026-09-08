# S1 兼容版本上线记录（2026-09-08）

**S1 已于 13:31:35 香港时间（05:31:35 UTC）完成晋级。实际运行 `7ce0205 / 0.10.0-rc.1 / green`；独立后验于 05:48:54 UTC 通过。**

本次上线建立旧外观的浏览器兼容回退目标。小吉终端名称、图标和新界面仍由下一次 S2 发布启用。完整角色舞台和公网语音没有启用，正在制作的独立工作树内容不会自动进入已固定的镜像或媒体包。

## 发布身份与批准

| 项目 | 身份 |
| --- | --- |
| 应用提交 | `7ce0205f1f9d6fad275b6542c853541ef1224db6` |
| 应用镜像 | `ghcr.io/x1aob/project_snow-public@sha256:02a3578350d3a97580da19221b3686135eae961b76ffecd828adc17acff73ce1` |
| 前端 | `compat / compat-096-r1` |
| 前端包 SHA256 | `5adc8133f908d6c2696752c329aea54e9546cb4245da970028af33ae2eea0503` |
| 独立发布控制器提交 | `b149d0571dfdef5bf8e822577565c6803c600093` |
| 实际晋级回执 | `b88b93a4c6b2bef614a64282b3a9018455407a5caf1e08b2d0d416a02b3ae265` |
| 回执创建 / 批准 | 05:28:25 / 05:30:22 UTC；依据用户对 S1 的明确继续授权 |
| 晋级执行 | 05:30:46–05:31:35 UTC，正常 runner exit 0 |
| 上一稳定版 | `502ec99412bef843c37e4b31a53df8fa9faeb33c / 0.9.6 / blue` |

应用 [main CI 34182286331](https://github.com/X1aoB/Project_Snow/actions/runs/34182286331) 和[发布证明 34182824713](https://github.com/X1aoB/Project_Snow/actions/runs/34182824713) 成功；服务器先独立验签，再执行候选。候选详情与原始检查见 [S1 候选验收](s1-candidate-20260908.md)。原 `09ed2cd6…` 回执已由完成路由维护后的新回执替代，不用于本次批准。

## 两项维护发现与实际处理

### Docker 挂载顺序导致指纹误判

首次持久路由迁移于 04:52 UTC 因 cloudflared 指纹误判触发自动恢复；公网重新确认旧版健康。Docker 对同一容器返回的 `Mounts` 顺序不稳定。修复对完整挂载记录排序，保留所有字段和重复项；路径、权限、网络等真实变化仍然拒绝保留。

[PR #47](https://github.com/X1aoB/Project_Snow/pull/47) 的 [main CI 34189419495](https://github.com/X1aoB/Project_Snow/actions/runs/34189419495) 和[签名 34189981242](https://github.com/X1aoB/Project_Snow/actions/runs/34189981242) 通过。服务器只独立更新发布控制器至 `b149d05`，应用候选继续使用 `7ce0205`，没有拉取或启用 `b149d05` 的应用镜像。控制器事务期间全部 25 个容器和在线配置保持不变。

05:25 UTC 的重试因旧 API 还有一个 TCP 连接在只读预检阶段停止，没有修改运行状态。05:27:54–05:28:00 UTC 的实际维护在请求、租约和 TCP 均为空时成功：仅以相同镜像重建 Caddy，挂载持久路由目录，上游仍为 blue；其余 24 个容器、受保护配置和恢复锚点不变。随后重做候选验收、记录批准并执行晋级。

### 晋级额外重建 origin-edge

晋级中的 origin-edge 没有按预期保留。首次独立后验正确报出 `unexpected-container-change`，原脚本与失败事实继续保留；没有直接取消这一检查。

原因是普通正向发布使用不同的不可变配置目录，即使 `OriginEdge.Caddyfile` 字节和版本化 TLS 目录相同，原分支也可能选择重建。预检中的配置等价检查没有覆盖这一实际动作选择。05:31:26 UTC 新 origin-edge 启动，属于一次额外入口重启；现有证据不证明该时段连接完全无中断。

后续独立验证固定了原、新容器 ID，核对 20 项运行安全/网络/挂载条件、全部 14 个配置摘要、Compose 模型与实际 config-hash。镜像、安全策略和三份 TLS 挂载均相同，唯一挂载来源变化为字节相同的 Caddyfile 所属发布目录。旧 origin 环境文件仍保留。新证据明确记录 `same-policy-restart-not-retention`，没有将容器重建改称保留。

后验只依据该精确证据允许已核对的 origin ID、binding 和环境枚举坐标变化；其他变化仍拒绝。额外重建的后续修复在独立工作树开发，尚未因此再次操作生产入口。

## 上线后验与恢复材料

- 实际镜像、内部 build-info、真实公网 TLS 及前端包身份一致；内部 live/ready/full 均为 `ok`，公网不暴露 full/private API。
- 在已完成路由维护后的 25 个容器中，22 个完整记录不变。blue 为原容器停止保留，mailer 正常换成 S1 镜像，origin-edge 是上述单独核对的偏差。Caddy、cloudflared、egress、共享 embedding 和 Dify 保持不变。
- 持久路由为 green，三项已保存路由服务指纹和模型相符；候选登记已清除；旧恢复锚点校验一致。磁盘可用 `17,461,370,880` 字节（约 16.3 GiB），超过 10 GiB 门槛。
- 05:38 UTC 生产监测 public/DB/retrieval/queue/draining/backup/disk 均正常；队列为 0 活跃、0 排队，配置上限 4/8。短时结果不代表容量或长稳验收。外部告警接收端仍未配置。
- `backup --pin` 成功，05:42:14 UTC 的 last-success 指向 S1 快照 `a2a685ad4aad63550d4fea0ae4b130f2a4c1e96cbbf8e62d04edc076311d246a`。本次不是新快照全量恢复；独立镜像归档与固定 registry digest 的获取仍须按恢复手册执行。
- 独立 restic 只读核验确认该快照仅带 S1 的永久 pin 标签；从加密快照读出的 recovery.json 与 green/7ce 和 dump 摘要一致，恢复清单 SHA256 为 `8eaca5bc429d4f138997c5beccb44d117ca779a189d7edc2afd2ce52c197c166`。原 `828b0a9f`、`48398acd`、`fd81b13a` 三份基线 pin 仍在。
- 公网只读浏览器在桌面 1365×900、移动 390×844 验证 TLS、资源哈希、22 角色目录、完整引导及可见头像。桌面 9/9、移动抽屉 8/8 头像真实 200 且完整解码，屏外懒加载不计作失败。CSP 保持开启，无页面异常。
- 只读截图中的 `Failed to fetch` 来自测试主动拦截 presence POST；这份报告只证明壳层和资产，不作为聊天可发送的证据。私有候选此前另有 4 次无模型 presence、角色选择及真实 IndexedDB v4 草稿刷新恢复检查。
- 随后独立公网用户流程于 05:53:45–05:54:07 UTC 通过：桌面和 390px 窄屏各完成公告/引导、角色切换、真实 IndexedDB v4 草稿提交和刷新恢复。严格限于 `/presence/resolve` 的 8 次请求均为 200；该路由无模型或业务记录写入，但会产生短期限流计数与临时 cookie。其余聊天、BYOK、provider、反馈、邮件请求未发送。无错误横幅、无页面异常，TLS/CSP 未放宽；截图经独立查看。窄屏长草稿的已知显示裁切由 S2 修复，保存与恢复正常。

原始 502 标签页需要刷新进入 S1，才进入后续 S1→S2→S1 兼容窗口。本次没有真实模型调用、真实手机软键盘、压力测试或空主机恢复验证；不能据此宣称全部优化计划完成或绝无故障。

## 证据索引

服务器私有目录 `/root/snow-s1-routing-20260908-r3/` 保留原始结果；副本只放集成工作树的忽略目录 `.tmp/s1-production-evidence/`，不提交主机私有配置。

| 文件 | SHA256 |
| --- | --- |
| `result.json`，路由维护成功 | `2d825edd30f667086561da5eb9ed130881a2facce24ca8f591497f4eafba1b99` |
| `promotion-result.json`，晋级 runner | `c0e14e28378d8bfd6d539b56adcc8a36e0b12228db1720af5a20a5090e1996fb` |
| `origin-replacement-validation.json` | `60a4b25e2d0cda40bf9c84aa911a67bb85a85fd2c7b6e93e4bf6d517c2570e50` |
| `promotion-validation.json` | `f3e2ba6d575a9a60d70d60376760ebfe50d53fcc9bdf605fb3a14df5552341cc` |
| `/srv/project-snow/backups/last-success.json`（05:42 快照） | `8682e1e3a66f9008a56143ca95d3e18bba31a61c26df2e5e0454c7d43c32b2b4` |

本地只读头像报告 `.tmp/s1-public-browser-xiwf7rfg/report.json` 的 SHA256 为 `a7f39c380c3ceca36ee4c0fec54a59a09d23ad576646d986be684410ac39148a`。此前测试观察器使用 eval 被生产 CSP 拦截的失败报告也保留；改为宿主有期限轮询后通过，未降低 CSP。

公网用户流程报告 `.tmp/s1-public-user-flow-awgpmbpc/report.json` 的 SHA256 为 `76789c36ad2fe8a4edbdcc4b86ce1b87fe9e9307e7ec2751ac6ca7e9a5dfb548`。两份浏览器报告的范围不同，不合并成真实模型对话验收。

## 后续顺序

先完成 origin-edge 保留逻辑的独立回归和 PR；S1 稳定观察后，以正常 CI、签名、候选验收流程准备 S2。S2 启用小吉终端品牌、新图标及新界面，并带入独立提交 `2ca1f88` 的窄屏按钮和多行输入框修复。角色素材继续由绘制任务审核，TTS 依其最新交接保持本地测试准备，不接入公网。
