# Public browser modules

The public interface keeps native DOM rendering and the existing public v1,
public-state-2 and IndexedDB 4 contracts. The typed boundary modules contain
storage safeguards, atomic import/retention operations, HTTP deadlines, bounded
history rendering and reviewed stage-asset loading. `app.js` retains the existing
conversation orchestration while these boundaries can be tested independently.

```sh
npm ci --prefix App/public_frontend_src
npm run check --prefix App/public_frontend_src
npm run build --prefix App/public_frontend_src
npm test --prefix App/public_frontend_src
git diff --exit-code App/public_frontend/modules
```

Commit `public_frontend/modules/runtime.js` with its TypeScript changes. The Python
production image serves the committed bundle and needs no Node runtime. The
ordered `public_frontend/assets-manifest.json` declares module dependencies,
styles, scripts and branding; fingerprinting occurs only in the copied image.

The browser reliability tier runs with `RUN_PUBLIC_E2E=1`. It uses temporary
origins and synthetic data, including 10,000-message exports, transaction abort,
storage denial, two-tab handoff, compatible old/new/old storage and request
snapshots. See `App/compat/README.md` for the supported previous-interface target.

## Stage release contract

The optional `config.stage_release` is disabled by default and makes no asset
request while disabled. Enabling requires a same-origin `/assets/stage/*.json`
manifest URL and its SHA-256. The manifest follows
`App/config/stage-release.schema.json`: version, exactly 22 unique roster IDs,
at least a neutral state per character, allowed motion names, and each raster's
SHA-256, provenance and recorded user approval. More expressions may be added to
the declared state set; missing expressions use approved neutral artwork.

Validate a candidate with:

```sh
python App/scripts/verify_stage_release.py /candidate/manifest.json \
  --asset-root /candidate/assets/stage --character-roster /candidate/roster.json
```

The validator verifies the recorded evidence and bytes; it is not human artwork
approval and never enables a release. The directory corresponds to
`App/public_frontend/assets/stage` in a future image. No new artwork is enabled
or bundled by this change. The current approved Mia pack remains the fallback.
The browser verifies manifest and image bytes before decoding, falls back on
missing/tampered images, plays motions only for a live presentation, and cancels
motion when the page is hidden or reduced motion is selected.
