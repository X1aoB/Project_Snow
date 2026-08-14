# Qwen3.8-Max Batch 自动审核指南

该流程使用 DashScope Batch File 对关系候选和缺失实体候选进行两轮证据审核。普通人工审核接口保持不变；只有通过固定样本校准、本地证据门槛和精确图节点映射的数据，才能由自动采纳入口写入 `verified` 图谱。

## 配置

在本地 `App/.env` 设置，不要提交真实 Key：

```dotenv
EVIDENCE_REVIEW_PROVIDER=dashscope-batch
EVIDENCE_REVIEW_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EVIDENCE_REVIEW_API_KEY=<private key>
EVIDENCE_REVIEW_MODEL=qwen3.8-max
EVIDENCE_REVIEW_MAX_BUDGET_CNY=300
```

Key 只用于请求头，不会写入 Batch JSONL、运行清单、日志或模型报告。Batch 输入会把候选实体、三元组和来源证据发送到配置的 DashScope 账户。

## 操作顺序

可在 `/workspace/#automation` 完成操作，也可以从 `App/` 使用 CLI。

1. 免费计算当前规模、token 和费用：

   ```powershell
   python -m pipelines.review_evidence_batch estimate --mode production
   ```

2. 先创建 `test` 运行验证 JSONL 格式，再创建 `calibration` 运行。创建会产生外部状态，因此必须传入刚生成的 `estimate_hash` 并明确确认：

   ```powershell
   python -m pipelines.review_evidence_batch create --mode calibration `
     --estimate-hash <hash> --confirm-submit
   ```

3. 每隔一至两分钟同步一次。首轮结束后，服务会自动提交需要思考复核的第二轮；失败请求最多单独重试两次：

   ```powershell
   python -m pipelines.review_evidence_batch sync <run_id>
   ```

4. 在工作台标注固定种子的校准样本。样本不足或准确率未达到门槛时，只关闭相应自动决策类别。
5. 以通过校准的运行 ID 创建 `production` 运行并同步到 `ready_to_admit`。
6. 在工作台一次确认采纳，或运行：

   ```powershell
   python -m pipelines.review_evidence_batch admit <run_id> --confirm-apply
   ```

7. 如需撤销，并且本次数据没有后续人工修改：

   ```powershell
   python -m pipelines.review_evidence_batch rollback <run_id> --confirm-rollback
   ```

## 判定与数据边界

- 首轮关闭 thinking 并使用 Qwen JSON Schema；第二轮开启 thinking，输出仍需通过本地结构与逐字证据校验。
- `MENTIONS`、缺失证据和非法引文由本地规则处理，不产生模型费用。
- 高影响关系必须两轮一致批准；模型拒绝也必须两轮一致。
- 实体必须两轮一致、名称逐字存在、类型唯一且不是泛化动作或标题。
- 无法唯一映射节点、证据截断、风险标记、结论冲突和持续弃权进入 `needs_human_review`。
- 自动节点/边使用 `confidence=model_approved_audited`，保留运行、策略、报告和候选 ID；不会冒充人工审核。
- 输入哈希、模型、策略和轮次完全相同的成功报告会直接复用；校准样本进入正式运行时不会再次付费，复用来源写入运行 manifest 和报告。
- 每个运行保存在 `runtime/review/automation/runs/<run_id>/`。只有成功请求计入实际费用；新阶段提交前继续执行 ¥300 熔断检查。
- 回滚会校验候选状态、审核时间以及本次创建节点/边的内容哈希；任何后续人工修改都会令自动回滚拒绝执行。

Batch 价格会变化。工作台显示的价格快照和估算哈希只在当天有效；折扣变化后必须重新估算。
