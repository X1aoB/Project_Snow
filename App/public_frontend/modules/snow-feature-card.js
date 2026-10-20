// 台词图片卡（只在点「生成图片卡」时加载）。图片在本机用 canvas 生成，不上传。
// 预览用 toDataURL（CSP 的 img-src 不允许 blob:），保存沿用历史导出的 a[download] 方式。
const STYLES = Object.freeze({
  雪白: { bg: ["#ffffff", "#eef4f8"], ink: "#1f2d3a", muted: "#4a5d6e", faint: "#bcd5e5" },
  暮色: { bg: ["#3e314c", "#2e2439"], ink: "#f8eff5", muted: "#cdbacb", faint: "#6e5a7e" },
  夜色: { bg: ["#1c2b44", "#142033"], ink: "#edf3f9", muted: "#b0c0d0", faint: "#3b5678" },
});
const WIDTH = 1080, HEIGHT = 1350;
const STORY = '"LXGW WenKai Screen", "KaiTi", "STKaiti", serif';
const UI = '"PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", sans-serif';
const NO_LINE_START = "，。！？、；：”’」』）》…—,.!?;:)";
let dialog = null;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) if (child != null) node.append(child);
  return node;
}
export function wrapLines(measure, text, width) {
  const lines = [];
  let line = "";
  for (const ch of Array.from(text.replace(/\s+/g, " ").trim())) {
    if (line && measure(line + ch) > width && !NO_LINE_START.includes(ch)) { lines.push(line); line = ch; }
    else line += ch;
  }
  if (line) lines.push(line);
  return lines;
}
function loadImage(src) {
  return new Promise(resolve => {
    if (!src || !src.startsWith("/")) { resolve(null); return; }
    const image = new Image();
    image.decoding = "async";
    image.onload = () => resolve(image);
    image.onerror = () => resolve(null);
    image.src = src;
  });
}
function cardDate(favorite, message) {
  const value = message?.createdAt ?? message?.created_at ?? favorite.createdAt;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Hong_Kong", year: "numeric", month: "long", day: "numeric" }).format(date);
}
export async function drawCard({ text, name, meta, styleName, charColor, avatar }) {
  const style = STYLES[styleName] || STYLES.雪白;
  await Promise.all([document.fonts.load(`64px ${STORY}`, text + name), document.fonts.load(`600 48px ${STORY}`, name)]).catch(() => null);
  const canvas = document.createElement("canvas");
  canvas.width = WIDTH; canvas.height = HEIGHT;
  const ctx = canvas.getContext("2d");
  const background = ctx.createLinearGradient(0, 0, 0, HEIGHT);
  background.addColorStop(0, style.bg[0]); background.addColorStop(1, style.bg[1]);
  ctx.fillStyle = background; ctx.fillRect(0, 0, WIDTH, HEIGHT);
  ctx.fillStyle = style.faint; ctx.globalAlpha = 0.5;          // 远处的雪丘
  ctx.beginPath(); ctx.moveTo(0, HEIGHT - 220);
  ctx.quadraticCurveTo(300, HEIGHT - 330, 620, HEIGHT - 250); ctx.quadraticCurveTo(880, HEIGHT - 190, WIDTH, HEIGHT - 280);
  ctx.lineTo(WIDTH, HEIGHT); ctx.lineTo(0, HEIGHT); ctx.fill();
  ctx.globalAlpha = 0.35;                                       // 雪粒（固定位置，同一句每次画出来一样）
  for (let index = 0; index < 46; index += 1) {
    ctx.beginPath(); ctx.arc((index * 197) % WIDTH, (index * 331) % (HEIGHT - 300), 2 + (index % 3), 0, Math.PI * 2); ctx.fill();
  }
  ctx.globalAlpha = 1;
  const accent = ctx.createLinearGradient(110, 0, 360, 0);    // 角色色细线，同对白框左上
  accent.addColorStop(0, charColor); accent.addColorStop(1, "rgba(0, 0, 0, 0)");
  ctx.fillStyle = accent; ctx.fillRect(110, 260, 250, 6);
  ctx.fillStyle = style.ink; ctx.font = `64px ${STORY}`; ctx.textBaseline = "alphabetic";
  let size = 64, lines = wrapLines(value => ctx.measureText(value).width, text, WIDTH - 220);
  while (lines.length > 8 && size > 44) {                       // 长句缩小字号，保证放进卡片
    size -= 4; ctx.font = `${size}px ${STORY}`;
    lines = wrapLines(value => ctx.measureText(value).width, text, WIDTH - 220);
  }
  const lineHeight = Math.round(size * 1.7), top = Math.max(380, (HEIGHT - 260 - lines.length * lineHeight) / 2 + 60);
  lines.forEach((line, index) => ctx.fillText(line, 110, top + index * lineHeight));
  const cy = HEIGHT - 190;                                      // 名牌：头像（没有图时用首字）+ 名字 + 地点日期
  ctx.save(); ctx.beginPath(); ctx.arc(160, cy, 50, 0, Math.PI * 2); ctx.fillStyle = style.bg[0]; ctx.fill(); ctx.clip();
  if (avatar) ctx.drawImage(avatar, 110, cy - 50, 100, 100);
  else { ctx.fillStyle = style.ink; ctx.font = `600 46px ${STORY}`; ctx.textAlign = "center"; ctx.fillText(Array.from(name)[0] || "", 160, cy + 16); }
  ctx.restore();
  ctx.beginPath(); ctx.arc(160, cy, 50, 0, Math.PI * 2); ctx.lineWidth = 6; ctx.strokeStyle = charColor; ctx.stroke();
  ctx.textAlign = "left"; ctx.fillStyle = style.ink; ctx.font = `600 48px ${STORY}`; ctx.fillText(name, 240, cy - 6);
  ctx.fillStyle = style.muted; ctx.font = `28px ${UI}`; ctx.fillText(meta, 240, cy + 40);
  ctx.font = `24px ${UI}`; ctx.textAlign = "right"; ctx.fillText("小吉终端 · 本机生成", WIDTH - 80, HEIGHT - 70);
  return canvas;
}

export async function openFavoriteCard({ host, favorite, icon, message }) {
  const snapshot = host.snapshot();
  const character = snapshot.characters?.find(item => item.character_id === favorite.characterId) || null;
  const name = character?.display_name || "角色";
  const meta = [favorite.locationName, cardDate(favorite, message)].filter(Boolean).join(" · ");
  const probe = el("i", { "data-snow-character": favorite.characterId, hidden: "" });
  document.body.append(probe);
  const charColor = getComputedStyle(probe).getPropertyValue("--char").trim() || getComputedStyle(document.documentElement).getPropertyValue("--ice-500").trim();
  probe.remove();
  const avatarInfo = host.avatarOf(character);
  const avatar = await loadImage(avatarInfo?.src || avatarInfo?.thumbnail_src);
  dialog?.remove();
  dialog = el("dialog", { class: "modal snow-feature-modal snow-card-modal", id: "snow-card-dialog", "aria-labelledby": "snow-card-title" });
  const close = el("button", { type: "button", class: "icon-button", "aria-label": "关闭图片卡" }, icon("x"));
  const preview = el("img", { alt: `图片卡预览：${name}，${favorite.text}` });
  const seg = el("div", { class: "snow-seg", role: "radiogroup", "aria-label": "卡片底色" });
  const save = el("button", { type: "button", class: "primary-button" }, "保存图片");
  const done = el("button", { type: "button", class: "secondary-button" }, "关闭");
  const status = el("span", { role: "status", "aria-live": "polite", text: `${WIDTH} × ${HEIGHT} PNG` });
  dialog.append(
    el("header", {}, el("div", {}, el("span", { class: "panel-kicker", text: "台词收藏" }), el("h2", { id: "snow-card-title", text: "生成图片卡" }),
      el("p", { text: "图片在本机生成，不会上传。" })), close),
    el("div", { class: "snow-card-preview" }, preview),
    el("div", { class: "snow-modal-row" }, el("span", { text: "卡片底色" }), seg),
    el("div", { class: "snow-modal-foot" }, status, el("div", { class: "modal-actions" }, done, save)));
  document.body.append(dialog);
  const tone = snapshot.tone === "dark" ? (snapshot.toneTime === "dusk" ? "暮色" : "夜色") : "雪白";
  let styleName = tone, canvas = null, version = 0;
  async function paint() {
    const current = ++version;
    seg.querySelectorAll("button").forEach(button => button.setAttribute("aria-checked", String(button.dataset.style === styleName)));
    const next = await drawCard({ text: favorite.text, name, meta, styleName, charColor, avatar });
    if (current !== version) return;
    canvas = next;
    preview.src = canvas.toDataURL("image/png");
  }
  for (const key of Object.keys(STYLES)) {
    const button = el("button", { type: "button", role: "radio", "aria-checked": "false", "data-style": key }, key);
    button.addEventListener("click", () => { styleName = key; paint(); });
    seg.append(button);
  }
  save.addEventListener("click", () => {
    if (!canvas) return;
    canvas.toBlob(blob => {
      if (!blob) { status.textContent = "没能生成图片，请再试一次。"; return; }
      const url = URL.createObjectURL(blob);
      const anchor = el("a", { href: url, download: `小吉终端-${name}-${new Date().toISOString().slice(0, 10)}.png` });
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      status.textContent = "已保存到浏览器的下载文件夹。";
    }, "image/png");
  });
  close.addEventListener("click", () => dialog.close());
  done.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { dialog?.remove(); dialog = null; });
  dialog.showModal();
  await paint();
  return dialog;
}
