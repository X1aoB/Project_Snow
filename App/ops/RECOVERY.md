# 小吉终端发布与恢复操作契约

本契约适用于 `/srv/project-snow`、`project-snow-public` 和独立安装的 root runner。其他同机应用（包括 Dify）的容器、网络、镜像和数据不属于发布操作目标。2026-09-07 核查主机为 16 vCPU、约 16 GiB RAM、4 GiB swap、49 GiB 根盘；容量必须读取实时值，不能沿用审查时的空闲数字。

## 安装与权限

先在 root 控制的受审 checkout 中运行 `sh App/ops/install-maintenance.sh`。它按全部模块内容 hash 安装不可变 generation，并原子更新 `/usr/local/libexec/project-snow/current`；保存旧 helper 与 unit 文件以供维护回退。它不更新生产 runner、不切换流量，默认不启用新 timer。已启用的旧 cleanup/backup timer 会在下一次触发时使用新 helper，因此安装前应完成下述备份验证。

`--enable-timers` 启用 cleanup、monitor 和已配置 restic 的 backup timer；`--enable-auto-stage` 单独启用候选拉取，要求 installed runner 与受审源码逐字节相同，且 gh 提供 attestation 的 bundle、signer-workflow、source-ref、source-digest、signer-digest 与 deny-self-hosted-runners 能力。不要为了安装 helper 再跑一次账户/权限 bootstrap；bootstrap 仅用于专门安排的主机迁移或新主机配置。

安装目录只接受 root 控制的源码，发布账户仍只有 inbox 写权限和既有 stage/promote/rollback/status sudo 协议。已安装 `verify_release_proof.py` 独立于 candidate checkout；runner 在 checkout 或执行任何 candidate Python/shell 之前验证 root 快照中的 manifest 和 proof。

## 候选与人工晋级

CI 完整成功后，受限独立工作流签署绑定 manifest bytes、main SHA、CI run/attempt 的 proof，并发布 `candidate-<40sha>` GitHub prerelease 元数据。auto-stage timer 每五分钟只查询最新 main，下载两个固定候选 JSON（每个最多 128 KiB），验证签名与精确 CI 运行后调用既有 runner stage。它不接收任意下载 URL，不自动下载数据/媒体、不执行 promote。

`pending-candidate.json` 保留待验收 SHA；新 main 不会覆盖不同待验收候选。查看 root-only `releases/auto-stage.json` 和 `releases/pending-candidate.json`。失败后补齐精确本地数据/媒体包，再显式执行：

```sh
python3 /usr/local/libexec/project-snow/maintenance.py auto-stage --retry
```

放弃候选必须填写完整 SHA：

```sh
python3 /usr/local/libexec/project-snow/maintenance.py discard-candidate --sha <40sha>
```

此命令仅解除候选保留状态，不删除容器、镜像或恢复锚点。人工部署客户端必须提供 `-ManifestPath` 和 `-ProofPath`；常规发布不需要上传 origin TLS 私钥，只有明确轮换时才提供 `-OriginPrivateKeyPath`。

stage 前先封存两个 colour 的不可变恢复锚点，再执行容量检查。应用与 Docker 文件系统都必须空闲至少 `max(10 GiB, 2 × 预计新增字节)`；估算包括 image 预留及 tar 成员大小。普通 stage 拒绝 embedding、PostgreSQL、Qdrant、Neo4j、egress proxy 的 image/config 变更，候选 API 使用 `--no-deps`。共享服务升级须独立安排备份、兼容性验证和维护窗口；不要为了消除 gate 错误把当前配置 pin 改成候选值。

晋级先完成 candidate 内部 smoke、公网及防火墙/TLS 检查，之后提交 durable colour/manifest/config 状态；现有失败和信号恢复协议仍保留。数据库迁移只允许向后兼容的扩展；应用回滚不会自动执行 Alembic downgrade，也不会隐式还原 PostgreSQL。

## 持久上游与流式连接

新 Caddyfile 从 `/etc/project-snow-routing/upstream.caddy` 导入 `to public-api-<colour>:8000`。该目录是 root 控制的 `/srv/project-snow/runtime/routing` 只读挂载，原子替换发生在目录内，因此 reload 和容器重启都读取同一持久目标。

首次引入这个挂载或修改 Caddy 服务/安全配置时，需要走原有容器重建路径，应在维护窗口验收。之后，只有规范化 Compose 服务模型和已记录的实际容器配置、挂载、网络 ID 指纹都不变，才保留 caddy/cloudflared/egress-proxy；Caddy 使用 reload 切流，其他两个服务无需重建。未知/变更的配置必须重建，不能把“固定镜像 digest 相同”当作完整相同配置证明。

reload 失败先恢复磁盘上的旧上游，既有 promote 回退继续恢复旧路由及状态。新的公共 API 最多排空 300 秒已接受任务，Compose 留出 6 分钟停止余量。旧代码不能主动处理新 lease：回退到缺少 `runtime_capabilities.request_leases` 的版本时，在新版 API 已停止后，用仍受信的新版 image 执行一次性恢复 job，最多等待 46 秒，仅将不确定请求保留为 `generation_interrupted` 终态，不能删除 UUID 或重发付费调用。

真实验收必须运行 `tests/test_caddy_routing_integration.py` 的显式 Docker 模式：旧 SSE 完整跨 reload、新请求进入新 colour、Caddy restart 后仍是新 colour。该测试只创建带随机名称的隔离网络及三个位于 128 MiB/0.5 CPU/pids64 边界内的容器；它不替代首次生产维护的公网验收。

## 不可变恢复锚点和垃圾回收

`maintenance.py anchor` 捕获 colour marker、manifest、config binding、全部 Compose pins 与 public/mailer env，生成 `<sha>-<contenthash>` 只读目录，重复执行幂等。覆盖非活动 colour 前必先归档；根目录保留的历史配置、数据/媒体和 image pins 不会被此工具删除。

```sh
python3 /usr/local/libexec/project-snow/maintenance.py restore-anchor --release-id <sha-contenthash> --colour <inactive-colour>
project-snow-release rollback <inactive-colour> <40sha>
```

prepare 只写非活动 colour，标记最后提交，先校验配置内容及共享依赖 pins。不能将锚点放回正在服务的 colour；共享依赖不同须先单独还原依赖。旧格式基线锚点仍原样备份；无法解释其引用时 `gc-plan` **拒绝继续**，不能通过忽略未知目录来清理镜像。

`gc-plan` 保护所有应用的运行和停止容器、Docker 容器引用计数（包括共享 runnable manifest 的 OCI index）、所有已知 release/runtime/anchor pins。只有全部标签都属于 Snow GHCR 仓库、且无引用的 image ID 可成为候选。未标记、混合标签、Dify 和未知镜像都被排除。

```sh
python3 /usr/local/libexec/project-snow/maintenance.py gc-plan
python3 /usr/local/libexec/project-snow/maintenance.py gc-remove --image-id sha256:<explicit-full-id>
```

删除前会逐项重新扫描；不使用 forced removal 或任何全局 prune。镜像显示的 Size 含共享层，不能相加当作预计可回收空间。

## 备份范围、基线与恢复演练

daily backup 使用当前 PostgreSQL digest 对应容器生成一致 custom dump，以 `pg_restore --list` 检查并记录 SHA-256；restic 加密保存 dump、全部 releases/anchors/runtime、部署 repo、不可变 data/media、`/etc/project-snow` 和已安装 helpers/runner/sudoers。发布锁固定配置与 release 引用；immutable 包直接由 restic 读取，不先复制到根盘。

只有 `restic check` 成功才执行七天日常保留策略，只有整次备份成功才更新 root-only `backups/last-success.json`。每日 tag 为 `project-snow-production`。一次性 `backup --pin` 使用 `project-snow-pinned-<40sha>`，不附日常 tag，保持到维护者显式退休；它不会被日常 `forget --tag project-snow-production` 选中。

2026-09-07 在独立生产维护中建立的 0.9.6 基线使用永久 tag `project-snow-pinned-502ec99412bef843c37e4b31a53df8fa9faeb33c`。对应 restic snapshot：`828b0a9f`（配置/数据/媒体/DB）、`48398acd`（10 个 Snow 在用及回退镜像归档，约 1.179 GiB）、`fd81b13a`（元信息和约 9.5 MB 基线源码 zip）。以 restic 实际查询结果为准，操作 snapshot tag 会改变 snapshot ID。

基线镜像归档通过 `docker save | gzip | restic --stdin` 流式创建，无镜像 tar 磁盘中转，独立于每日备份。每日备份记录精确 registry digests，不重复导出镜像；恢复基线可以使用上述 image archive，恢复之后的新版本若未另行归档，需要保留的 GHCR digest。每个决定长期保留的发布应另行封存相应镜像与 pin 备份，不要声称 daily dump 自身能脱离 registry 完成裸机恢复。

Qdrant/Neo4j 是由不可变 data release 重建的派生索引；它们不通过正在运行的数据卷裸拷贝备份。裸机恢复必须先恢复同版本共享服务，再用被还原受信 checkout 的 data loader 加载并核对精确版本/集合/graph 指针。

演练按顺序执行：

1. 在隔离目录从选定 snapshot 恢复，核对 recovery JSON、dump checksum、源码、配置、数据/媒体与 image archive 清单。只在隔离 Docker 环境加载测试镜像和数据库。
2. 验证同版 `pg_restore --list`，恢复并执行只读行数/schema 校验、内部 API、检索和媒体 smoke；确认不知道生产模型密钥的测试不会发起付费模型请求。
3. 使用 `python3 App/ops/restore_drill.py` 恢复固定基线到全新随机私有目录和专用 `--internal` Docker 网络；先校验 dump/配置/源码及数据媒体 hash，再向独立 PostgreSQL 写入，核对 dump TOC、schema、约束、migration head 并生成实际行数 receipt。基线 API 不发布任何宿主端口，检查仅通过本次容器 ID 执行固定的内部 loopback readiness/full GET。`--full` 另启动隔离 embedding/Qdrant/Neo4j 并重建检索索引。所有容器总限额为 3.75 GiB RAM / 3 CPU，前后容量门要求至少 10 GiB 空闲。
4. 原 `maintenance.py restore-postgres` 和包装脚本保留明确拒绝路径，**不会覆盖生产数据库，即使 writers 已停止**。灾难恢复须先在独立目标验证，单独审阅上线后新增反馈/数据如何保留与合并，再决定切换。演练只回收自己创建的容器 ID、volume 和网络，不执行生产切换。

含新版 request lease 的备份必须在独立恢复目标选择匹配的受信新版 image/anchor；不能用旧镜像强制消除新 lease 或删除不确定请求。基线未捕获历史行数，因此演练只能核对 dump/schema 自洽并记录恢复结果，不能声称已经比对历史行数。

首次执行应将受审的 `restore_drill.py`、`maintenance.py`、`release_state.py` 一起复制到 root 控制目录，再通过 `systemd-run --wait --collect -p EnvironmentFile=/etc/project-snow/restic.env -p UMask=0077 -- /usr/bin/python3 /受控目录/restore_drill.py` 运行。receipt 位于 root-only `backups/restore-drills/`；日志只给出状态和路径。源码 zip 使用独立核验的固定 SHA-256，因为原 `sha256.json` 生成早于源码归档。Docker 创建超时也会按本次随机名称与 label 找回未返回 ID 的资源；归属不符时保留私有目录并报告失败，不能扩大清理范围。

restic 环境由 root-only `/etc/project-snow/restic.env` 注入；手工任务可使用 `systemd-run --wait --collect -p EnvironmentFile=/etc/project-snow/restic.env -- /usr/bin/python3 /usr/local/libexec/project-snow/maintenance.py backup --pin`。不要将环境内容贴进日志或工单。

## 最小监控

monitor timer 每分钟读取公网 live 及活动 API localhost full 健康（DB、embedding/Qdrant/Neo4j、generation queue、draining），不调用 LLM、不导出 provider/DB 凭据。公网/依赖/队列连续三次异常才发生告警转换，成功立即记录恢复。磁盘 80% warning、90% critical；最近成功备份超过 26 小时或记录缺失为 critical。

状态保存在 root-only `runtime/monitor-state.json`。仅状态变化输出含 severity/check/previous_state/state 的 JSON journal；无变化保持安静。当前没有配置外部通知渠道，因此只能声称“本机记录告警”，不能声称维护者已收到邮件或消息。查看 `journalctl -u project-snow-monitor.service`、`systemctl list-timers 'project-snow-*'` 和状态文件。

## 本次验证记录（2026-09-07）

本地运维回归包含 112 项测试与 33 个既有故障子测试；Windows 不支持的符号链接安装器测试和显式 Docker 集成测试在便携套件中跳过。后续新增候选 hash/HEAD 和 lease 操作边界另通过 32 项定向测试。所有 ops shell 及 deploy.ps1 完成语法检查。

独立 Docker 实测通过：旧 SSE 的 40 段跨 reload 完整返回、新连接切到 green、Caddy restart 后仍读取持久 green 上游（7.37 秒）。实际运行的 Caddy 二进制也只读校验了新配置语法。后端 8 个 PostgreSQL 场景已实跑，其中新 worker 崩溃到旧版显式 lease 恢复场景通过。

永久基线镜像 snapshot 回读核对 10 个镜像、159 个 blobs、1,296,395,349 字节归档内容；源码 zip、配置 tar 和 PG dump 均与记录 hash 一致。这些证据证明备份可读取、文件和镜像材料完整，尚不代表完成 Docker load 或整台主机的灾难恢复演练。

首次隔离演练实际恢复了 8 张 PostgreSQL 表，migration head 为 `20260819_0004`，无未验证约束；data 11、avatar 47、sticker 1090 个文件全部通过 hash 校验。该次 API 检查因 internal 网络没有宿主端口而失败，所有临时资源成功清理；已改为容器内部 loopback 检查，完整 API/检索恢复仍须重跑后记录，不能把这次结果标成通过。
