# Project Snow Provider blind test `voice-provider-blind-test-run-03e185dea3250d531f91`

This directory is private operator material. `manifest.json` contains the
candidate-to-opaque-label map. Never copy it into `review/`.

Each `render-next` attempt is committed under `audits/` before a WebSocket is
opened. If any attempt lacks a matching result, the whole run is blocked and
must not be retried automatically. The review package is generated only after
all 24 outputs validate. No Provider voice is deleted by this workflow.
