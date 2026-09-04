# Local selected-voice runtime

Project Snow can route local voice replies to the terminally selected Beijing
Qwen VC profile. The profile is private, style-specific, and fail-closed.

## Runtime contract

- Provider region: `cn-beijing`
- Model: `qwen3-tts-vc-realtime-2026-01-15`
- Output: mono 24 kHz 16-bit PCM wrapped as WAV
- Style slots: `neutral`, `breathy`, and `heightened`
- Paused slots never fall back to another style or character voice
- Provider voice IDs and Workspace IDs never appear in API responses
- Non-lexical paralinguistic events remain outside this runtime
- The model receives no unsupported `instructions` field

The private profile is generated from the terminal human preference receipt:

```powershell
python scripts/voice_runtime_profile_ops.py `
  --voice-root C:\path\to\Project_Snow\Data\Voice `
  prepare `
  --terminal-round-id voice-preference-round-9fcc6ed7447cbfa10728 `
  --prepared-at 2026-09-04T16:00:00+08:00 `
  --execute
```

Building or validating the profile is offline and costs nothing. The generator
prints only counts and hashes; private candidate references, voice IDs, and the
Workspace ID stay inside the private manifest.

## Activation

Runtime Provider calls remain off by default. After an ongoing usage budget is
separately authorized, set:

```dotenv
LOCAL_VOICE_ENABLED=true
```

The runtime reads `DASHSCOPE_API_KEY` by default and automatically discovers the
single compatible profile beneath `Data/Voice/tts_runtime_profiles`. An explicit
private profile path can be supplied with `LOCAL_VOICE_PROFILE_PATH` when more
than one compatible profile exists. `LOCAL_VOICE_API_KEY` is an optional
dedicated credential override.

The application uses the selected runtime only for characters present in the
private profile. Other characters continue through the existing generic TTS
provider route. If a selected style is paused, the API returns
`status: unavailable`, `reason: style_slot_paused`, and `fallback_used: false`
without opening a Provider connection.

When the per-thread voice preference is enabled, an in-person reply always
requests voice and is marked for automatic playback. A text-channel reply uses
an idempotent 25% offer rate, raised to 45% for conservatively detected strong
emotion; text-channel audio is rendered as a playable voice message but is not
auto-played. The decision is stable for the same message, so a client retry does
not create a fresh random chance. The rates can be adjusted with
`LOCAL_VOICE_TEXT_PROBABILITY` and `LOCAL_VOICE_EMOTION_PROBABILITY`.

## Real-provider smoke gate

`scripts/voice_runtime_smoke_ops.py` prepares an immutable five-output plan,
writes an attempt receipt before each WebSocket connection, refuses automatic
retry after an uncertain attempt, and creates a non-blind local listening page.
The current completed smoke run used 179 Provider-reported characters, with an
estimated cost of USD 0.0025660187 under its authorized USD 0.005 ceiling. It
called only the five locked slots and did not open a fourth preference round.
