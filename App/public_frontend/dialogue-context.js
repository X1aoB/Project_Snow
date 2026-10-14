// Sidecars are display hints until the server verifies their signatures. The
// complete receipt index stays local; only relevant receipts travel each turn.
export const DIALOGUE_PROTOCOL = "public-dialogue-v2";
export const SCENE_PROTOCOL = "public-scene-action-v2";
export const dialogueRecordKey = characterId => `dialogue_context_v1:${characterId}`;
export function decodeSidecar(token) {
  try {
    const encoded = String(token || "").split(".")[0].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(encoded + "=".repeat((4 - encoded.length % 4) % 4)), c => c.charCodeAt(0))));
  } catch { return {}; }
}
export function mergeDialogueReceipts(previous = [], incoming = []) {
  const receipts = new Map();
  for (const receipt of [...previous, ...incoming]) {
    if (receipt?.memory?.memory_id && typeof receipt.proof === "string") receipts.set(receipt.memory.memory_id, structuredClone(receipt));
  }
  return [...receipts.values()];
}
export function relevantDialogueReceipts(receipts, text = "") {
  const query = String(text).toLowerCase();
  const terms = query.match(/[a-z0-9]{2,}|[\u3400-\u9fff]{2}/g) || [];
  const sharedRecall = /我们|咱们|一起|上次|还记得|共同/.test(query);
  const placeRecall = /哪[里儿]|哪里|地点|去哪|会合|赴约|上次.*去/.test(query);
  return receipts.map((receipt, order) => {
    const memory = receipt.memory || {};
    const sourceScore = sharedRecall && memory.kind === "shared_scene_event" ? 3 : 0;
    const locationScore = placeRecall && memory.kind === "shared_scene_event" && memory.location_id ? 8 : 0;
    return { receipt, order, score: sourceScore + locationScore
      + terms.reduce((score, term) => score + (String(memory.value || "").toLowerCase().includes(term) ? 1 : 0), 0) };
  })
    .sort((a, b) => b.score - a.score || b.order - a.order).slice(0, 12).map(item => structuredClone(item.receipt));
}
export function isPureRendezvousJoin(text) {
  return /^(?:好[的啊]?[，,\s]*)?(?:那(?:就)?[，,\s]*)?(?:我(?:现在|这就|马上)?(?:过去|来找你)(?:了|啦|吧)?|(?:我们)?走吧|那就走吧|这就过去)[。！!～~\s]*$/.test(String(text || "").trim());
}
export function sceneVisualKey(location) {
  const value = String(location || "").trim();
  const mappings = [[['个人房间', '宿舍'], 'quarters'], [['休息区', '公共区'], 'lounge'], [['训练区'], 'training'], [['资料室', '资料阅览区', '阅览区'], 'archive'], [['餐厅', '食堂'], 'canteen'], [['观景区'], 'observation'], [['医务室'], 'medical'], [['走廊'], 'corridor']];
  return mappings.find(([terms]) => terms.some(term => value.includes(term)))?.[1] || 'generic';
}
