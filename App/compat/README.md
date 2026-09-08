# 0.9.6 public interface compatibility target

`public-0.9.6.patch` is the exact runtime diff from the immutable baseline
`502ec99412bef843c37e4b31a53df8fa9faeb33c` to the browser compatibility changes
in `4265b5e`. It keeps the earlier interface and public v1 / public-state-2 /
IndexedDB 4 contracts. It includes the storage adapter, draft and request
lifecycle fixes, tab coordination, and the controls needed for backup and safe
refresh. It does not include the subsequent branding or timeline redesign.

The adjacent manifest binds the baseline, compatible commit, patch bytes and
every input/output file with SHA-256. To prepare a **separate candidate checkout**
at the baseline, verify these hashes and run:

```sh
git -c core.autocrlf=false apply --check /absolute/path/to/public-0.9.6.patch
git -c core.autocrlf=false apply /absolute/path/to/public-0.9.6.patch
python App/scripts/fingerprint_public_frontend.py --app-root App
```

The generated runtime module is included; this compatibility build needs no Node
runtime. Do not apply the patch to the current source tree or move the baseline
tag. The patch is a reviewable build input, not a second live frontend bundle.
Promotion still requires the ordinary candidate verification and manual gate.

The production S1 target is now `compat-096-r1`: the original patch above keeps
its exact bytes, followed by `public-0.9.6-r1.patch`. Its adjacent manifest binds
the original patch SHA, the reviewed source fix `4e97fdcd6ad1fb9423e893d2bec86638d2223ed5`,
and the before/after SHA of `app.js`. The delta preserves newly typed text when
an asynchronous arrival save confirms the already visible character/channel.
Only an actual composer owner change restores another draft.

`config/public_frontend_release.json` selects the only UI shipped by an image.
Its initial `edition: compat-096` is S1; S2 requires a normal reviewed main commit
changing that field to `current`. There is no runtime edition environment flag.
Use the deterministic builder from a trusted checkout with complete Git history:

```sh
python App/scripts/prepare_public_frontend.py
python App/scripts/prepare_public_frontend.py --verify --output App/.build/public-ui
```

The builder reads the committed selector and source blobs at the checkout's
exact HEAD, ignoring modified/untracked assets. It exports complete
`public_frontend`, `frontend/shared`, and `frontend/assets/immersive` trees from
the fixed baseline for S1 (including privacy resources), applies both verified
patches in an isolated temporary Git root, and fingerprints the resulting tree.
For S2 it exports the current commit's canonical trees. The build output must
not already exist; generated files live only under the ignored `.build` path.

`frontend-identity.json` contains the schema, track (`compat` or `current`), UI
version and SHA-256 of the sorted canonical JSON file inventory (relative path,
byte size and SHA-256 for every final static file). The bundle manifest also
records the complete source commit and baseline/patch/delta provenance. Image
verification checks the exact inventory, rejects extra/missing/changed assets,
and binds its source commit to `APP_REVISION`. The image copies one completed
bundle and does not fingerprint it again. The API exposes that immutable UI
identity separately from the backend application version.

The reliability browser tier verifies patch application and resulting hashes,
then exercises compatible old → current → compatible old with the same real v4
database, pending request UUID/snapshot and unsent draft. It needs the fixed
baseline in git history (`fetch-depth: 0` in CI). No paid requests are made.

The **unpatched** baseline has a known defect: `setChannel(..., false)` saves an
empty composer over the stored draft during boot. It is not a lossless rollback
target. The current interface additionally mirrors drafts in the v4 app-state
record `drafts_recovery_v1`. Earlier readers ignore that record; returning to the
current interface can recover a draft overwritten by the untouched baseline.
The test explicitly verifies this limitation and recovery. Backups remain the
portable recovery mechanism; they do not export model credentials or signed
state, and imported unfinished requests are not automatically replayed.

The complete-bundle browser tier additionally exercises S1 → S2 → S1 at the
same origin with actual IndexedDB v4 drafts and unfinished request UUID/snapshot,
loads privacy resources from each selected tree, and applies the deterministic
arrival transaction regression to both tracks. Synthetic API, announcement and
media fixtures are used; no paid model or real browser state is involved. This
does not make already-open, unpatched 0.9.6 tabs a supported lossless reader.
