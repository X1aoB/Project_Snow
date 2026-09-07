# 候选验收与人工晋级

`stage`、技术检查、准备回执和人工批准是四个独立步骤。自动任务最多准备候选，
不能签署人工批准。常规晋级必须在同一发布锁内重新核对批准；应急回滚继续使用保留的
原版本身份，不依赖互联网或新回执。

候选通过私有 SSH 通道验收后，维护者把实际检查结果保存到 root 私有证据文件：

```json
{
  "schema_version": "project-snow-candidate-evidence-1",
  "commit_sha": "候选完整40位SHA",
  "expected_current": "当前完整40位SHA",
  "checks": {
    "api_health": true,
    "build_identity": true,
    "data_media": true,
    "browser_smoke": true,
    "rollback_baseline": true
  },
  "reports": ["实际检查记录及对应哈希"]
}
```

这些布尔值应来自完成的检查，不能因脚本需要而预填为通过。`browser_smoke` 是候选浏览器
流程检查，不表示真实模型人格质量或未批准素材已通过。证据文件不得含密钥、聊天正文或用户资料。

以下命令中的 SHA 和回执 ID 必须替换为实际完整值。脚本只在 Linux root 下执行，使用既有发布锁；
不要在已持锁的父进程内运行 `prepare` 或 `approve`。

```sh
python3 /srv/project-snow/repo/App/ops/candidate_acceptance.py prepare \
  --colour green --sha CANDIDATE_SHA --expected-current CURRENT_SHA \
  --evidence /root/candidate-review/evidence.json
```

返回 `ready_for_review` 和回执 SHA256。回执保存于
`/srv/project-snow/releases/acceptance/<receipt-id>.json`，以独占方式写入、校验值寻址，
最长有效 24 小时。它绑定当前与候选的完整 marker、manifest、配置绑定、运行环境哈希、
候选预留记录、stage nonce、两色实际容器 ID 和镜像 ID。候选还必须通过 loopback
`build-info` 的完整 revision 和 app_version 校验。元数据不会输出秘密配置内容。

维护者审核具体回执并明确批准后，才运行：

```sh
python3 /srv/project-snow/repo/App/ops/candidate_acceptance.py approve \
  --receipt-id EXACT_RECEIPT_SHA256 --expected-current CURRENT_SHA
sudo /usr/local/sbin/project-snow-release promote green CANDIDATE_SHA
```

准备和批准都会重新读取实际运行身份。晋级脚本在首次启动、重建或切换服务前，
在继承的 FD9 发布锁内再次核对回执、批准和预期当前版本。数据库或 GitHub故障不是绕过依据。
同 SHA 重新 stage、任一 API 重启、当前版本/配置变化、回执被篡改或过期都会使旧批准失效；
此时重新完成检查、准备回执和人工批准。命令不提供强制通过选项。

Docker 探测每条最多 20 秒，整次 CLI 的探测总期限 90 秒；超时在切流前失败并释放锁。
回执不是素材批准，也不能替代数据库不兼容变更或首次 Caddy 持久挂载所需的独立维护验收。
