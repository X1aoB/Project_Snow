# 1.2.0 发布修复与本地验证

基于 `a58c2be44831dd987d0c8109266142b813cd20b3`。以下是发布修复的本地证据，不是生产验收或上线完成记录。

## 镜像安全

`required-checks` [42568304700](https://github.com/X1aoB/Project_Snow/actions/runs/42568304700) 的发布扫描报告旧 embedding 中 `apt`、`libapt-pkg7.0` 和两项 `zlib1g` 高危发现。更新 `SYSTEM_SECURITY_REVISION` 强制重建系统升级层；`trivy-exceptions.json` 仍为空，模型 ID、revision 和维度不变。新镜像的漏洞消除与向量一致性必须以 PR 和 main 的实际构建结果为准，本机没有 Docker，未在本机作出这两项通过结论。

## 标签页接管

旧流程在 1.5 秒后恢复按钮，并用按钮的 `disabled` 判断是否处理后续 `released`，因此慢持久化会丢失接管请求；通知与超时并发还可能重复申请锁。

接管现在有独立请求状态：Web Locks 使用可取消的排队请求，实际锁释放后继续；租约路径串行等待，释放通知只负责唤醒。15 秒未获得写权限会取消等待并明确提示，重复通知不会重新申请已持有的锁，释放持久化也不会并发执行。原接管测试已移出 `TIMING_SENSITIVE`，恢复并行执行。

新增浏览器回归在 IndexedDB 的真实事务完成事件上设置门闩，延迟持久化完成超过旧 1.5 秒窗口，验证 Web Locks 和租约路径均无需第二次点击即可恢复草稿；另覆盖重复释放、超时撤销排队和显式重试。将同一回归指向原始 `a58c2be4` 的 `app.js` 时，两种锁路径均失败；修复后通过。

## CI 稳定性

- Electron 安装下载最多尝试三次，间隔 10/20 秒，步骤总上限 8 分钟。依赖安装阶段关闭隐式二进制下载，让所有 Electron 下载进入这个受限步骤。三次仍失败时保留失败退出码；不重跑测试。用合成安装结果验证了一次成功、两次失败后成功、三次失败的尝试次数和退出码。
- 原舞台测试在多行缩回单行后的固定 200 毫秒处读取布局。输入框已更新，但 `ResizeObserver` 尚未把高度写入 `--snow-composer-h`，导致对白与立绘使用旧高度。故障注入延迟该回调，复现了原 CI 的准确 72px 差值（三行 × 24px）；回调恢复后几何尺寸完全恢复，页面错误为零。测试改为等待字体就绪及观察值与当前布局同步，再执行原始严格像素断言；未放宽断言或更换测试通道，未修改冻结的 `app.css`。

## 本地结果（2026-11-09）

- `test_public_frontend_reliability.py` 与 `test_snow_adv_features_ui.py`：Chrome，`pytest -n 2 --dist loadfile`，42 项与 26 个子测试通过。
- 部署、前端发布契约、Snow ADV 部署、embedding 向量比较、Trivy 报告及变更分类测试：151 项与 129 个子测试通过，1 项跳过；本机缺少 jq 的 `test_stale_origin_probe_cleanup_is_exact_and_fail_closed` 未运行，仍由 CI 执行。
- `node --check public_frontend/app.js` 与 `git diff --check` 通过。首屏体积证据已重新生成，仍满足原预算。

后续门槛仍包括签名提交、完整 CI、真实 PostgreSQL old/new/old、新镜像扫描与向量比较、主机双 colour 备份和锚点、运行时探针与替换授权、精确候选验收、所有者回执批准、生产晋级和上线核对。

## main 暴露的第二个观察回调分支

PR #119 的首轮完整 CI 通过后，合并提交 `b3c1acbc` 的首轮 [main 检查](https://github.com/X1aoB/Project_Snow/actions/runs/42595123280) 在同一舞台测试的 dusk 场景失败：提示条底部为 610px，请求状态标签顶部为 568px，两者重叠。此时 composer 与 dialogue 高度已经正确，单独出现的请求标签尚未通过其 ResizeObserver 回调更新 `--snow-toast-b`；只等前两项高度不足以确认完整布局已同步。

等待条件现同时核对 composer 高度、dialogue 高度、包含请求标签高度的 toast 偏移及字体完成状态，原来的位置和尺寸断言保留。新增回归通过门闩分别延迟 textarea 缩回与请求标签出现时的真实观察回调：延迟期间必须判定未就绪，释放后必须恢复精确尺寸及提示条间距。该失败运行不重跑，使用修复后的新提交重新走发布门槛。

补充验证：旧等待条件在新增回归中确定性失败；完整等待条件的定向测试 2 项、3 个子测试通过。相关功能、接管、输入网络与场景四份浏览器套件以 `-n 4 --dist loadfile` 执行，56 项、340 个子测试通过。
