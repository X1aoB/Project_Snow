const SURFACES = new Set(["landing", "immersive", "assistant"]);

export function surfaceFromPath(pathname = window.location.pathname) {
  const path = String(pathname || "/").replace(/\/+$/, "") || "/";
  if (path === "/immersive") return "immersive";
  if (path === "/assistant") return "assistant";
  return "landing";
}

export function modeForSurface(surface) {
  return surface === "assistant" ? "assistant" : "immersive";
}

export function pathForSurface(surface) {
  if (!SURFACES.has(surface) || surface === "landing") return "/";
  return `/${surface}/`;
}

export function navigateToSurface(surface) {
  window.location.assign(pathForSurface(surface));
}
