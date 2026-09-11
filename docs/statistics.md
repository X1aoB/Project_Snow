# 可选统计适配

`App/public_frontend/statistics/config.mjs` 默认 enabled=false。开启后仍需用户主动允许；仅允许公开角色和页面路径，外域 endpoint 需精确加入 CSP connect-src。

单个模块入口注册可删除的适配器；聊天提交点只通过可选回调通知 request_id，默认关闭时回调不存在。不修改请求正文、API、签名状态、浏览器业务存储或数据库。页面队列有界、超时及重试有限，异常与聊天隔离。

服务质量来自外部已有脱敏完成日志；日志转发器由 Snow_Statistics 单独部署，不作为聊天服务依赖。访问统计与请求成功指标分别处理，客户端不能申报可信完成。

所有副本随本仓库独立构建、测试、发布，不需要 Snow_Statistics checkout。推广仍遵守 AGENTS.md 的候选验收和手动生产发布流程。

移除：回退 index.html 的统计模块入口、app.js 的可选回调，删除 statistics 目录及相应隐私说明。原有功能兼容不变。
