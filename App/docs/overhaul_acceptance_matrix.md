# 小吉终端全面优化验收矩阵

状态截点：2026-09-07，集成分支已推送 `77b8643`，PR CI 运行中；本表不预设其结果。
生产基线为 `0.9.6 / 502ec99412bef843c37e4b31a53df8fa9faeb33c`。main 尚未合并新方案，发布证明尚未签发，未人工晋级。后续操作以对应提交、产物摘要和实际回执更新本表。

状态含义：**代码已实现**仅说明存在实现；**实测通过**必须注明本地、隔离环境或生产范围；**待生产启用**尚未完成在服启用；**部分实现**仍有明确缺口；**外部任务**由独立任务交付；**尚未完成**没有足够验收证据。测试通过、合并、准备候选均不等于用户批准上线。
证据台账见 [overhaul_progress.md](overhaul_progress.md)，操作边界见 [RECOVERY.md](../ops/RECOVERY.md)。以下共 45 项，不以粗略完成百分比替代验收。

## A 线上容量与维护故障

| ID | 可验收工作项 | 当前状态 | 证据、通过条件与关键文件 |
| --- | --- | --- | --- |
| A1 | 有保护清单的镜像 GC 与容量门 | 实测通过（生产） | 移除 36 个无引用 Snow 镜像；保护 30 个摘要引用，空闲约 16.9 GiB；未删容器、卷、数据版本或 Dify。后续仍须实际引用复核并满足至少 10 GiB 容量门。[release_state.py](../ops/release_state.py)、[maintenance.py](../ops/maintenance.py)。 |
| A2 | 修复过期数据清理任务的网络故障 | 实测通过（生产） | 旧 helper `d73e3e2` 使用既有 data 网络；清理仅过期记录，25 个容器 ID/摘要不变。不得把这次结果当作新 helper 已完成升级。[maintenance.py](../ops/maintenance.py)、[test_host_maintenance.py](../tests/test_host_maintenance.py)。 |
| A3 | 共享服务配置漂移与重建控制 | 代码已实现；待生产启用 | 普通 stage 拒绝共享数据库、检索和代理依赖变更；按 Compose 模型及实际容器指纹保留无变化服务。共享升级须独立维护验收。[routing.py](../ops/routing.py)、[maintenance.py](../ops/maintenance.py)。 |
| A4 | 原版本、配置、数据和镜像可找回 | 实测通过（生产备份及只读读回） | 固定 Git tag、root 基线锚点、永久 restic tag；10 镜像/159 blobs 和代码、配置、dump 哈希读回通过。还原能力另见 F5，不能据此称完成裸机恢复。[RECOVERY.md](../ops/RECOVERY.md)、[release_state.py](../ops/release_state.py)。 |

## B CI/CD、发布和回滚

| ID | 可验收工作项 | 当前状态 | 证据、通过条件与关键文件 |
| --- | --- | --- | --- |
| B1 | 删除、重命名及未知路径触发足够 CI | 实测通过（本地）；最终 PR CI 待完成 | 分类器测试覆盖删除/重命名；未知变更保守触发完整检查。main 发布依赖完整门禁，不能仅凭路径快检发布。[classify_changes.py](../scripts/classify_changes.py)、[ci.yml](../../.github/workflows/ci.yml)。 |
| B2 | 真实 PostgreSQL 迁移与旧代码兼容 | 实测通过（隔离 8 组）；新增用例待 CI | 覆盖空库、增量升级、实际旧 Store 读写及崩溃回退、权限隔离和失败 DDL/版本号事务回滚；历史 0001 DDL 冻结。新增第 9 组精确续租 SQL 尚待实库执行；测试角色不代表生产凭据已拆分。[migrations](../migrations)、[test_postgres_integration.py](../tests/test_postgres_integration.py)。 |
| B3 | 构建及实际在服镜像漏洞门禁 | 部分实现 | CI 绑定 public/embedding 摘要，复用 embedding 也重新扫描。实际在服 scanner 已实现，首次实跑失败；`77b8643` 修复 Java DB 磁盘临时目录后待重跑。须保留完整报告/退出码；旧 admin 无 manifest 的差异不能称发布一致。[publish-images.yml](../../.github/workflows/publish-images.yml)、[running_image_scan.md](running_image_scan.md)。 |
| B4 | 成功 main CI 签发可验证发布证明 | 代码已实现；尚未完成在线签发 | 绑定 source/signer SHA、main push、run/attempt 与完整 manifest 哈希；拒绝失败或进行中的后续重跑。最终须用真实 GitHub attestation 验证；main 需稳定一个 CI 周期。[release-proof.yml](../../.github/workflows/release-proof.yml)、[verify_release_proof.py](../scripts/verify_release_proof.py)。 |
| B5 | 候选代码执行前完成可信校验 | 实测通过（故障测试）；待生产启用 | root 快照 inbox 后先调用固定 installed verifier，再 checkout/执行候选；不受信 artifact 不作为可直接执行的脚本。在线 runner 安装与首个真实候选另验收。[project-snow-release](../ops/project-snow-release)、[verify_release_proof.py](../scripts/verify_release_proof.py)。 |
| B6 | 候选回执、当前版本 CAS 和手工晋级 | 代码已实现；待生产启用 | 回执绑定提交、产物、配置、数据与当前版本；基线改变、过期回执或另一候选未处理时拒绝。prepare 不代表批准，不自动切公网流量。[release_manifest.py](../scripts/release_manifest.py)、[release_state.py](../ops/release_state.py)、[promote.sh](../ops/promote.sh)。 |
| B7 | SSE 排空、切换与代理重启保持目标 | 实测通过（隔离）；待生产维护验收 | Caddy reload 保留全部 40 SSE chunks，新连接转 green，重启保持 green；后端接受的聊天可排空最多 300 秒。首次生产持久路由挂载维护尚未验收。[routing.py](../ops/routing.py)、[test_caddy_routing_integration.py](../tests/test_caddy_routing_integration.py)。 |
| B8 | 应用和浏览器旧→新→旧回退 | 实测通过（隔离/浏览器） | 新租约须在停新 worker 后由受信新版维护逻辑终结，应用回退不降 DB schema。浏览器无损兼容目标为补丁版 0.9.6；原始基线既有草稿覆盖缺陷仍保留并明确披露。[test_public_request_recovery.py](../tests/test_public_request_recovery.py)、[compat/README.md](../compat/README.md)。 |
| B9 | 最终 CI、main 合并、签名、候选与晋级闭环 | 尚未完成 | 最终提交通过所选 CI 后，仍依次需要 main 合并、证明签发、服务端验证、候选验收回执及人工晋级；截至本表公网保持 0.9.6。[release-0.10.0-rc.1.md](release-0.10.0-rc.1.md)、[public_deployment.md](public_deployment.md)。 |

## C 后端架构、功能稳定性与安全

| ID | 可验收工作项 | 当前状态 | 证据、通过条件与关键文件 |
| --- | --- | --- | --- |
| C1 | 显式对话核心与公共/本地边界 | 部分实现 | 已有 Context/WorldState/Budget/Result、上下文隔离及纯输出规则；大型 MVP/public facade 中仍有提示、证据选择和复杂守卫待抽取。保持模块化单体，不以多 worker 绕过进程内准入。[ARCHITECTURE.md](../backend/snow_app/ARCHITECTURE.md)、[dialogue_core.py](../backend/snow_app/dialogue_core.py)。 |
| C2 | 真正 HTTP 尝试次数预算和生成准入 | 实测通过（隔离） | 每次 provider POST 前计数；动作最多 1 或 2 次，包含改写；4 活跃/8 排队有上限。模拟 HTTP 行为验证不等于真实账单或线上容量结论。[mvp_service.py](../backend/snow_app/mvp_service.py)、[test_backend_hardening.py](../tests/test_backend_hardening.py)。 |
| C3 | 同步 DB 的有界异步适配 | 实测通过（隔离） | 完整事务放入有界线程池；连接/语句/锁有期限。取消等待不能假定写入回滚或提前释放执行容量。[async_store.py](../backend/snow_app/async_store.py)、[public_store.py](../backend/snow_app/public_store.py)。 |
| C4 | 请求租约、结果未知、取消及写入故障 | 实测通过（隔离） | 10 秒按活任务续租、45 秒失联恢复；处理记录保留一天，正常结果保留 10 分钟；付费后不释放重计费。13 个故障用例覆盖同 UUID 接管、迟到清理和 commit 后回执丢失。[public_main.py](../backend/snow_app/public_main.py)、[test_public_request_recovery.py](../tests/test_public_request_recovery.py)。 |
| C5 | 状态签名、密钥兼容和协议不变 | 代码已实现；生产轮换待验收 | 保持 public v1 / public-state-2，支持当前与前一状态密钥；状态归属和旧读取契约有测试。生产密钥轮换及对应恢复材料必须另行执行，不能从代码存在推断已轮换。[public_security.py](../backend/snow_app/public_security.py)、[test_public_security.py](../tests/test_public_security.py)。 |
| C6 | 整体检索期限与可控降级 | 实测通过（隔离） | 向量、词法、图查询共用 3 秒期限；有限线程、熔断和剩余时限避免无限排队。真实高负载下的延迟分布另见 C8。[public_repository.py](../backend/snow_app/public_repository.py)、[test_backend_hardening.py](../tests/test_backend_hardening.py)。 |
| C7 | 本地监听、Host/Origin、legacy 与上传限制 | 实测通过（隔离） | 默认 loopback；校验 Host/Origin 与写请求 cookie；legacy Agent/Connector 默认关闭。按实际字节限制附件 100 MiB、读取 15 秒，不能只信 Content-Length。[local_boundary.py](../backend/snow_app/local_boundary.py)、[main.py](../backend/snow_app/main.py)。 |
| C8 | 公网并发容量、取消风暴与长稳运行 | 尚未完成 | 需在受控环境量测单 worker 吞吐、排队、p95/p99、DB/检索降级、内存与恢复；现有有界机制和少量并发测试不是容量验收。[ARCHITECTURE.md](../backend/snow_app/ARCHITECTURE.md)、[dialogue_core.py](../backend/snow_app/dialogue_core.py)。 |

## D 数据、RAG、人格与反馈

| ID | 可验收工作项 | 当前状态 | 证据、通过条件与关键文件 |
| --- | --- | --- | --- |
| D1 | 稳定 ID、重复构建与审核决定保留 | 实测通过（隔离） | 重建保留 jobs/candidates/profiles 与人工状态；证据变化形成新 revision，付费结果冲突保留待核对，不自动重试模型。[review_state.py](../pipelines/review_state.py)、[test_review_state_preservation.py](../tests/test_review_state_preservation.py)。 |
| D2 | 跨进程审核并发与原子写入 | 部分实现 | OS 共享锁、短读取修改写入和单文件原子替换已验证；模型请求不持人工审核锁。多文件断电事务、直接编辑器并发不在保证内。[review_lock.py](../backend/snow_app/review_lock.py)、[test_review_concurrency.py](../tests/test_review_concurrency.py)。 |
| D3 | 数据版本保留与复用内容真实性 | 实测通过（合成及真实 Neo4j） | 激活不隐式 GC；Qdrant 比较 ID、payload、维度、距离与归一化向量；Neo4j 比较节点/边属性、标签和端点。真实图服务破坏场景通过；不能仅凭数量复用。[data_loader.py](../backend/snow_app/data_loader.py)、[test_data_release.py](../tests/test_data_release.py)。 |
| D4 | 固定 22 角色离线质量门禁 | 实测通过（合成行为） | 固定 22×8 正反例执行生产规则，覆盖通道、动作、文本清理和检索范围；不是只数 case 数量，也不代表自然度或人物还原获得人工批准。[test_dialogue_quality.py](../tests/test_dialogue_quality.py)、[dialogue_quality_v1.md](../tests/fixtures/dialogue_quality_v1.md)。 |
| D5 | 全角色真实模型人格/RAG评测与成本 | 尚未完成 | 需固定 provider/model、提示和检索版本，评估人物一致性、证据引用、幻觉、拒答及成本；先约定调用预算与验收阈值。TTS 的 300 元不自动授权本项支出。[test_dialogue_quality.py](../tests/test_dialogue_quality.py)、[public_service.py](../backend/snow_app/public_service.py)。 |
| D6 | 反馈隐私、去重、保留和邮件 outbox | 实测通过（隔离）；生产权限拆分待验收 | 反馈脱敏、去重、过期清理与 outbox 重试有回归；未以真实邮件发送或生产凭据轮换作为本轮证据。[feedback_mailer.py](../backend/snow_app/feedback_mailer.py)、[test_feedback_regressions.py](../tests/test_feedback_regressions.py)。 |

## E UI、用户流程与浏览器可靠性

| ID | 可验收工作项 | 当前状态 | 证据、通过条件与关键文件 |
| --- | --- | --- | --- |
| E1 | TypeScript 边界与可复现前端构建 | 部分实现 | persistence/transport/runtime/view/stage 已类型检查、Node 测试及确定构建；大体积 app.js 对话编排仍待拆分。[public_frontend_src/README.md](../public_frontend_src/README.md)、[app.js](../public_frontend/app.js)。 |
| E2 | 摘要取消、期限、checkpoint 与迟到响应 | 实测通过（真实浏览器/合成接口） | 前台操作可取消摘要，保留 checkpoint，更新 request UUID；迟到结果不得覆盖新摘要。服务端中断写终态，不能用变化后的 turns 重放旧 UUID。[transport.ts](../public_frontend_src/src/transport.ts)、[test_public_frontend_reliability.py](../tests/test_public_frontend_reliability.py)。 |
| E3 | Storage 禁用回退和旧版草稿恢复 | 实测通过（真实浏览器） | Web Storage 禁用可运行；保持 IndexedDB v4，保留独立草稿恢复记录，兼容补丁 old→new→old 已验证。原始旧版缺陷见 B8。[persistence.ts](../public_frontend_src/src/persistence.ts)、[compat/README.md](../compat/README.md)。 |
| E4 | 多标签页占用、草稿与请求快照 | 实测通过（真实浏览器） | 验证跨 tab 接管、草稿保存、请求 UUID/snapshot 一致和迟到操作隔离；浏览器关页不是服务端回滚承诺。[runtime.ts](../public_frontend_src/src/runtime.ts)、[test_public_frontend_reliability.py](../tests/test_public_frontend_reliability.py)。 |
| E5 | 1,000/10,000 消息历史与稳定分页 | 实测通过（真实浏览器/合成数据） | 验证长历史、同时间戳排序分页、持久化清理和有限 DOM 渲染；真实用户设备性能仍须后续采样。[view.ts](../public_frontend_src/src/view.ts)、[test_public_frontend_reliability.py](../tests/test_public_frontend_reliability.py)。 |
| E6 | 导入导出原子性与凭据隔离 | 实测通过（真实浏览器） | 导入事务失败可恢复；导出不携带模型 keys 或签名状态，导入未完成请求不自动重放付费调用。[persistence.ts](../public_frontend_src/src/persistence.ts)、[compat/README.md](../compat/README.md)。 |
| E7 | 无障碍、移动布局、缩放与软键盘 | 部分实现 | 已检查焦点/抽屉、1440×900、390×844、原生 200% 缩放和状态栏遮挡；短视口只模拟键盘占位。真实 Android/iOS 软键盘及辅助技术未完成验收。[app.css](../public_frontend/app.css)、[test_public_frontend_reliability.py](../tests/test_public_frontend_reliability.py)。 |
| E8 | build-info 更新提示与安全刷新 | 实测通过（本地/浏览器）；待生产启用 | 对比 revision/app_version，协议版本仅提示兼容；保存草稿和请求快照后在空闲时更新，不强制中断聊天。实际跨发布更新仍待 B9。[public_main.py](../backend/snow_app/public_main.py)、[app.js](../public_frontend/app.js)。 |

## F 运维、开发规范与文档

| ID | 可验收工作项 | 当前状态 | 证据、通过条件与关键文件 |
| --- | --- | --- | --- |
| F1 | 分钟级探测、状态转换及日志隐私 | 代码已实现；待生产启用 | 1 分钟探测、连续 3 次异常告警/恢复；覆盖 DB、retrieval、queue、磁盘 80%/90%、备份超过 26 小时。内部 full 不向公网开放，日志不输出 keys、聊天或完整 Env。[monitor.py](../ops/monitor.py)、[project-snow-monitor.timer](../ops/project-snow-monitor.timer)。 |
| F2 | 外部告警通知维护者 | 尚未完成 | 当前仅本地 journal 状态转换；需维护者选择并配置接收端，再做失败/恢复送达验收。没有接收端不能称已经通知维护者。[monitor.py](../ops/monitor.py)、[RECOVERY.md](../ops/RECOVERY.md)。 |
| F3 | 独立 helper 升级、锁与失败回退 | 实测通过（Linux 故障注入）；生产维护执行中 | 不变 generation、原子 current、receipt 和失败还原通过 19 项 Linux 测试；新 helper 的生产完成须看独立回执，不更新应用/runner 或暗启 auto-stage。支持继承锁的命令校验 FD9；backup 自取锁。[install_maintenance.py](../ops/install_maintenance.py)、[maintenance.py](../ops/maintenance.py)。 |
| F4 | 加密备份、永久 pin 和资源边界 | 部分实现 | 现有基线永久备份已读回；新 helper 的 backup --pin 待实跑回执。pin 不带 daily tag，不被七天 forget 选中；手工任务须显式总时限/CPU/内存限制，不可父持 release 锁后再次获取。[recovery_backup.py](../ops/recovery_backup.py)、[project-snow-backup.service](../ops/project-snow-backup.service)。 |
| F5 | 完整组件恢复、空主机 RPO/RTO | 部分实现 | 隔离 full 恢复 279.41 秒通过：schema0004、8 表、1,148 资源、API 与重建检索；临时资源清理完成。空主机恢复尚未测量，RPO≤24小时/RTO≤4小时仍是目标，生产原地覆盖默认禁用。[restore_drill.py](../ops/restore_drill.py)、[RECOVERY.md](../ops/RECOVERY.md)。 |
| F6 | 开发校验、依赖维护、版本与桌面交付 | 部分实现 | 统一校验、依赖锁/Dependabot、候选说明已提交；API/Web 0.10.0-rc.1、桌面预览 0.5.0、数据媒体独立版本。小吉品牌/图标来源、Electron 安全 smoke 与 Windows 预览通过；可信代码签名和正式桌面发布未完成。[validate_all.ps1](../scripts/validate_all.ps1)、[dependabot.yml](../../.github/dependabot.yml)、[client/README.md](../client/README.md)。 |

## 独立 TTS 与角色舞台

| ID | 可验收工作项 | 当前状态 | 证据、通过条件与关键文件 |
| --- | --- | --- | --- |
| T1 | VoiceLab 导入、目录与成本账本 | 外部任务 | 独立任务负责从旧 voice scripts 导入最新 main 隔离树；不整分支合并陈旧 runtime/UI。目标 `Data/Voice` 原始只读、`App/runtime/voice_lab` 可重建、`App/voice_lab` 代码/文档；本树尚无最后者，不算已迁移。[实施台账](overhaul_progress.md)。 |
| T2 | 全 22 角色及琴诺 morso 语音待审核 | 外部任务；尚未完成交付 | 新发生费用硬上限 300 元；普通角色与 morso 分列，保留可复用样本、来源、参数、成本和失败记录。六类文案/候选对比、可播放文件及完整人工盲审清单齐全才算“待审核”；机器分数不算批准，语音不上线。[实施台账](overhaul_progress.md)。 |
| S1 | 通用全角色舞台载入和失败回退 | 实测通过（合成素材）；待素材后启用 | 校验恰好 22 唯一角色、同源路径、manifest/asset 哈希、来源、批准记录和 motion allowlist；异常回退，中止/低动态偏好有处理。当前 stage_release 固定关闭。[stage.ts](../public_frontend_src/src/stage.ts)、[verify_stage_release.py](../scripts/verify_stage_release.py)、[stage-release.schema.json](../config/stage-release.schema.json)。 |
| S2 | 全 22 角色舞台实物与统一启用 | 外部任务；尚未完成交付 | 画图任务提交完整资源和逐角色人工审核；全部 22 角色验收后统一开。现有 Mia、基础肖像或合成 fixture 均不能代替新素材批准；启用仍须绑定候选回执。[public_frontend_src/README.md](../public_frontend_src/README.md)、[实施台账](overhaul_progress.md)。 |

## 交付边界与后续人审

- 本表证明的是逐项进度，不能作为自动晋级、外部消息发送、素材公开或额外模型消费的授权。
- TTS 独立任务“Project Snow 全角色 TTS 候选制作与待审核交付”负责 300 元硬预算内的账本和本地候选；新声音先本地人审，达到“待审核”仍不等于批准发布。琴诺 morso 不能被普通琴诺样本替代。
- 角色舞台需要全部 22 角色的实际文件、来源和人工批准；校验器只能验证记录与字节，不能替代审美、角色一致性及授权审核。
- 全角色真实模型质量评测、外部告警接收端、真实移动键盘/辅助技术、容量和空主机恢复仍需独立验收；若产生额外费用，先明确本项预算。新证据应关联具体 SHA、环境、报告与时间，不能把隔离测试改写为生产结果。
