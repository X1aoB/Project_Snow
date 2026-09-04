# Qwen voice provider enrollment

`voice_provider_enrollment_ops.py` binds an approved provider preflight to a
fail-closed live-operation contract. The current contract is intentionally
narrow:

- Provider family: DashScope / Qwen custom voice;
- enrollment model: `qwen-voice-enrollment`;
- synthesis target: `qwen3-tts-vc-realtime-2026-01-15`;
- current successor region: China, North China 2 / Beijing (`cn-beijing`);
- four independent ephemeral voices: Vidya A/B and Chenxing A/B;
- maximum direct creation cost: USD 0.04 in total, excluding later synthesis;
- one candidate per mutation command, with no automatic retry or batch rollback.

The offline contract does not grant rights, accept Provider terms, read a key,
upload audio, create a voice, authorize synthesis, or authorize publication.
The selected source route remains recorded as `unverified_fanwork_source`, with
no claim that written source authorization exists.

## Why enrollment is one candidate at a time

A timeout can occur after a Provider has accepted a create request but before
the client receives the voice ID. Before every request, the tool commits an
immutable attempt receipt. If no matching result receipt exists, that candidate
is considered uncertain and cannot be retried. Use `list` to reconcile Provider
state first. This prevents an automatic four-item batch retry from silently
creating duplicate paid voices.

Successful create and delete calls each receive a separate result receipt under
`Data/Voice/tts_provider_enrollment_audits/<run-id>`. Provider voice IDs stay in
that private local directory and must not enter public application config.

## Official contract facts checked on 2026-09-02

- The Qwen voice-clone API accepts action `create`, model
  `qwen-voice-enrollment`, a target model, preferred name, Data URL audio,
  transcript and language.
- The preferred name is limited to 16 alphanumeric/underscore characters. This
  tool derives a stable 13-character name for each slot.
- The reviewed sample recommendation is 10–20 seconds; all four pinned WAVs are
  mono PCM16 at 24 kHz and are within that interval.
- The published creation price is USD 0.01 for each successfully created clone.
  The documented trial quota is Singapore-only, so the Beijing contract assumes
  no free quota; deletion does not restore quota.
- Custom voices unused for synthesis for one year are documented as subject to
  automatic cleanup.
- A precise retention period for the uploaded source sample was not identified
  in the reviewed official documents. Every live create therefore requires a
  separate explicit acceptance of that uncertainty.

Primary references: [voice-clone HTTP API](https://help.aliyun.com/en/model-studio/voice-clone-design-http-api),
[voice-cloning guide](https://www.alibabacloud.com/help/zh/model-studio/voice-cloning-user-guide),
[realtime TTS guide](https://www.alibabacloud.com/help/zh/model-studio/realtime-tts-user-guide),
and [Model Studio pricing](https://www.alibabacloud.com/help/ja/model-studio/model-pricing).

## Prepare the immutable offline run

Dry-run first:

```powershell
uv run --no-project --python 3.12 python `
  App/scripts/voice_provider_enrollment_ops.py `
  --voice-root C:\Users\25685\Desktop\Myprojects\Project_Snow\Data\Voice `
  prepare `
  --preflight-id voice-provider-preflight-277b384f4a1451063562 `
  --expect-preflight-manifest-byte-sha256 d9633af150a658183933941345524eeb265101a55a0b332dddf4c023529b383a `
  --prepared-at <timezone-aware-timestamp> `
  --region cn-beijing
```

Append `--confirm-offline-only --execute` to write the two-file run directory.
That confirmation only permits the local write. Repeating an identical command
is idempotent.

Validate a written run before any later operation:

```powershell
uv run --no-project --python 3.12 python `
  App/scripts/voice_provider_enrollment_ops.py `
  --voice-root C:\Users\25685\Desktop\Myprojects\Project_Snow\Data\Voice `
  validate `
  --run-id <voice-provider-enrollment-run-id> `
  --expect-run-manifest-byte-sha256 <run-manifest-file-sha256>
```

## Inspect a redacted create request

`inspect` revalidates the run, preflight, transcript and WAV. It does not read
`DASHSCOPE_API_KEY`, and its JSON replaces both audio and text with SHA-256
placeholders:

```powershell
uv run --no-project --python 3.12 python `
  App/scripts/voice_provider_enrollment_ops.py `
  --voice-root C:\Users\25685\Desktop\Myprojects\Project_Snow\Data\Voice `
  inspect `
  --run-id <run-id> `
  --candidate-key vidya-a
```

## Live create boundary

The operator may set `DASHSCOPE_WORKSPACE_ID` / `DASHSCOPE_API_KEY`, or pass
`--api-key-file` pointing to a private UTF-8 file. For a Beijing run, if neither
key source is present, the tool safely checks `<project>/App/.env`: it first
looks for a Beijing-bound `DASHSCOPE_API_KEY`, then permits the existing
`EVIDENCE_REVIEW_API_KEY` alias only when both associated base URLs resolve to
`dashscope.aliyuncs.com`. It does not relabel or print the key. Never put an API
key on the command line. Repeated unrelated dotenv fields are ignored;
identical Provider fields and an empty placeholder followed by one non-empty
value are accepted, while two different non-empty Provider values fail closed.

`create-one --execute` additionally requires exact run, candidate, model,
region and USD 0.04 ceiling values plus four Boolean confirmations covering the
contract region upload, the unverified-fanwork risk route, Provider
terms/voice-cloning consent, and undocumented source-sample retention. Use
`--confirm-external-upload-to-region` for the Beijing successor.

Without `--execute`, `create-one` is only another redacted dry run. The tool
reads the key only after all confirmations, source validation, duplicate checks
and uncertain-attempt checks have passed.

## Reconcile and delete

`list` is the read-only Provider reconciliation command. Use it after an
uncertain attempt; do not retry first. `delete-one` is dry-run by default and
requires `--execute`, an exact repeated voice ID, a bounded reason, and
`--confirm-delete-does-not-restore-free-quota` for a live deletion.

Deleting the losing A/B voice is a later lifecycle action, not part of creation.
Keep both voices until the same-prompt blind comparison has a recorded winner.

## Region migration history

The original immutable run
`voice-provider-enrollment-run-f31ef669cfeb9af52060` remains a valid Singapore
audit artifact. It is not edited in place. The user subsequently selected the
China/Beijing route so the tool generates a distinct, hash-addressed successor
run with `--region cn-beijing`. Live operations must use the successor run; the
two region contracts and their API keys must never be mixed.

A 2026-09-02 read-only probe initially found the existing Beijing-bound project
key but received HTTP 400 / `Arrearage`. After the user recharged the account,
the same read-only route returned HTTP 200. The manually obtained Beijing
Workspace ID then passed the workspace-specific `list` check.

All four successor-run candidates were subsequently created one at a time and
reconciled through Provider `list`: four attempt receipts, four result receipts,
four unique voice IDs, and no uncertain attempt. The result-receipt SHA-256
values are recorded in the TTS corpus status report. The actual billed amount
remains unknown until billing reconciliation and is bounded by the confirmed
USD 0.04 direct-creation ceiling. No synthesis, blind test, deletion,
publication, or rollout was authorized by this creation step.
