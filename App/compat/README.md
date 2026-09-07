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
