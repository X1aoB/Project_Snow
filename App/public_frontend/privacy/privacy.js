const attribution = document.getElementById("avatar-attribution");
const escapeHtml = (value) => {
  const span = document.createElement("span");
  span.textContent = String(value ?? "");
  return span.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
};

async function load() {
  const [configResponse, attributionResponse] = await Promise.all([
    fetch("/public/v1/config", { credentials: "same-origin" }),
    fetch("/public/v1/attributions", { credentials: "same-origin" }),
  ]);
  if (!configResponse.ok || !attributionResponse.ok) throw new Error("load_failed");
  const config = await configResponse.json();
  const attributions = await attributionResponse.json();
  document.getElementById("privacy-project-link").href = config.source_links.project_snow;
  document.getElementById("privacy-website-link").href = config.source_links.mywebsite;
  document.getElementById("privacy-release-link").href = config.source_links.releases;
  const providerLinks = document.getElementById("provider-privacy-links");
  providerLinks.innerHTML = (config.providers || []).map((provider) =>
    `<a href="${escapeHtml(provider.privacy_url || provider.documentation_url)}" rel="noreferrer">${escapeHtml(provider.display_name)} 隐私说明</a>`
  ).join("");
  attribution.innerHTML = (attributions.avatars || []).map((entry) => {
    const source = `<a href="${escapeHtml(entry.file_page_url)}" rel="noreferrer">来源页</a>`;
    const licenseSource = `<a href="${escapeHtml(entry.license_source_page)}" rel="noreferrer">许可依据</a>`;
    return `<article><strong>${escapeHtml(entry.display_name)}</strong><span>${source} · 修订 ${escapeHtml(entry.source_revision_id)} · 上传者 ${escapeHtml(entry.source_uploader)}</span><span>${licenseSource} · ${escapeHtml(entry.license)} · 已标注裁剪与格式转换</span></article>`;
  }).join("");
  document.getElementById("sticker-attribution-summary").textContent =
    `当前表情包含 ${(attributions.stickers || []).length} 项公开署名记录；每项保留文件页、固定修订、上传者、许可依据和显示衍生图的修改说明。`;
  document.getElementById("download-attributions").href = config.attribution_url || "/public/v1/attributions";
  const knowledge = attributions.knowledge_data || {};
  const sourceCount = Number(knowledge.source_count || 0);
  const modifications = Array.isArray(knowledge.modifications) ? knowledge.modifications.join("、") : "清洗、分段与检索加工";
  document.getElementById("knowledge-attribution-summary").textContent =
    `知识数据包 ${knowledge.version || "当前版本"} 记录 ${sourceCount || "全部"} 个来源页的固定修订；加工包括${modifications}。`;
  document.getElementById("download-data-attributions").href = knowledge.url || "/public/v1/attributions/data";
}

load().catch(() => { attribution.innerHTML = "<p>暂时无法读取媒体清单，请稍后刷新。</p>"; });
