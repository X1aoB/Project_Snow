# 在服镜像漏洞扫描

`scripts/scan_running_images.py` 只扫描 `project-snow-public` 项目实际运行的白名单服务。
它读取主机的在服 marker、manifest 和 Compose 镜像 pin，不读取 Git main 或 latest tag。
活跃与候选 API 分别匹配自己的颜色版本；同一 image ID 只扫描一次。Dify 和其他项目不属于扫描范围。

运行前需要 Linux、Python 3.11+、Docker CLI/daemon、root 权限，以及已由维护者审核并安装的
`ghcr.io/aquasecurity/trivy@sha256:...` 完整摘要。工具不会安装或拉取扫描器，也不接受 tag。
集成本轮由 root 查询并选择的版本为 Trivy v0.74.0，index digest 为
`sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969`；
执行者仍须先核实该镜像已在目标主机安装。工具会检查其本地 RepoDigests。

在目标主机用已审核的代码执行，输出文件必须不存在：

```sh
sudo python3 /srv/project-snow/repo/App/scripts/scan_running_images.py \
  --root /srv/project-snow \
  --trivy-image ghcr.io/aquasecurity/trivy@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969 \
  --deadline-seconds 1800 \
  --output /srv/project-snow/running-image-scan-20260907.json
```

本次主机审查还发现 `admin` 使用保留的旧镜像，且没有对应的历史 manifest。
默认命令会拒绝这个部署差异。维护者若已确认只对该实际镜像进行检查，可追加：

```sh
--retained-admin-image ghcr.io/x1aob/project_snow-public@sha256:09d8bee633ed00c03972512e6dac81b095b03c39c43068e8e044dcd896430d03
```

该参数仍要求实际 RepoDigests 精确匹配，仅允许扫描；报告将保留
`identity_status=retained_unbound`、当前期望镜像与指定旧镜像之间的差异。
它不创建历史 manifest、不赋予部署信任，整体门禁不会返回 0。

若代码仍在集成分支，不应改动在服 checkout 来取得脚本；可把已审核的脚本和同目录的
`report_trivy_findings.py` 放入单独的 root 受控工具目录后执行。

扫描固定使用本机 `unix:///var/run/docker.sock`，不会被保存的远端 Docker context 重定向。
扫描会占用现有 release lock，遇到发布/维护操作立即拒绝；不会重启业务服务。
Docker inspect 模板仅返回容器 ID、实际 Image ID、运行状态、项目/服务标签和镜像摘要/大小/平台，
不会读取完整容器 Env 输出。脚本核对 RepoDigests 与部署身份后，以实际 Image ID 执行
`docker image save`。每个临时 tar 的 SHA256 会写入报告，OCI/Docker archive 内的 platform
config SHA 也会与 Trivy 报告绑定；不会假定 containerd index ID 就是平台 config ID。

Trivy 容器使用批准的完整 digest 和 `--pull=never`，不挂载 Docker socket，不连接业务网络，
只读根文件系统、丢弃 capabilities、禁止提权，限制 1 CPU、2 GiB 内存（含 swap 上限）、128 个进程，
临时 `/tmp` 为 512 MiB。扫描器只能读导出的镜像，写私有临时缓存/报告目录。
数据库下载阶段使用默认 bridge 访问官方数据库仓库；实际镜像分析阶段关闭网络。
数据包识别采用 offline 模式，数据库在本次运行中重新下载；它不是对运行容器可写层、宿主机或配置秘密的扫描。

总 deadline 包含发现、导出、漏洞库/Java 数据库下载与扫描，默认 30 分钟，允许 1–60 分钟。
失败或超时最多另用两个 10 秒清理调用，且只删除本工具 UUID 标签和实际 ID 同时匹配的扫描容器。
不存在全局 prune，也不会清理业务容器。默认每镜像最多 4 GiB，下载前至少需要 3 GiB 空闲空间；
逐镜像导出前还会检查 `max(2 GiB, 2 × image.Size + 1 GiB)` 余量。
缓存所在文件系统应有足够临时空间；这些空间检查不是文件系统硬配额。
显式 `--work-root` 和 `--output` 的所有父目录必须为 root 控制、无符号链接且不可被组或其他用户写入；
默认临时目录使用系统安全随机目录机制创建 mode 0700 的工作目录。

退出码与验收：

- `0`：完整扫描结束，运行容器与部署绑定在扫描前后未改变，没有 HIGH/CRITICAL 且已有修复版本的发现。
- `1`：存在上述可修复漏洞，或已显式确认但仍未绑定发布清单的旧 admin 差异。
  无漏洞而存在差异时 `status=identity_drift`；有漏洞时为 `vulnerable`，同时保留 `identity_status`。
  报告保留每个实际在服 image ID、RepoDigests、服务、数据库时间和发现。
- `2`：依赖未就绪、部署身份不匹配、过期数据库、导出/扫描失败、超时或扫描期间部署变化；不能记作扫描通过。

输出文件以 mode 0600 独占创建，不覆盖旧报告。原始 Trivy ImageConfig/Env、包清单、诊断日志不会进入最终报告。
成功完成的脱敏报告同时提供标准 `Results` 字段，可交现有
`report_trivy_findings.py` 复用 HIGH/CRITICAL 可修复漏洞门禁；应先检查本工具的退出码，不能对
`status:error` 的未完成报告或 `identity_drift` 的不一致部署运行下游报告器后宣称通过。
该门禁沿用 CI 的漏洞政策，不代表没有未修复漏洞或低等级漏洞。

依据 [Trivy 官方 image CLI 文档](https://trivy.dev/docs/latest/references/configuration/cli/trivy_image/)
核对 archive input、漏洞等级、忽略未修复项、数据库更新和 timeout 参数。

本轮行为测试覆盖部署摘要不匹配拒绝、实际旧版摘要扫描、Dify 排除、OCI/config 身份差异、
错误报告身份、HIGH/CRITICAL 门禁、数据库过期、下载/扫描失败和超时后只清理自有容器。
这些测试不等于已扫描生产；生产验收需要保留本工具真实生成的完整报告及退出码。
