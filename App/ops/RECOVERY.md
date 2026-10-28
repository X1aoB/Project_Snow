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

`baseline-502ec99-0.9.6-20260907` 的五文件历史快照可由只读兼容校验器识别。它核对完整 0.9.6 commit、marker、manifest、配置内容及容器清单，将清单内所有应用的镜像 ID 和基线镜像 digest 一并加入保护集合；不转换、移动或删除原始资料。缺失文件、身份不一致、无法解析的镜像或其他未知旧格式仍会中止清理。只有完成严格核验后的 `gc-plan` 可作为逐镜像清理依据，空间调查报告不能代替该检查。

`gc-plan` 保护所有应用的运行和停止容器、Docker 容器引用计数（包括共享 runnable manifest 的 OCI index）、所有已知 release/runtime/anchor pins。只有全部标签都属于 Snow GHCR 仓库、且无引用的 image ID 可成为候选。未标记、混合标签、Dify 和未知镜像都被排除。

```sh
python3 /usr/local/libexec/project-snow/maintenance.py gc-plan
python3 /usr/local/libexec/project-snow/maintenance.py gc-remove --image-id sha256:<explicit-full-id>
```

删除前会逐项重新扫描；不使用 forced removal 或任何全局 prune。镜像显示的 Size 含共享层，不能相加当作预计可回收空间。

## 稳定回滚基线与可逆退休

保留策略（所有者决定）：主机只长期保留 (a) 活动 colour 的发布、(b) 非活动 colour 的发布（一步回滚）、(c) 最近 **2** 个稳定基线。其余锚点一律**退休**——移出受 GC 保护的命名空间、此处绝不删除——仍可由 restic + GHCR digest 恢复。退休项存放在 `/srv/project-snow/retired/`（`releases/`、`runtime/` 的同级同盘目录），因此 `protected_image_references` 的 `releases.rglob` / `runtime.rglob` 扫描不再看到它们，而 `anchor-unretire` 可原子移回。

基线注册表是 root-only 的 `releases/baselines/registry.json`（0600，目录 0700）。`active` 为当前基线（newest-first，至多 2 个），`retirable` 为被挤出活动窗口、可退休的旧基线。

- 版本规则：bug-fix 发布第三位 +1；feature 发布第二位 +1、第三位归零；第一位只由所有者拍板。基线只接受 **x.Y.0**（feature 发布）；`1.0.0` 因自 2026-09-09 起被多个构建复用而**不可信**，只对祖父基线 id `0c27109f1b02fbbcb409366f5fb1bbf7c850d2d7-6dcfa1b28547eb8d875b6912f9b27b1e4b0745e77f3ad5ecea325e96dc4c7681`（最后一个 1.0.0 发布）接受。
- `baseline-mark` 幂等；标记第 3 个基线会把最旧的活动基线降级为 `retirable`（绝不删除）。
- `anchor-retire` 拒绝活动 colour 锚点、非活动 colour 锚点、`releases/anchors/latest.json` 引用的锚点、以及任何**当前**基线；`latest.json` 必须正好指向两个 colour 当前发布的锚点（晋级后它仍指向旧占位者，需先跑一次 `anchor`），否则拒绝；并要求具体备份前提（`backups/last-success.json` 在锚点创建之后成功完成）。遗留 0.9.6 基线 `baseline-502ec99-0.9.6-20260907` 不是两个稳定基线之一，走同一机制退休但免备份前提（已有永久 pinned restic snapshot）。`anchor-unretire` 为其逆操作。
- `runtime/origin-edge/<sha256>.compose.env` 是常驻 origin-edge 监听器的快照，promote.sh 只读取 `releases/live-origin-edge-config.json` 指向的那一份。`origin-edge-retire` 校验该绑定（schema、路径、字节哈希）后，退休其余全部快照；`origin-edge-unretire --name` 逐个移回。
- GC 保护：origin-edge 快照只保护 `CADDY_IMAGE`——origin-edge 服务只运行这个镜像，promote.sh 用快照执行的 compose 命令也只针对 origin-edge；快照里的其他镜像键是拍摄时 slot 环境的副本，不再固定旧 API/embedding 镜像。colour slot、`runtime/compose.env`、锚点和 manifest 的保护不变。
- `retired/` 纳入每日与 pinned 备份，退休后的记录在之后的快照里同样可恢复。

常规 stage/promote/rollback 代码路径不变：本节新增 `maintenance.py` 子命令与 `release_state.py` 函数；`archive_colours`/`restore_colour`/`delete_planned_images` 不变，`protected_image_references` 只收窄了 origin-edge 快照一处，备份只多纳入 `retired/`。

所有者批准的主机操作顺序（★ 为所有者审批点；只有 `gc-remove` 不可逆，被删镜像可按 digest 从 GHCR 重新拉取）：

```sh
# 1. 安装受审的新 helper generation（含本节修复，使 gc-plan 不再在 0.9.6 基线上报“Unknown recovery anchor format”）
sh App/ops/install-maintenance.sh                       # 可逆：旧 generation 由安装收据保留
# 2. 刷新锚点索引（幂等），并做一次成功备份
python3 /usr/local/libexec/project-snow/maintenance.py anchor
systemctl start project-snow-backup.service             # 与每日备份同一单元（带 restic 环境文件）
# 3. ★ 标记基线（1.0.0 祖父基线；之后每个 x.Y.0 晋级后标记）
python3 /usr/local/libexec/project-snow/maintenance.py baseline-mark --release-id <anchor-id>   # 可逆：仅写注册表
python3 /usr/local/libexec/project-snow/maintenance.py baseline-list                           # 只读核对
# 4. 干跑退休集（只读：锚点、遗留基线、origin-edge 快照、索引是否新鲜）
python3 /usr/local/libexec/project-snow/maintenance.py retire-plan
# 5. ★ 逐一可逆退休
python3 /usr/local/libexec/project-snow/maintenance.py anchor-retire --release-id <id>   # 可逆：移到 retired/anchors
python3 /usr/local/libexec/project-snow/maintenance.py origin-edge-retire                # 可逆：移到 retired/runtime-origin-edge
# 6. 生成 GC 计划并逐 id 删除（★ 最后一步不可逆）
python3 /usr/local/libexec/project-snow/maintenance.py gc-plan
python3 /usr/local/libexec/project-snow/maintenance.py gc-remove --image-id sha256:<explicit-full-id>
```

退休写错时用 `anchor-unretire --release-id <id>` 原子移回，再重跑 `gc-plan`。只有确认目标镜像确实只被已退休锚点/快照固定后，才执行 `gc-remove`。

## 共享 embedding 镜像的安全替换

共享 embedding 容器由 blue/green 两个 colour 共用，普通 stage 仍拒绝其 image 变更。只有在模型 id/revision/维度均不变、仅为修补依赖（如 CVE 修复重建）的场景，才走本节的受控替换；它在 promote 时原子重建共享 embedding 容器，并在失败时自动回退，marker/manifest/current/compose.env 始终与运行中的容器一致。替换期间共享 embedding 重启约 1-2 分钟，两个 colour 的在线会话都会短暂检索失败，必须安排在低流量维护窗口。root runner `ops/project-snow-release` 与 `bootstrap-release-runner.sh` 不变。

替换要素是一份 root 写入、按目标 digest 命名、与一次性探针回执绑定的授权文件 `/etc/project-snow/embedding-swap/<新 digest hex>.env`（目录 root 0700，文件 root 0600，无符号链接）：

```
schema=project-snow-embedding-swap-1
from=<image@sha256:...>   # 必须等于当前运行的 embedding digest
to=<image@sha256:...>     # 候选 embedding digest
approved_at=<ISO>
probe_sha256=<探针回执字节的 sha256>
```

`shared_dependency_gate` 只有在**唯一**变更项是 `EMBEDDING_IMAGE`、且存在 from=当前运行、to=候选 的匹配授权时，才返回 `shared_dependencies: embedding-swap-authorized`；其余任何共享 image/config 变更仍硬性拒绝。stage 仍只拉取新 embedding 镜像、绝不重建 embedding 容器，候选 API 仍以 `--no-deps` 对运行中的 embedding 启动。

完整流程（★ 为所有者审批点）：

1. 先合并独立的打包 PR（Dockerfile pin、CI 旧-新对比），main 的 manifest 随之携带新 embedding digest。
2. ★ 所有者对两个 colour 执行 `backup --pin` 与 `anchor`，并确认新 embedding 镜像已 `docker pull` 到主机。
3. ★ 所有者运行一次性探针（上传后在主机以 root 执行）：

   ```sh
   systemd-run --wait --collect -p UMask=0077 -- /usr/bin/python3 /受控目录/embedding_probe.py --from <运行 digest> --to <候选 digest>
   ```

   探针以 `--network none --read-only --cap-drop=ALL --memory 1536m` 无端口启动候选镜像，等待容器内 `/health` 维度 512，再对固定样本分别调用候选容器与**在线** embedding 容器的 `/embed`（均通过 `docker exec` 打本地 127.0.0.1:8000，不向线上容器加载第二个模型），比较 512 维向量（最小 cosine ≥ 0.99999，报告最大绝对差），始终删除探针容器，并在 root-only `/root/release-1026/` 写入回执 JSON、打印其 sha256。
4. ★ 所有者据通过的回执写授权（二选一，二者等价且都校验 from=运行 digest、回执为 root 0600、状态 pass）：

   ```sh
   /usr/bin/python3 /受控目录/embedding_authorize.py --receipt /root/release-1026/probe-<新digest>.json
   # 或
   python3 /usr/local/libexec/project-snow/maintenance.py embedding-swap-authorize --from <运行> --to <候选> --probe-receipt <回执路径>
   ```

5. `project-snow-release stage <inactive> <sha>`：gate 因授权自洽通过；API 拉取新镜像，embedding 容器尚未重建。
6. candidate 验收照常：`candidate_acceptance prepare` → ★ 所有者 `approve`。
7. `project-snow-release promote <inactive> <sha>`：在 API 就绪/smoke 与边缘切换全部通过后、持久写 compose.env/current 之前，若运行中的 embedding 与目标 marker 的 digest 不同，promote 先校验匹配授权并确认目标镜像已在本机（`docker image inspect`），再 `compose up -d --no-deps --force-recreate embedding`，等待维度 512 健康并做一次 `/embed` 探活；任一失败则按上一份 compose.env 重建回旧 digest、等待健康并恢复旧运行态、非零退出且不提交切换。成功提交后删除授权文件（`embedding-swap-clear`），授权无法重放。embedding 未变的普通发布完全不进入该分支，走与此前逐字节相同的切换路径。

清除未消费的授权：

```sh
python3 /usr/local/libexec/project-snow/maintenance.py embedding-swap-clear
```

回滚语义：回滚到某个 marker 记录了不同 embedding digest 的 colour 时，promote 的 rollback 模式会把共享 embedding 容器重建到**该 colour 签名 manifest 里记录的**那个 digest（同样的健康等待与失败回退），无需新授权（该 digest 此前已被晋级、已在签名清单中），重建前同样先 `docker image inspect` 确认镜像在本机。`restore_colour` 允许锚点与运行 compose.env **仅** `EMBEDDING_IMAGE` 不同（随后的 rollback/promote 完成容器切换，使 marker/manifest/current/compose.env 一致）；其余任何共享 key 不同仍拒绝。旧镜像由锚点保护不被 GC，替换前应确认仍在本机。

## 共享 egress 配置（egress-squid.conf）的安全替换

共享 `egress-proxy` 由 blue/green 共用，bind-mount 主机部署树里的 `infra/egress-squid.conf`（`compose.prod.yml` 中 `./infra/egress-squid.conf:/etc/squid/squid.conf:ro`，compose 项目 `project-snow-public`，镜像 `EGRESS_PROXY_IMAGE`）。该文件在 `maintenance.py` 的 `SHARED_CONFIG_PATHS`、`release_state.py` 与 `deploy.sh` 的 `base_configuration_paths` 中，属共享配置，普通 stage 对其内容哈希变更一律硬拒绝。放行第三方中转站（relay）需要用 variant B 的 squid 配置（先拒私网/回环/链路本地/CGNAT/ULA/metadata 目标，再放行公网 CONNECT 443），这是一次共享配置变更，须走本节的受控 egress-config swap，完全镜像 embedding swap。

替换要素是一份 root 写入、按候选配置 sha256 命名、记录 from→to 内容哈希与 `squid -k parse` 通过状态的授权文件 `/etc/project-snow/egress-swap/<候选conf的sha256>.env`（目录 root 0700，文件 root 0600，无符号链接）：

```
schema=project-snow-egress-swap-1
from_sha256=<当前运行 colour 配置绑定里 egress-squid.conf 的 sha256>
to_sha256=<候选 colour manifest 里 egress-squid.conf 的 sha256>
approved_at=<ISO>
parse_ok=true
```

`shared_dependency_gate` 只有在**唯一**变更项是 `infra/egress-squid.conf`（按 `configuration_sha256` 比较）、且存在 from=当前、to=候选 的匹配授权时，才返回 `shared_dependencies: egress-config-swap-authorized`；其余任何共享 image/config 变更仍硬性拒绝。

完整流程（★ 为所有者审批点）：

1. 合并包含新 `infra/egress-squid.conf` 的 PR，main 的 manifest 随之携带新 conf 哈希。
2. ★ 所有者对两个 colour 执行 `backup --pin` 与 `anchor`（新 conf 哈希随配置绑定自动被锚定，`restore-anchor` 按绑定恢复该哈希，无需新恢复逻辑）。
3. ★ 所有者先对候选 conf 跑 `squid -k parse` 确认通过，再据此写授权：

   ```sh
   python3 /usr/local/libexec/project-snow/maintenance.py egress-config-swap-authorize --manifest <候选manifest> --parse-ok
   ```

   该命令从 live 配置绑定读 from、从候选 manifest 读 to，二者必须不同且均为合法 sha256。
4. `project-snow-release stage <inactive> <sha>`：gate 因授权自洽通过。
5. candidate 验收照常：prepare → ★ approve。
6. `project-snow-release promote <inactive> <sha>`：在 API 就绪/smoke 与边缘切换全部通过后、持久写之前，若目标 colour 的 egress conf 哈希与当前运行的不同，promote 先校验匹配授权，再对候选 conf 做 `squid -k parse`，随后 `compose up -d --no-deps --force-recreate egress-proxy`，并从 API 容器 CONNECT 探测三个官方厂商主机（必须 200）与一个私网目标 169.254.169.254（必须被拒）。任一失败则重建回上一个 colour 的 conf 并恢复旧运行态、非零退出且不提交切换。成功提交后删除授权文件（`egress-config-swap-clear`），授权无法重放。egress conf 未变的普通发布完全不进入该分支。

清除未消费的授权：

```sh
python3 /usr/local/libexec/project-snow/maintenance.py egress-config-swap-clear
```

回滚语义：回滚到某个 colour 时，promote 的 rollback 模式按该 colour 签名配置绑定里记录的 conf 重建 `egress-proxy`，无需新授权。因为「加放行项」对现有主机是纯超集，blue/green 两个 colour 各自看自己的 conf 都能正常工作，切换与回退都干净；但授权+哈希绑定+锚点步骤仍是强制的——`verify_candidate`/`verify_promoted` 断言 active marker、manifest、compose.env 与配置绑定一致，`configuration_sha256` 内容寻址，改了 conf 却不更新记录哈希会使校验失败。

**PUBLIC_ENABLED_PROVIDERS 两 colour 必须一致**：`deploy.sh` 规范化器的允许列表正则已拓宽为 `openai|deepseek|dashscope|zhipu|moonshot|anthropic|xai|gemini|relay`。但 `verify_candidate.py` 断言两 colour public env 仅 `PUBLIC_APP_VERSION` 不同，`verify_promoted.py` pin 精确 `public_environment_sha256`，所以厂商放行变更必须对两 colour **逐字相同**地应用，并重新 pin `verify_promoted.PUBLIC_ENVIRONMENT`，否则下次 promote 会在 colour-diff 校验上失败。

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

PostgreSQL 就绪和恢复连接都固定使用容器内 TCP `127.0.0.1`，避免初始化阶段的临时 Unix socket server 被误判为最终服务。失败 receipt 只记录固定 phase/命令名称；原始 stderr 位于同次 `run-*.diagnostics/` 的 root-only 文件中，每种命令最多保留最新 1 MiB，不输出参数或日志正文。诊断可能含私有数据，只能由 root 在受控环境排障；成功演练删除临时诊断。

restic 环境由 root-only `/etc/project-snow/restic.env` 注入；手工任务可使用 `systemd-run --wait --collect -p EnvironmentFile=/etc/project-snow/restic.env -- /usr/bin/python3 /usr/local/libexec/project-snow/maintenance.py backup --pin`。不要将环境内容贴进日志或工单。

## 最小监控

monitor timer 每分钟读取公网 live 及活动 API localhost full 健康（DB、embedding/Qdrant/Neo4j、generation queue、draining），不调用 LLM、不导出 provider/DB 凭据。公网/依赖/队列连续三次异常才发生告警转换，成功立即记录恢复。磁盘 80% warning、90% critical；最近成功备份超过 26 小时或记录缺失为 critical。

状态保存在 root-only `runtime/monitor-state.json`。仅状态变化输出含 severity/check/previous_state/state 的 JSON journal；无变化保持安静。当前没有配置外部通知渠道，因此只能声称“本机记录告警”，不能声称维护者已收到邮件或消息。查看 `journalctl -u project-snow-monitor.service`、`systemctl list-timers 'project-snow-*'` 和状态文件。

## 本次验证记录（2026-09-07）

本地运维回归包含 112 项测试与 33 个既有故障子测试；Windows 不支持的符号链接安装器测试和显式 Docker 集成测试在便携套件中跳过。后续新增候选 hash/HEAD 和 lease 操作边界另通过 32 项定向测试。所有 ops shell 及 deploy.ps1 完成语法检查。

独立 Docker 实测通过：旧 SSE 的 40 段跨 reload 完整返回、新连接切到 green、Caddy restart 后仍读取持久 green 上游（7.37 秒）。实际运行的 Caddy 二进制也只读校验了新配置语法。后端 8 个 PostgreSQL 场景已实跑，其中新 worker 崩溃到旧版显式 lease 恢复场景通过。

永久基线镜像 snapshot 回读核对 10 个镜像、159 个 blobs、1,296,395,349 字节归档内容；源码 zip、配置 tar 和 PG dump 均与记录 hash 一致。这些证据证明备份可读取、文件和镜像材料完整，尚不代表完成 Docker load 或整台主机的灾难恢复演练。

完整隔离恢复于 **2026-09-07 09:39:42 UTC** 通过，耗时 **279.41 秒**：8 张 PostgreSQL 表、migration head `20260819_0004`、零未验证约束；data 11、avatar 47、sticker 1090 个文件全部通过 hash 校验，独立 embedding/Qdrant/Neo4j 完成索引重建，基线 API 的 readiness/full 全部正常。临时容器、网络、卷全部清理，生产数据库未被覆盖，也未调用模型或邮件服务。

原始回执为 `/srv/project-snow/backups/restore-drills/run-d0303d777c9b471289feb96e33ccae42.json`，SHA256 `a6de02e4ae035f67754aec0bb7f865a1f05b40c60a8825105ac15ead50e13780`。前两次失败促成容器内 loopback 探测及 PostgreSQL 最终 TCP 就绪修复，失败回执仍保留。这是组件恢复验证，**不是空主机恢复/RTO 验收，也没有历史行数基准可逐项比对**。
