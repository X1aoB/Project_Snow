export const STAGE_STATES = ["neutral", "gentle_smile", "happy", "amused", "teasing", "relieved", "serious", "focused", "thinking", "confused", "skeptical", "concerned", "surprised", "embarrassed", "sad", "disappointed", "annoyed", "angry"] as const;
export const STAGE_MOTIONS = ["none", "lean_in", "tremble", "recoil", "startle"] as const;
export interface StageAsset {
  url: string; sha256: string; media_type: "image/png" | "image/webp";
  source: { kind: string; reference: string };
  approval: { status: "approved"; approved_by: string; approved_at: string; evidence: string };
}
export interface StageRelease {
  schema: "project-snow-stage-1"; version: string;
  characters: Array<{ character_id: string; states: Record<string, StageAsset>; motions: string[] }>;
}

function record(value: unknown): value is Record<string, unknown> { return !!value && typeof value === "object" && !Array.isArray(value); }
function text(value: unknown): value is string { return typeof value === "string" && value.trim().length > 0; }
function assetUrl(value: unknown): value is string { return typeof value === "string" && /^\/assets\/stage\/[a-zA-Z0-9_./-]+$/.test(value) && !value.includes(".."); }
function hash(value: unknown): value is string { return typeof value === "string" && /^[0-9a-f]{64}$/.test(value); }
export function validateStageRelease(value: unknown, expectedIds?: string[]): StageRelease {
  if (!record(value) || value.schema !== "project-snow-stage-1" || !text(value.version) || !Array.isArray(value.characters) || value.characters.length !== 22) throw new Error("stage_manifest_invalid");
  const ids = new Set<string>();
  for (const character of value.characters) {
    if (!record(character) || typeof character.character_id !== "string" || !/^[0-9a-f]{12}$/.test(character.character_id) || ids.has(character.character_id)
      || !record(character.states) || !character.states.neutral || !Array.isArray(character.motions) || !character.motions.includes("none")
      || character.motions.some(motion => !(STAGE_MOTIONS as readonly unknown[]).includes(motion))) throw new Error("stage_manifest_invalid");
    ids.add(character.character_id);
    for (const [state, asset] of Object.entries(character.states)) {
      if (!(STAGE_STATES as readonly string[]).includes(state) || !record(asset) || !assetUrl(asset.url) || !hash(asset.sha256)
        || !["image/png", "image/webp"].includes(String(asset.media_type)) || !record(asset.source) || !text(asset.source.kind) || !text(asset.source.reference)
        || !record(asset.approval) || asset.approval.status !== "approved" || !text(asset.approval.approved_by)
        || !text(asset.approval.evidence) || !text(asset.approval.approved_at) || !Number.isFinite(Date.parse(asset.approval.approved_at))) throw new Error("stage_manifest_invalid");
    }
  }
  if (expectedIds && (expectedIds.length !== 22 || expectedIds.some(id => !ids.has(id)))) throw new Error("stage_roster_mismatch");
  return value as unknown as StageRelease;
}

async function verifiedBytes(url: string, expectedHash: string, maximum: number): Promise<ArrayBuffer> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(url, { signal: controller.signal, credentials: "omit", redirect: "error", cache: "force-cache" });
    if (!response.ok || Number(response.headers.get("content-length")) > maximum || !response.body) throw new Error("stage_asset_unavailable");
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maximum) { await reader.cancel(); throw new Error("stage_asset_too_large"); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
    if (digest !== expectedHash) throw new Error("stage_asset_hash_mismatch");
    return bytes.buffer;
  } finally { clearTimeout(timer); }
}

export async function loadStageRelease(config: unknown, expectedIds: string[]): Promise<StageRelease | null> {
  if (!record(config) || config.enabled !== true) return null;
  if (!assetUrl(config.manifest_url) || !config.manifest_url.endsWith(".json") || !hash(config.sha256)) throw new Error("stage_config_invalid");
  const bytes = await verifiedBytes(config.manifest_url, config.sha256, 2 * 1024 * 1024);
  return validateStageRelease(JSON.parse(new TextDecoder().decode(bytes)), expectedIds);
}

const images = new Map<string, Promise<string>>();
export function verifiedStageImage(asset: StageAsset): Promise<string> {
  const key = `${asset.url}:${asset.sha256}`;
  const cached = images.get(key);
  if (cached) return cached;
  const result = verifiedBytes(asset.url, asset.sha256, 8 * 1024 * 1024).then(bytes => URL.createObjectURL(new Blob([bytes], { type: asset.media_type })));
  images.set(key, result);
  void result.catch(() => { if (images.get(key) === result) images.delete(key); });
  while (images.size > 32) {
    const oldestKey = images.keys().next().value!;
    const oldest = images.get(oldestKey)!;
    images.delete(oldestKey);
    void oldest.then(url => URL.revokeObjectURL(url), () => undefined);
  }
  return result;
}
