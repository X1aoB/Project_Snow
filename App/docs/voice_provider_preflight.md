# Voice provider offline preflight

`voice_provider_preflight_ops.py` converts the approved Vidya/Chenxing A/B
selection into an immutable **offline-only** provider handoff package. It does
not read credentials, call DashScope, create a custom voice, train a model, or
authorize cost.

The generated directory contains only:

- `manifest.json`: four hash-pinned, independent candidate references;
- `blind_test_plan.json`: operator-side A/B prompts, rubric, blindness and
  decision rules;
- `README.md`: the handoff boundary and required next approval.

Source audio is never copied. Each candidate references one approved compacted
WAV and its exact transcript. Vidya A/B and Chenxing A/B are never concatenated
or jointly weighted: if a later provider run is approved, each slot becomes one
ephemeral voice so that the winning slot can be selected by a fair paired test.

The two nonlexical events (ordinals 2/3) stay out of base TTS enrollment. Their
receipt remains pinned as `pending_human_event_qa`; a future, separately
authorized experiment may compare base TTS alone with base TTS plus a curated
recorded-event bank. This preserves breathy/murmured delivery without letting
nonlexical clips distort the base speaker identity.

## Build and validate

Dry-run first. A dry-run does not create `tts_provider_preflights`:

```powershell
uv run --no-project --python 3.12 python `
  App/scripts/voice_provider_preflight_ops.py prepare `
  --voice-root C:\Users\25685\Desktop\Myprojects\Project_Snow\Data\Voice `
  --prepared-at 2026-09-02T15:28:35.5396116+08:00 `
  --expect-review-byte-sha256 40e7af00e16460d48f460290259a7f2f1bc965c065ee069b30b138344c9029ee `
  --expect-paralinguistic-byte-sha256 c792986bcc2da3afad31ced8850a5d1cc086d77d82c591ec8693b84d001afc08
```

Append `--confirm-offline-only --execute` to write the three-file immutable
package. Repeating the exact command is idempotent. The confirmation names the
scope boundary; it is not Provider enrollment permission.

Validate a written package using the ID printed by `prepare`:

```powershell
uv run --no-project --python 3.12 python `
  App/scripts/voice_provider_preflight_ops.py validate `
  --voice-root C:\Users\25685\Desktop\Myprojects\Project_Snow\Data\Voice `
  --preflight-id <voice-provider-preflight-id> `
  --expect-manifest-byte-sha256 <manifest-file-sha256>
```

Validation rebuilds all three artifacts from both source receipts and rehashes
the four transcript/WAV pairs. It fails closed if a source byte, candidate,
event classification, gate, plan, or README changes.

## Still required before a provider call

The manifest keeps every execution gate false and waits for one new, explicit
authorization covering all of the following:

- source-audio rights and derived-voice consent;
- Provider terms and voice-cloning consent;
- credential access and target workspace;
- model, region, retention and privacy choices;
- an explicit cost ceiling.

Provider registration, training, publication, and rollout are deliberately not
implemented by this preflight command. The separate
`voice_provider_enrollment_ops.py` command can now create an immutable
Singapore/model/cost execution contract and redacted per-candidate dry runs;
live Provider calls remain fail-closed until its additional confirmations and
credentials are supplied. See `voice_provider_enrollment.md`.

## Current local package

The 2026-09-02 run created
`voice-provider-preflight-277b384f4a1451063562` with semantic manifest SHA-256
`340585bf0dc35eb20a99b9a51cec3eb66431d16ab72a4353572e0a64eb30b71e`
and manifest-file SHA-256
`d9633af150a658183933941345524eeb265101a55a0b332dddf4c023529b383a`.
An exact replay returned `existing_valid`; full reconstruction validation also
passed with zero provider interactions.
