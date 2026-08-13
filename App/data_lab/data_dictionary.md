# Data dictionary

| Field | Type | Description |
|---|---|---|
| event_id | string/UUID | Synthetic idempotency key |
| event_type | enum | `chat_completed` or `feedback_submitted` |
| occurred_at | timestamp | UTC event time |
| character_id | string | One of the public 22-character registry ids |
| provider | string | Synthetic provider label |
| model | string | Synthetic model label; never an API key |
| request_stage | string | `generation`, `retrieval`, `feedback` or `complete` |
| error_code | nullable string | Standardized synthetic error code |
| degraded_services | array<string> | Synthetic dependency degradation labels |
| latency_ms | integer | Synthetic end-to-end latency |
| feedback_topic | nullable string | Synthetic topic category, no raw text |

The schema intentionally excludes API keys, raw prompts, raw model output, QQ and source IP addresses.
