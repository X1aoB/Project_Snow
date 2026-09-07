小吉终端后端边界

当前部署仍是单进程、单 worker 的模块化单体。公共接口是 `public_main.py`；本地工作台是 `main.py`。不要通过增加 Uvicorn workers 绕过容量限制：全局 4 个生成槽、8 个排队槽和匿名主体占用仍是进程内状态。

`public_service.py` 负责 public v1 输入、带签名的 public-state-2 状态和呈现结果；`dialogue_core.py` 定义 Context、WorldState、Budget、Result 与生成准入。公共请求通过 ContextVar 安装隔离的会话/世界快照，不再借用本地缓存条目。`output_validation.py`、`dialogue_output.py`、`text_rules.py` 是无存储/网络操作的输出规则。`MVPService` 保留现有方法委派以兼容本地调用；提示、证据选择与部分复杂守卫尚在该兼容服务内，继续抽取时必须保持行为回归。

模型预算在每次 HTTP POST 尝试之前计数；公共动作最多 1 或 2 次，包含改写。诊断记录真实尝试数。检索分支共享 3 秒期限，慢向量或图服务会退化；后台线程和队列有界，超时不会创建无限任务。运维依赖探测不调用模型，单次等待最多 12 秒，并共享一个未完成的探测。

`AsyncPublicStore` 将完整同步事务放到有限线程池，数据库连接、语句和锁都有超时。取消 HTTP 等待不会假装已经回滚一笔提交中的事务，也不会提前释放执行容量。应通过适配器调用所有异步路由中的 Store 操作。

新增 Alembic 0005 只增加独立的请求所有权表，原缓存表结构、public v1 和 public-state-2 不变。每个进程有随机 owner，10 秒续租、45 秒过期。失联请求转为 `generation_interrupted`，不会自动发出第二次付费请求。旧 owner 的晚到完成与释放被拒绝。无结果的处理记录保留一天；正常完成的结果仍按原规则保留 10 分钟。

应用回退保留 0005 增量表，不执行数据库降级。旧版 Store 在升级后的 PostgreSQL 上另有实际旧代码兼容测试。迁移失败需要回滚 DDL 和 Alembic 版本号，不能伪造 head。数据库 owner 与运行时 DML 角色的目标权限另有测试，生产凭证分离由受控运维步骤完成。签名密钥和对应数据版本必须与恢复清单一起保留；迁移测试不等于整机恢复演练。

前台操作可取消同主体的后台摘要；浏览器断开也会取消摘要。已开始或结果不确定的摘要保存中断终态，不能换一个变更后的请求体重放同一 UUID。停机拒绝新生成，已接受聊天最多等待 300 秒，之后进行持久化中断与清理；容器停止宽限应至少 330 秒。

`/public/v1/build-info` 提供 revision、build_time、app/data version 与兼容协议版本；未知构建信息返回 null。`/public/v1/health/full` 额外提供聚合 generation_queue 与 draining，不包含主体、凭证或聊天内容。full 由反向代理拒绝公网访问，监控从容器内读取。媒体完整校验发生在启动时，后续健康查询读取缓存。

本地监听默认 loopback，写操作需要可信 Origin 与 GET 引导产生的 HttpOnly、SameSite=Strict cookie。Agent/Connector legacy 默认关闭，只有显式 `LOCAL_LEGACY_ENABLED=true` 才恢复。附件按实际接收字节数限制 100 MiB 和 15 秒读取期限，不信任 Content-Length。

22 角色离线固定行为门禁在 `tests/test_dialogue_quality.py`。这些合成反例与正例保证通道、动作、文本清理和检索范围约束，不代表真人对人格、自然度、立绘或声音的批准。通用 stage_release 描述目前明确关闭，保持现有素材呈现。
