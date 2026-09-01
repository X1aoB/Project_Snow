const SURFACES = new Set(["landing", "immersive"]);

export function surfaceFromPath(pathname = window.location.pathname) {
  const path = String(pathname || "/").replace(/\/+$/, "") || "/";
  if (path === "/immersive") return "immersive";
  return "landing";
}

export function modeForSurface(_surface) {
  return "immersive";
}

export function pathForSurface(surface) {
  if (!SURFACES.has(surface) || surface === "landing") return "/";
  return `/${surface}/`;
}

export function navigateToSurface(surface) {
  window.location.assign(pathForSurface(surface));
}
