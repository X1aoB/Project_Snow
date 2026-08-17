# Public immersive deployment

The public process exposes only `/public/v1` and the static immersive client. The existing internal `/api/v1`, attachments, voice, agents and workspace are not mounted by `public_main.py`.

The 0.8.3 client keeps text and in-person messages in IndexedDB v3 and stores one shared signed `public-state-2` world package separately. A v1 package is accepted, completed with the current 22-character presence registry and re-signed as v2. Text messages may contain one manifest-backed sticker after the message text; in-person messages continue to accept `speech` and `action`. Public presence endpoints are:

- `POST /public/v1/presence/resolve`: read a character scene without changing revision.
- `POST /public/v1/presence/transition`: move to the character or open the communicator without a model call.
- `POST /public/v1/presence/arrival`: make one idempotent 50/50 arrival decision; only the noticed branch consumes chat quota and invokes the current BYOK model.

Arrival generation failures keep the signed location transition but return no fabricated role dialogue. The daily character schedule is deterministic (HMAC-derived) and shared for all anonymous users in the Hong Kong calendar day; its signed package includes `schedule_date`, `generated_at`, and the next-midnight `expires_at`. A browser can explicitly continue yesterday's segment or start a new one without sending yesterday's transcript. Sticker media `2026.08.16.sticker.1` is a separate private candidate package with 363 resources (29 GIFs), verified by manifest and SHA256SUMS before promotion.

## Private acceptance gate

Keep Cloudflare Access enabled and do not add the MyWebsite play button until a second explicit approval. `PUBLIC_ENABLED_PROVIDERS` starts empty; add a provider ID only after a real-key smoke test for that provider succeeds.

## Server layout

- `/srv/project-snow/repo`: deploy-owned Git checkout of the full Project Snow repository.
- `/srv/project-snow/app`: symlink to `/srv/project-snow/repo/App`, so operations always run from the selected repository commit's application tree.
- `/srv/project-snow/data`: current and previous immutable data releases.
- `/srv/project-snow/runtime`: read-only serving runtime and release metadata.
- `/srv/project-snow/runtime/public-<main-sha>.env`: versioned non-secret public settings generated from the release manifest for a staged colour; the promoted colour keeps this path in its colour compose environment.
- `/etc/project-snow/public.env`: non-secret production settings, mode `0640` and group `deploy`, so Compose can read the service environment without placing secrets there.
- `/etc/project-snow/images.env`: deploy-readable fixed infrastructure image digests, mode `0640` and group `deploy`.
- `/srv/project-snow/runtime/compose.env`: the last promoted application, embedding and infrastructure image set, written atomically by `ops/promote.sh`.
- `/srv/project-snow/runtime/colours/{blue,green}.compose.env`: the last staged environment for each colour; the inactive colour can be inspected and rolled back without overwriting the active colour's metadata.
- `/srv/project-snow/releases/colours/`: per-colour release markers and manifests. `/srv/project-snow/releases/active-colour` is changed only by `ops/promote.sh`.
- `/srv/project-snow/media/current`: the atomically promoted, independently verified avatar media package. It is mounted read-only into both API colours.
- `/srv/project-snow/media/releases/<version>`: immutable, independently verified media releases. A staged API colour pins `PUBLIC_MEDIA_ROOT` to its release directory so a new package cannot invalidate the active or rollback colour during acceptance.
- `/srv/project-snow/media/stickers/current`: the atomically promoted, independently verified sticker media package. It is mounted read-only into both API colours; `ops/fetch-promote-sticker-media.sh` verifies 363 resources and 363 thumbnails before switching the symlink.
- `/etc/project-snow/secrets`: root-only secret files, mode `0700`; individual files mode `0600`. The public image starts with a minimal root entrypoint, copies only its approved secret files into a private container tmpfs with mode `0400`, and immediately drops to the unprivileged `snow` user before Alembic, Uvicorn or admin code runs. Secret values are never placed in Compose environment interpolation.
- `/etc/project-snow/cloudflared`: root-only named-tunnel configuration and credentials. Ingress maps only `snow.xiaob.dev` to `http://caddy:8080`, followed by an explicit `http_status:404` catch-all.
- Caddy port `8080` exists only on the private Docker `edge` network; it is not published on the host.
- Candidate API ports `127.0.0.1:18081` (blue) and `127.0.0.1:18082` (green) are loopback-only and exist solely for SSH-tunnel acceptance; they are not reachable through the firewall or Cloudflare Tunnel.
- `127.0.0.1:19090`: feedback administration through an SSH tunnel only. The admin container keeps its
  database access on the internal `data` network and uses a dedicated `management` bridge solely so Docker
  can publish the loopback-bound port; Docker's userland proxy is enabled explicitly for this binding.

Run Alembic before starting a new application colour. Never let the web process auto-create production schema. Application migrations follow expand/contract compatibility with the previous application image.

The local deployment entrypoint fetches `origin/main`, refuses a SHA that is not an ancestor of that ref, checks out the exact commit as detached HEAD, verifies it, and only then enters `App/ops/deploy.sh`. This prevents a caller from staging an unmerged feature-branch commit or running deployment scripts from a different checkout than the selected image SHA.

Pin PostgreSQL, Qdrant, Neo4j, Caddy, cloudflared and Squid by digest in `/etc/project-snow/images.env`. The local deployment command requires the application and embedding digests generated by the selected, CI-passing `main` SHA. Use `ops/fetch-promote-media.sh <version> stage-only` to install a verified media release without changing the legacy `current` symlink; the candidate colour then uses its pinned release path. `App/scripts/deploy.ps1` stages only the inactive colour and runs direct health/smoke checks; it does not start Caddy or change traffic. After private acceptance, `App/scripts/promote.ps1` invokes `ops/promote.sh`, which performs a candidate and post-switch smoke test before atomically updating the promoted environment and colour marker. A failed post-switch smoke automatically restores the previous Caddy upstream.

Private acceptance can use an SSH tunnel such as `ssh -p 43556 -L 18081:127.0.0.1:18081 deploy@server` for blue. The browser/API test client should send the production Origin header when exercising write endpoints; no development origin is enabled in production.

Store the Neo4j password alone in `secrets/neo4j_password` for the API. Store the Docker-image-compatible `neo4j/<password>` value separately in `secrets/neo4j_auth`; the Neo4j container reads it through `NEO4J_AUTH_FILE`, so the password is not placed in Compose interpolation or process arguments.

Start from `ops/public.env.example` and `ops/images.env.example`. The first file must remain non-secret; all database URLs, API cryptographic keys, Turnstile secret, Qdrant key, the admin token and the SMTP password are supplied through `/etc/project-snow/secrets` file mounts. The infrastructure image file contains only public image references and immutable digests. `PUBLIC_FEEDBACK_EMAIL_TO` defaults to `admin@xiaob.dev`; the worker sends QQ only after temporary decryption and never stores it in the outbox.

The public API uses `HTTPS_PROXY=http://egress-proxy:3128`; Squid permits CONNECT only to the five official model API domains and Cloudflare Turnstile. This is an additional SSRF boundary on top of fixed adapter URLs and redirect rejection.

## Resource policy

The production Compose file caps API, PostgreSQL, Qdrant, Neo4j, embedding, Caddy and cloudflared. Docker JSON logs rotate at 10 MB with five files. PostgreSQL uses `shared_buffers=1GB`, `effective_cache_size=4GB`, `work_mem=8MB` and `max_connections=50`.

## Data publication

`export_publishable_graph.py` quarantines reviewed nodes that lack traceable page evidence or use unsupported node types. `build_data_release.py` then packages the derived documents, 512-dimensional vectors, FTS5 database, publishable graph, persona profiles, attribution index and license metadata in the exact read-only directory layout consumed by the public API. Raw Wiki pages and images are not copied into the application image or Git history. `ops/promote-data.sh` verifies every byte and atomically moves the `current`/`previous` symlinks; deployment loads a versioned Qdrant collection before switching its alias and loads a versioned Neo4j dataset before switching the active pointer.

Before a data build, refuse work when disk usage is at least 70% or free space is below 12 GiB. Run builders with a maximum of eight CPUs and 8 GiB of memory. A failed build never changes the `current` symlink or Qdrant/Neo4j active version.

## Backups and recovery

Use restic to an R2 bucket for daily PostgreSQL logical dumps and release manifests. Qdrant snapshots are created when the data version changes; Neo4j is rebuilt from the checked graph import package. Run cleanup immediately after restoring PostgreSQL so feedback older than 30 days is hard-deleted. Private backup retention is seven days; current and previous stable data releases remain available.
