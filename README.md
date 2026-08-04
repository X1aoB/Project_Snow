# Project Snow

基于 RAG 架构的《尘白禁区》世界观智能对话系统。项目以可追溯的 Wiki
资料、角色证据视图和受控检索为基础，提供 22 名角色的本地测试聊天产品。

## 仓库边界

- `App/`：可提交的应用代码、前端、桌面壳、管线与测试。
- `Data/`：本机只读的 Wiki 原始资料与采集输出。它可能包含大量或受许可
  限制的内容，因此默认不纳入 Git。
- `App/runtime/`：本机生成的索引、审核队列、反馈、日志和聊天 SQLite 数据库；
  默认不纳入 Git。

应用从 `Data/` 读取资料，但不会修改它。需要在新机器上重建资料时，请使用本地
`Data/Scraper/` 的采集说明；资料本身不会随应用代码推送。

## 本地启动

需要 Python 3.11+；桌面客户端另需 Node.js 20+。在 PowerShell 中：

```powershell
cd App
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m backend.snow_app.main
```

在第二个 PowerShell 窗口启动 Web 客户端：

```powershell
cd App
.\.venv\Scripts\Activate.ps1
python scripts/dev_server.py
```

浏览器访问 `http://127.0.0.1:8080/`；内部证据与审核工作台位于
`http://127.0.0.1:8080/workspace/`。模型、桌面客户端、测试与运行时数据的完整说明见
[App/README.md](App/README.md)。

`.env` 只保存在本机，绝不要提交 API Key、令牌、数据库或会话记录。可提交的
`.env.example` 仅用于说明所需变量。

从 Wiki 下载的角色头像也仅保留在本机测试目录，默认不会推送；缺少图片时客户端会使用
角色文字头像，避免将未经确认可再分发的媒体带入源代码仓库。
