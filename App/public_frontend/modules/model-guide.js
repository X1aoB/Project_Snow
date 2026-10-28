// 订阅连接教程正文：只在切到「Codex 订阅」时加载，不占首屏体积。说明见 docs/snow-adv-ui-notes.md。
const TUTORIAL = "                <ol>\n                  <li><strong>安装官方 Codex CLI 并登录。</strong><p>需要 Node.js 22 或更高版本，以及 Codex 0.153.4 或更新版本。Codex CLI 0.151 不支持此连接方式，请先升级。在本机终端运行：</p><pre><code>npm install -g @openai/codex@latest\ncodex login</code></pre><p>在浏览器中登录你已有 ChatGPT 订阅的账号，选择 ChatGPT 登录方式。若 npm 安装的 CLI 无法正常运行，连接器也会尝试使用官方桌面版中符合版本要求的 Codex；请先将桌面版更新到最新版本。</p></li>\n                  <li><strong>下载个人连接器。</strong><p><a href=\"/downloads/snow-codex-connector.mjs\" download>下载 snow-codex-connector.mjs</a>，保存到电脑上的文件夹。</p></li>\n                  <li><strong>在此页生成配对码，然后启动连接器。</strong><p>在下载文件所在的文件夹打开终端，运行：</p><pre data-command-slot></pre><p>连接器会交互提示输入上方配对码。只在提示中输入，不把配对码写入网址或命令参数。配对码仅用于本次个人连接，请勿分享。</p></li>\n                  <li><strong>配对后选择模型和推理强度，再点击“使用所选模型”。</strong><p>默认优先 Luna Max（GPT-5.6-Luna · 极高）。账号没有此模型时，请明确选择其他可用模型；网页不会自动切换。改变选项只在保存后生效。聊天期间保持本机和连接器运行；停止连接器或点击“断开连接”可停止连接。每位访客都需要配对自己的订阅。</p></li>\n                </ol>\n                <p>额度与频率限制由你自己的订阅计划决定，不走 API 账单。模型及推理强度权限因账号而异，以连接器读取的当前账号列表为准。较高强度可能等待更久；额度消耗以订阅计划为准。请勿向网站或他人提供 Cookie、auth.json、登录密码或登录凭据。</p>\n                <p>订阅连接时暂停自动摘要，避免与下一条消息争用连接；切回 API Key 后会按你原来的偏好恢复。</p>\n                <p><a href=\"https://learn.chatgpt.com/docs/auth\" target=\"_blank\" rel=\"noreferrer\">官方登录说明</a> · <a href=\"https://learn.chatgpt.com/docs/app-server\" target=\"_blank\" rel=\"noreferrer\">官方 App Server 文档</a></p>";

export function renderSubscriptionTutorial(details, origin) {
  const summary = details?.querySelector(":scope > summary");
  if (!summary || details.querySelector(":scope > ol")) return;
  // 命令行元素留在 index.html（旧版脚本也会写它），这里把它挪进第 3 步。
  const command = details.querySelector("#subscription-connect-command");
  summary.insertAdjacentHTML("afterend", TUTORIAL);
  const pre = command?.closest("pre"), slot = details.querySelector("[data-command-slot]");
  if (pre && slot) { pre.hidden = false; slot.replaceWith(pre); }
  if (command) command.textContent = `node snow-codex-connector.mjs --site ${origin}`;
}
