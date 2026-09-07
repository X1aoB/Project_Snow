# 小吉终端 implementation ledger

Baseline: `502ec99412bef843c37e4b31a53df8fa9faeb33c` / public `0.9.6`.
Production promotion requires candidate acceptance; implementation does not imply
production activation. This ledger records evidence rather than inferred completion.

| Workstream | Status | Required evidence |
| --- | --- | --- |
| Protected image GC and disk capacity | first production batch verified | 36 unused Snow images removed; 30 protected digest references; 94% → 64% used; 16.9 GiB free |
| Retention maintenance | installed and verified | root-owned helper from d73e3e2; 2026-09-07 07:48:48 UTC successful; all 25 container IDs/digests unchanged |
| Immutable baseline / full restore | isolated component restore passed; whole-host drill pending | 10 images / 159 blobs verified; restored DB/assets/API and rebuilt retrieval passed in 279.41 seconds |
| CI classification / PostgreSQL / attestations | implemented, online issuance pending | deletion/rename tests, 8 real PG scenarios, strict source/signer/run/attempt identity, server verifier before candidate execution |
| Backend budgets / lifecycle / modular core | first modular core implemented | actual shared budget, bounded DB adapter, leases/drain, loopback/local boundary and 3-second retrieval budget |
| Review preservation / versioned data / quality | first batch implemented | cross-process review protection; no activation GC; 22 × 8 offline behavior cases |
| Browser persistence / TS / UI | implemented and locally verified | 66 Python/UI checks, 15 subtests, extra generic-stage browser check, 6 Node checks; source typecheck/build |
| 小吉终端 name and restored icon | implemented and packaged | AI source disclosed; Electron 44.2.0 smoke and Windows preview package, dependency audit 0; binary has no trusted code-signing certificate |
| All-character stage assets | external drawing task | requires all 22 approved; runtime integration remains disabled until ready |
| All-character TTS | separate task active | CNY 300 ledger and complete local blind review required; public voice disabled |
| Production promotion | pending manual acceptance | exact release and receipt approved after gates |

Initial host audit: 16 vCPU, ~16 GiB RAM, 49 GiB filesystem, 94% used.
Backup timer last succeeded; retention timer failed while Compose tried to
reconcile a management network occupied by an old admin container. Dify shares
the host and is outside the mutation scope.

## Recorded implementation evidence (2026-09-07)

- Integration branch `codex/xiaoji-overhaul`, isolated from the original checkout.
- Reviewable implementation: [PR #36](https://github.com/X1aoB/Project_Snow/pull/36). Candidate version `0.10.0-rc.1`; desktop preview remains independently versioned `0.5.0`.
- Annotated code tag `audit-baseline-20260907-0.9.6` points to the fixed baseline.
- Root-only host anchor: `/srv/project-snow/releases/anchors/baseline-502ec99-0.9.6-20260907`.
- Permanent restic tag `project-snow-pinned-502ec99412bef843c37e4b31a53df8fa9faeb33c`: snapshot `828b0a9f` contains database dump, configuration/secrets archive and installed data/media (2,727 files / 1.144 GiB); `48398acd` contains 10 baseline Docker images; `fd81b13a` includes the baseline code archive and recovery metadata. IDs changed when the permanent tag was applied; use these current IDs, not the earlier snapshot IDs. These snapshots have no daily retention tag.
- Actual encrypted readback: all 159 image blobs (1,296,395,349 bytes) matched their SHA256 identities, all 10 expected images were present, gzip CRC passed, and recovered code/configuration/PostgreSQL files matched the recorded hashes. No Docker image was loaded or production database overwritten during this check. This is not an empty-host recovery or an RTO measurement.
- Host configuration archive SHA256: `a83575f2ea6e237e7c3c74a65eff031d2314230bbec4d12f2a8ead3f8f214895`.
- Applied image cleanup plan SHA256: `d3948453be3c9b4e34f895a3e3635fcdf2c10367353f1139f99a3177b1bb5458`. Both container image references and Docker OCI reference counts protect images; no container, volume, data release or Dify image was deleted.
- Capacity after maintenance: 18,168,623,104 bytes free, above the 10 GiB minimum. Public health remains `ok / 0.9.6`.
- Cleanup removed only expired state: request cache 1, rate limits 137, feedback attempts/dedupes 3 each; feedback and mail outbox 0.
- Real PostgreSQL 16: 8 scenarios passed in isolated temporary databases (5 initial checks, 2 role/failure-migration checks, 1 new-worker-crash/old-API recovery check). Tests execute actual baseline `PublicStore` code on the expanded schema. Production PostgreSQL was not used; synthetic roles do not mean production credentials were rotated.
- Real Neo4j 5.26.29: 4 corruption scenarios (node properties, relationship properties, endpoint mismatch, missing label) plus 6 data tests passed; valid load/reuse preceded corruption. Temporary database containers and SSH tunnels were removed.
- Unified PowerShell validation: 750 portable tests and 566 subtests passed (16 environment-dependent skips, 18 private-runtime deselections); 51 browser tests and 11 subtests passed; 6 Node tests, TypeScript, deterministic build and Electron brand smoke passed. Local Docker was unavailable, so this invocation skipped Compose model validation; CI and earlier isolated Docker checks are separate evidence.
- Maintenance installer failure injection ran on Linux in a temporary filesystem: 19 passed, zero skipped. Original bytes/modes/symlinks are retained in private receipts; publication or daemon-reload failure restores prior files and units, and incomplete rollback is an explicit failure.
- Complete isolated baseline restore passed at 2026-09-07 09:39:42 UTC in 279.41 seconds: 8 PostgreSQL tables, migration head `20260819_0004`, zero unvalidated constraints, 1,148 data/avatar/sticker files, rebuilt embedding/Qdrant/Neo4j, and API readiness/full health. All temporary containers, networks and volumes were cleaned. Root receipt: `/srv/project-snow/backups/restore-drills/run-d0303d777c9b471289feb96e33ccae42.json`, SHA256 `a6de02e4ae035f67754aec0bb7f865a1f05b40c60a8825105ac15ead50e13780`. Earlier failed probes led to internal-container health checks and waiting for the final TCP PostgreSQL server. No production database, model or mail call was used; this is not an empty-host RTO measurement.
- Installed GitHub CLI 2.100.0 from its verified official archive (SHA256 `e4d4bb4498e8d007abe545b6568926793ace1b6447da598294a610018cb164be`), with no host GitHub credential configured. Trivy 0.74.0 is pinned to `sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969`; the first actual scan failed and a diagnostic retry is in progress, so no vulnerability-pass claim is made.
- Browser reliability: disabled Web Storage, persisted pruning, same-timestamp paging, mobile drawer, cross-tab drafts, transactional import failure and 10,000-message history verified. Supported old→new→old uses `compat/public-0.9.6.patch`; raw 502ec99 has a pre-existing boot draft-overwrite defect. The untouched baseline remains available, and a separate draft journal lets the new UI recover after visiting it. See `compat/README.md`.
- Real Caddy Docker test: reload preserved all 40 SSE chunks, new connections reached green, and restart kept green (1 passed, 7.37 seconds). Three temporary containers and their isolated network were removed. This does not replace the first production persistent-mount maintenance acceptance.
- Official actionlint v1.7.12 checked all four workflows with zero diagnostics; TypeScript check/build produced no bundle drift. Windows Electron brand smoke verified name, decoded icon and disabled Node access; portable build succeeded with stable application/user-data identifiers.
- GitHub CI passed for `1aea3b5`: [run 34106294698](https://github.com/X1aoB/Project_Snow/actions/runs/34106294698). Additional request-failure and installer corrections will be verified on the final PR commit before merge.
- Actual visual inspection covered desktop 1440×900, mobile 390×844, native browser 200% zoom and seven layout scenarios using synthetic data. Fixed a status banner obscuring mobile navigation; no horizontal overflow or input-area overflow remained. A short viewport simulated keyboard space, without claiming a real Android/iOS keyboard test.
- Release proof workflow and server verification are implemented in the branch. End-to-end GitHub issuance, candidate staging and manual production promotion remain outstanding.

Do not treat this ledger, passing unit tests, or a prepared candidate as approval of new artwork, TTS voices, production promotion, or completion of the entire overhaul.

## Remaining acceptance and follow-up scope

- Publish the reviewed branch and pass GitHub CI; install the reviewed runner/helper generation in a dedicated maintenance operation; exercise real Sigstore issuance/verification and candidate staging. The new proof workflow cannot issue production evidence while it only exists on an unmerged branch. Main must remain stable for a CI cycle; a newer main commit causes an older completion event to be intentionally skipped.
- Complete a separately timed empty-host rehearsal. RPO ≤24 hours and RTO ≤4 hours are targets, not measured achievements. Connect an operator-chosen external alert destination; current monitoring writes local journal transitions only. Actual in-service digest vulnerability scanning is still in progress.
- Continue extracting the large compatibility facade and remaining plain JavaScript into the typed/runtime modules. Local review JSONL writes now coordinate processes but are not a crash-atomic multi-file database. Production role separation, full capacity/load measurements and real-model persona evaluations remain separate acceptance items.
- Artwork production and human approval for all 22 characters remain with the drawing task. The generic stage loader is disabled until a complete validated package is supplied. No stage approval is inferred from base portraits or synthetic browser fixtures.
- TTS creation resolved to active task `01a07ac7-6477-7a10-8101-83fe68a50af9`, titled “Project Snow 全角色 TTS 候选制作与待审核交付”. No TTS spend or candidate completion is claimed by this integration task. VoiceLab migration, playable samples and the CNY 300 ledger belong to that task.
- Production remains 0.9.6 / 502ec99 until an exact candidate is accepted and manually promoted. Component release versions and final release notes must be set for that accepted release.
