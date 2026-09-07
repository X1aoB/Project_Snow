import { build } from "esbuild";
import { fileURLToPath } from "node:url";

await build({
  entryPoints: [fileURLToPath(new URL("./src/runtime.ts", import.meta.url))],
  outfile: fileURLToPath(new URL("../public_frontend/modules/runtime.js", import.meta.url)),
  bundle: true,
  format: "esm",
  target: "es2022",
  minify: false,
  sourcemap: false,
  legalComments: "none",
});
