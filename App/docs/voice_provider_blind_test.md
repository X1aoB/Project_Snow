# Provider voice blind test

`scripts/voice_provider_blind_test_ops.py` creates a private, fail-closed A/B
test for the four reviewed Qwen custom voices. It does not train a model,
publish audio, delete Provider voices, or include paralinguistic ordinals 2/3.

## Fixed contract

- Region: `cn-beijing`
- Model: `qwen3-tts-vc-realtime-2026-01-15`
- Transport: Qwen-TTS Realtime WebSocket, one prompt per connection
- Mode: `commit`
- Output: PCM s16le, 24 kHz, mono, wrapped locally as WAV
- Post-processing: none
- Final package: 12 prompt pairs / 24 WAV files
- Whole-stage synthesis ceiling: USD 0.02
- Retry rule: an attempt without a result blocks the entire run

Alibaba Cloud counts each CJK ideograph as two billing characters and other
codepoints as one. The pinned prompts contain 594 Unicode codepoints and 1,052
planned billing characters. Always include any usage from a superseded run in
`--prior-stage-usage-characters`; the tool rejects a plan that would exceed the
fixed whole-stage ceiling.

## Prepare

Preparation revalidates the immutable preflight, Beijing enrollment run, and
all four successful create receipts. It generates cryptographically random
opaque labels and never reads credentials or calls the Provider.

```powershell
python scripts/voice_provider_blind_test_ops.py `
  --voice-root <Data/Voice> `
  prepare `
  --prepared-at <ISO-8601 timestamp> `
  --prior-stage-usage-characters <integer> `
  --supersedes-run-id <superseded run if any> `
  --confirm-synthesis-only `
  --execute
```

Record the returned run ID and manifest byte SHA-256. The private
`manifest.json` contains the candidate-to-opaque-label map and must never be
copied into `review/`.

## Inspect and render

`inspect-next` is offline. `render-next` submits exactly one output.
`render-all` performs the same operation sequentially and stops on the first
uncertain attempt. Live commands require the exact run/model/region/cost
confirmations shown by `--help`.

The API key is read through the same Beijing-bound environment or `.env`
contract as enrollment. Pass the Workspace ID at runtime; it is sent in the
documented `X-DashScope-WorkSpace` header but is not copied into manifests,
audits, logs, or review files.

Every live call:

1. writes `audits/<attempt>-attempt.json` and fsyncs it;
2. waits for `session.created` and validates `session.updated`;
3. sends one `input_text_buffer.append` plus one commit;
4. collects base64 PCM until both audio and response completion;
5. sends `session.finish` and waits for `session.finished`;
6. validates duration, PCM alignment, RMS, WAV format, and hashes;
7. atomically commits the WAV, then writes the result receipt.

## Validate and finalize

```powershell
python scripts/voice_provider_blind_test_ops.py `
  --voice-root <Data/Voice> `
  validate `
  --run-id <run ID> `
  --expect-run-manifest-byte-sha256 <SHA-256>

python scripts/voice_provider_blind_test_ops.py `
  --voice-root <Data/Voice> `
  finalize `
  --run-id <run ID> `
  --expect-run-manifest-byte-sha256 <SHA-256> `
  --execute
```

Finalization succeeds only with 24 successful results and zero pending
attempts. The public `review/manifest.json` and `review/review.html` contain
opaque labels only. A privacy guard checks the exact four Provider voice IDs,
candidate keys, and private mapping fields before either file is written.

The local page stores incomplete ratings in browser local storage and can
export a ratings JSON receipt. Exporting ratings does not resolve A/B mapping;
the operator applies the fixed winner rule only after receiving the blinded
submission.

## Official references

- [Model pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing)
- [Realtime TTS guide](https://www.alibabacloud.com/help/en/model-studio/realtime-tts-user-guide)
- [WebSocket interaction](https://www.alibabacloud.com/help/zh/model-studio/interactive-process-of-qwen-tts-realtime-synthesis)
- [Client events](https://www.alibabacloud.com/help/zh/model-studio/qwen-tts-realtime-client-events)
- [Server events](https://www.alibabacloud.com/help/zh/model-studio/qwen-tts-realtime-server-events)
