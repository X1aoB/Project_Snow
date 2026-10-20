# Snow retention adapter handoff

`modules/snow-proactive-client.js` keeps proactive-message consent and its model
session separate from the model credential. The host owns the credential and
selects the retention policy; the client only persists a consent token, model
identity digest, model session ID, confirmation time, and consent expiry.

## Adapter contract

The adapter passed to `createProactiveClient()` may provide:

```js
{
  retention: "tab", // or (context) => "tab" | "2h" | "24h" | "3d" | "7d"
  storageForRetention: kind => kind === "tab" ? sessionStorage : localStorage,
  // `getCredentialStorage` is an accepted compatibility alias.
}
```

Only these exact retention strings are accepted: `tab`, `2h`, `24h`, `3d`, and
`7d`. Missing or invalid values fail closed to `tab`. The client passes only
`tab` or `local` to the storage hook: `tab` uses the tab-scoped store and every
other accepted value uses the local store. The host should use the same policy
when writing and restoring the actual credential.

`storageForRetention` (preferred) or `getCredentialStorage` may return a store
synchronously or as a promise. The client understands native `Storage`
(`getItem`/`setItem`/`removeItem`), `load`/`save`/`clear(key)`, and
`get`/`set`/`delete` adapters. A zero-argument `clear()` is ignored so a
namespace-wide store cannot be erased accidentally. Hook failures leave the
client memory-only and do not block ordinary in-tab use.

The storage key is `project-snow-public:proactive-consent:v1`. Stored values are
whitelisted and never contain the model credential. Consent is bounded to the
shorter of the server expiry and 12 hours after confirmation. Model/provider,
credential, retention, history invalidation, explicit clear/disable, and expiry
remove the old authorization before a new model session can be created.

The host still needs to pass `retention` and one of the storage hooks when it
installs the runtime. The client does not discover or write the host's
credential store by itself.

## Release and migration handoff

- Run migration `20261020_0010` before starting the new candidate. It only adds
  `public_credential_revocation` and its expiry index; repeat upgrade is safe.
- If the migration or the candidate startup/readiness check fails, stop the
  rollout immediately. Do not stage BLUE, enable the release flags, or retry a
  paid request. Resolve the database failure first.
- A rollback to the online `fff998b` code keeps the new table in place. The old
  code ignores it and continues to read ordinary request data. Retained v2
  credentials are sealed with `project-snow-byok-v2`, so old code rejects them;
  the user must enter the API Key again. Tab-scoped v1 credentials remain
  compatible across the rollback.
- `PUBLIC_PROACTIVE_MESSAGES_ENABLED` and `PUBLIC_BYOK_RETENTION_ENABLED` are
  exact `true`/`false` candidate switches and default to `false`. The proactive
  server path remains closed unless its switch and the reviewed scene-action
  prerequisites are enabled together.
- Candidate acceptance must observe zero paid model calls and zero calls to
  `/public/v1/byok/session`, `/public/v1/byok/revoke`,
  `/public/v2/rendezvous/consent`, and `/public/v2/rendezvous/message`.
