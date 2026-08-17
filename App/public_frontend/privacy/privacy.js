const attribution = document.getElementById("avatar-attribution");
const escapeHtml = (value) => {
  const span = document.createElement("span");
  span.textContent = String(value ?? "");
  return span.innerHTML;
};

async function load() {
  const [configResponse, characterResponse] = await Promise.all([
    fetch("/public/v1/config", { credentials: "same-origin" }),
    fetch("/public/v1/characters", { credentials: "same-origin" }),
  ]);
  if (!configResponse.ok || !characterResponse.ok) throw new Error("load_failed");
  const config = await configResponse.json();
  const characters = (await characterResponse.json()).characters || [];
  document.getElementById("privacy-project-link").href = config.source_links.project_snow;
  document.getElementById("privacy-website-link").href = config.source_links.mywebsite;
  document.getElementById("privacy-release-link").href = config.source_links.releases;
  const entries = [];
  if (config.analyst_avatar) {
    entries.push({ display_name: "分析员（默认头像）", avatar: config.analyst_avatar });
  }
  entries.push(...characters);
  attribution.innerHTML = entries.map((character) => {
    const avatar = character.avatar || {};
    const source = avatar.source_page
      ? `<a href="${escapeHtml(avatar.source_page)}" rel="noreferrer">来源页</a>`
      : "来源页待补充";
    const licenseSource = avatar.license_source_page
      ? `<a href="${escapeHtml(avatar.license_source_page)}" rel="noreferrer">许可依据</a>`
      : "许可依据待补充";
    return `<article><strong>${escapeHtml(character.display_name)}</strong><span>${source} · ${licenseSource}</span><span>${escapeHtml(avatar.license || "CC BY-NC-SA")} · ${escapeHtml(avatar.license_version || "version unspecified by source")}</span></article>`;
  }).join("");
}

load().catch(() => { attribution.innerHTML = "<p>暂时无法读取媒体清单，请稍后刷新。</p>"; });
