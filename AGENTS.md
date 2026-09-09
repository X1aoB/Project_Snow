# 小吉终端 / Project Snow engineering contract

The public product name is 小吉终端. Stable repository, API, deployment, cookie,
IndexedDB and Electron application identifiers remain Project Snow identifiers.

## Delivery

- Work in an isolated worktree and make reviewable, tested commits. Coordinate
  file ownership before parallel edits; never reset another task's work.
- Preserve baseline commit 502ec99412bef843c37e4b31a53df8fa9faeb33c (0.9.6).
- Production promotion is manual after a concrete candidate acceptance receipt.
  Automated builds, verification and safe candidate preparation are allowed.
- On 2026-09-09 the user explicitly authorized this coordinating task to monitor
  the approved U4 character delivery and begin production rollout when it is
  complete. This scoped authorization permits promotion after fresh, exact
  candidate acceptance; it does not waive artifact review, CI, rollback gates,
  or authorize unrelated releases. Do not ask for duplicate generic permission.
- Ordinary application staging must not mutate shared infrastructure or require
  a new TLS key. Resource capacity and a reconstructible rollback anchor are gates.
- Never globally prune a shared host. Protect every running/stopped container,
  baseline/current/rollback/candidate release reference and unrelated applications.

## Data and compatibility

- Data is read-only source material. Build derivatives into versioned runtime
  directories; preserve review candidates, decisions and provenance on rebuild.
- Keep public v1, public-state-2, IndexedDB v4 and request snapshots compatible.
  Database changes use expand/contract and old/new/old verification.
- Do not log or commit credentials, raw IPs, chat bodies or private Provider IDs.
  Results of uncertain paid requests must be reconciled rather than retried blindly.
- Review conclusions must identify their evidence. Automated QA is not human approval.

## Active workstreams

- Main integration worktree: codex/xiaoji-overhaul. Public UI, backend and ops
  are independently owned during parallel implementation; coordinate with root.
- TTS is a separate task, maximum CNY 300 new spend, ready for human review only.
- The 2026-09-09 instruction additionally authorizes the selected voices in a
  private loopback test application. Preserve prior approvals and the shared
  CNY 300 cap; public voice remains disabled. Local implementation is owned by
  codex/local-voice-test, with source material in the untouched cd1e worktree.
- Character artwork remains owned by its ongoing drawing task. Runtime integration
  is gated by all 22 characters' approved asset manifests; static fallbacks remain.

## Validation

Use synthetic/temporary fixtures in tests, never live user runtime. Keep meaningful
behavioral regressions for faults, persistence, charging and recovery. Use real
PostgreSQL for migration compatibility; no paid model calls in ordinary CI.
