// 记忆档案「通讯器对话」视图（按需加载，不占首屏）：读本机 IndexedDB 里该角色
// communicationChannel !== "in_person" 的历史，按天分组，每轮显示用户一句与她的回复摘要，
// 最新在前；点击一条在回看面板里定位到该消息。只保存在本机，不上传。说明见 docs/snow-adv-ui-notes.md。

// host/logic 由 snow-features.js 传入；el/icon/dayLabel/clock/nameOf 复用那里的同一套工具。
export function createMemoryConvoView(host, { el, icon, dayLabel, clock, dayKey, nameOf, openTranscript }) {
  const { logic } = host;
  const root = el("div", { class: "snow-convo-list" });
  let version = 0;
  const limit = 60;

  async function render(characterId) {
    const current = ++version;
    root.replaceChildren(el("p", { class: "snow-empty", text: "正在读取……" }));
    let data;
    try { data = await logic.conversations.byDay(characterId, { limit }); }
    catch { data = null; }
    if (current !== version) return;
    if (!data || !data.days.length) {
      root.replaceChildren(el("p", { class: "snow-empty",
        text: "还没有通讯器对话。在聊天器里聊几句，这里就会按天留下你们聊过的内容——只保存在这个浏览器里。" }));
      return;
    }
    const nodes = [];
    for (const day of data.days) {
      nodes.push(el("h3", { class: "snow-memory-day", text: dayLabel(day.dayKey, host.snapshot().localDayKey) }));
      for (const exchange of day.items) {
        const open = () => { void openTranscript(exchange.replyId || exchange.messageId); };
        const card = el("button", { type: "button", class: "snow-convo-item" },
          exchange.user ? el("p", { class: "snow-convo-you" }, el("span", { class: "snow-convo-tag", text: "你" }),
            el("span", { text: exchange.user.text })) : null,
          el("p", { class: "snow-convo-her" }, el("span", { class: "snow-convo-tag", text: nameOf(characterId) }),
            el("span", { text: exchange.reply.text })),
          el("span", { class: "snow-convo-time", text: exchange.createdAt ? clock(exchange.createdAt) : "" }));
        card.addEventListener("click", open);
        nodes.push(card);
      }
    }
    if (data.total > data.shown) nodes.push(el("p", { class: "snow-hint snow-convo-more",
      text: `只显示最近 ${data.shown} 轮，更早的在回看里。` }));
    root.replaceChildren(...nodes);
  }

  return { node: root, render, destroy() { version += 1; root.replaceChildren(); } };
}
