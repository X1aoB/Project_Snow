# Project Snow voice corpus archive

This directory preserves the local recording, alignment, review, and TTS
evaluation material produced during the Vidya and Chenxing voice investigation.
Binary media is stored with Git LFS so the ordinary Git history contains only
small pointer records.

## Current conclusion

- Four reference recordings were used for the completed Beijing Qwen voice
  enrollment: Vidya A/B and Chenxing A/B.
- The terminal preference result locks five runtime style slots and pauses one:
  Vidya's breathy lexical slot. The paused slot must not fall back to another
  voice or trigger automatic resampling.
- The final runtime smoke run completed all five locked slots successfully. It
  reported 179 billable characters and an estimated cost of USD 0.0025660187
  under the authorized USD 0.005 ceiling.
- No fourth preference round is required. Ongoing paid runtime use remains off
  until a separate operational budget is approved.

The four enrollment references, as actually used, are:

| Character | Slot | Reference |
| --- | --- | --- |
| Vidya | A | `recording_dialogue_comparisons/voice-recording-dialogue-comparison-a62af942ac96e7bc059e/A/compacted.wav` |
| Vidya | B | `recording_dialogue_comparisons/voice-recording-dialogue-comparison-a62af942ac96e7bc059e/B/compacted.wav` |
| Chenxing | A | `recording_dialogue_comparisons/voice-recording-dialogue-comparison-9eaa4601e56ff9917069/A/compacted.wav` |
| Chenxing | B | `recording_dialogue_comparisons/voice-recording-dialogue-comparison-9eaa4601e56ff9917069/B/compacted.wav` |

The final listening page is
`tts_runtime_smoke_tests/voice-runtime-smoke-run-9b5aac516b45bdfcff53/review/review.html`.
It is a sanity check for obvious wrong voice, clipping, corruption, or routing
errors; it is not another abstract scoring round.

## Directory map

- `source_batches`, `raw`, `clean`, and `recording_normalized`: captured and
  normalized source material.
- `span_*`, `recording_*reviews`, `recording_*audits`, and `quarantine`:
  transcript, boundary, alignment, and quality-control evidence.
- `recording_dialogue_*`: dialogue-oriented compositions and human comparison
  packages.
- `tts_provider_blind_tests`, `tts_preference_challenger_runs`, and
  `tts_preference_tournaments`: synthesized evaluation audio and blinded human
  decisions.
- `tts_runtime_smoke_tests`: final locked-route smoke audio and review page.
- `tts_corpus_status`: chronological technical conclusions and gate status.
- `corpus-inventory.json`: SHA-256 and byte size for each committed payload file.

## Deliberate exclusions

The following remain local and are ignored by Git:

- `.runtime` and all `.bin`/`.safetensors` model files;
- runtime profiles containing the private Workspace ID or Provider voice IDs;
- provider enrollment/preflight records and provider attempt receipts;
- unblinded candidate maps and other records that reveal private Provider
  routing identities;
- every API key and credential.

These exclusions do not prevent reconstruction of the corpus review history.
The implementation and reproducible operators live in `App/scripts`, while the
runtime contract is documented in `App/docs/voice_runtime.md`.

## Git LFS

Install Git LFS before checking out the media:

```bash
git lfs install
git lfs pull
```

WAV, MP3, and MKV files below this directory are LFS objects. JSON, Markdown,
HTML, and transcript files remain ordinary Git blobs.

## Rights and redistribution warning

Some source recordings and their derivatives may contain third-party or
fan-sourced character audio. Their presence in this research archive is not a
license grant and does not establish permission for public redistribution,
commercial voice cloning, or paid rollout. Before publishing this branch or
using the corpus commercially, the project owner must complete an independent
rights and consent review.
