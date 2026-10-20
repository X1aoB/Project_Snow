// 「在本机保存」凭证前的告示与清除后的作废（交接说明第 13.3 节）。只在用户选择保存、清除本机凭证
// 或上次作废未完成时按需加载；复用 .modal / .notice-list / .notice-choice，旧界面和雪境界面都能直接显示。
const LABELS = { "2h": "2 小时", "24h": "24 小时", "3d": "3 天", "7d": "7 天" };
const PENDING_KEY = "project-snow-public:byok-revoke";

// 服务端 POST /byok/revoke 不带参数：作废这个匿名身份在此刻之前签发的全部「本机保存」凭证（v2），
// 仅当前标签页的凭证（v1）不受影响。所以作废必须在签发新的本机凭证之前完成，否则会连新凭证一起作废。
// 待作废标记存在 localStorage，所有标签页共用：网络错误、5xx、429 时保留，下次打开页面或下次签发前重试；
// 其他 4xx（例如回滚后的旧服务端没有这个接口）重试也没用，直接放弃。返回 true 已作废、null 放弃、false 待重试。
async function revoke(api, storage) {
  storage.setItem(PENDING_KEY, "1");
  try {
    await api("/byok/revoke", { method: "POST", timeoutMs: 15000, body: "{}" });
  } catch (error) {
    if (!(error?.httpStatus && error.httpStatus < 500 && error.httpStatus !== 429)) return false;
    storage.removeItem(PENDING_KEY);
    return null;
  }
  storage.removeItem(PENDING_KEY);
  return true;
}

// 「清除模型会话」和启动时重试用：返回给用户看的提示。
export async function revokeCredential(api, storage) {
  const done = await revoke(api, storage);
  return done ? "本机保存的模型凭证已清除并作废"
    : done === null ? "本机凭证已删除；服务器未能作废，它会在到期时失效"
      : "本机凭证已删除；服务器暂未确认作废，下次打开页面会重试";
}

// 签发前调用：replacing 表示要换掉一张本机凭证（app.js 已在本地删掉它）；keep 表示这次要签发本机凭证。
// 作废没有完成时不能签发新的本机凭证；「仅当前标签页」的凭证不受之后重试影响，可以照常签发。
export async function revokeBeforeIssue(api, storage, keep, replacing) {
  if (!replacing && !storage.getItem(PENDING_KEY)) return;
  if (await revoke(api, storage) === false && keep) {
    throw new Error("之前保存在本机的凭证还没有作废成功，请稍后再保存；也可以先选「仅当前标签页」。");
  }
}

export function confirmCredentialRetention(option) {
  const label = LABELS[option];
  if (!label) return Promise.resolve(false);
  const dialog = document.createElement("dialog");
  dialog.className = "modal notice-modal credential-retention-modal";
  dialog.setAttribute("aria-labelledby", "credential-retention-title");
  dialog.setAttribute("aria-describedby", "credential-retention-notes");
  dialog.innerHTML = `<form method="dialog"><header><div><span class="panel-kicker">模型凭证</span><h2 id="credential-retention-title">在本机保存 ${label}</h2></div><button class="icon-button" type="button" data-retention-cancel aria-label="取消">×</button></header>
<div id="credential-retention-notes" class="notice-list">
<p>保存后关闭标签页也不会清除凭证，重新打开页面不用再输入 API Key。${label}后自动失效，不会续期。</p>
<p>服务器不保存你的 API Key；这个浏览器里保存的是服务器加密过的凭证。</p>
<p>有效期内，使用这个浏览器的人都能用你的模型服务对话，费用由你的账号承担。公共或共用电脑请不要开启。</p>
<p>随时可以在「设置 → 模型」点「清除模型会话」，本机凭证会被删除并立即作废。</p>
</div>
<label class="notice-choice"><input type="checkbox" />我已了解，在这台设备上保存 ${label}</label>
<div class="modal-actions"><button type="button" class="secondary-button" data-retention-cancel>取消</button><button type="button" class="primary-button" data-retention-confirm disabled>确认保存</button></div></form>`;
  document.body.append(dialog);
  const check = dialog.querySelector('input[type="checkbox"]');
  const confirm = dialog.querySelector("[data-retention-confirm]");
  return new Promise((resolve) => {
    let accepted = false;
    // 现有主按钮没有禁用样式，未勾选时会和可点时一样；这里只给本弹窗的按钮调暗。
    const sync = () => { confirm.disabled = !check.checked; confirm.style.opacity = check.checked ? "" : "0.5"; confirm.style.cursor = check.checked ? "" : "default"; };
    check.onchange = sync;
    sync();
    for (const button of dialog.querySelectorAll("[data-retention-cancel]")) button.onclick = () => dialog.close();
    confirm.onclick = () => { accepted = check.checked; dialog.close(); };
    dialog.addEventListener("close", () => { dialog.remove(); resolve(accepted); }, { once: true });
    dialog.showModal();
    check.focus();
  });
}


// 设置页「凭证保存」改选时调用：选本机保存先看告示，取消就退回上一次的选择。
// 告示确认只对选中的时长有效，真正签发要等用户输入 API Key 后保存（state.retentionConsent 由 app.js 校验）。
export async function chooseRetention(select, state, refresh) {
  if (select.value !== "tab" && state.retentionConsent !== select.value) {
    if (await confirmCredentialRetention(select.value)) state.retentionConsent = select.value;
    else select.value = select.dataset.previous || "tab";
  }
  select.dataset.previous = select.value;
  refresh();
}
