# Google indexing review — 2026-11-10

Scope: `snow.xiaob.dev`, based on `origin/main` at
`c48663d79524210acfc2cc27f00dff8e9ec31dd6`. Work is isolated on
`codex/google-indexing-fix`. This review does not deploy a release or submit a
Search Console validation/indexing request. `xiaob.dev` is investigated by the
coordinating task in its separate website repository.

## Evidence and conclusion

The coordinating task checked production HTTP on 2026-11-10: both websites' home
pages returned 200, `index, follow` and the expected canonical URL. Snow's
`robots.txt` and `sitemap.xml` returned 200. The production robots response still
contains `Disallow: /public/v1/`, `/api/` and `/workspace/`, without an exception
for page-render resources. Cloudflare prepends managed rules for several AI
crawlers; Googlebot is not in that denied list and the managed search signal is
`search=yes`.

The sanitized [HTTP snapshot](evidence/google-indexing-public-http-20261110.json)
records 70 URL checks at `2026-11-10T03:48:04.281374+00:00`: 67 returned 200 and
three returned expected 404s (two diagnostic nonexistent URLs and Snow's private
`/workspace/`). All 37 sitemap URLs (Snow 2, MyWebsite 35) returned 200, had a
canonical matching their final URL and had no HTML/header `noindex`. No public
internal-link 404 was found by this check. These observations describe current
HTTP responses, not Google's indexing records.

The [redirect/user-agent snapshot](evidence/google-indexing-redirects-ua-20261110.json)
records a second confirmed defect: `https://snow.xiaob.dev/privacy` returns 307
with `Location: http://snow.xiaob.dev/privacy/`, then Cloudflare redirects back to
HTTPS. StaticFiles derives that absolute slash redirect from the internal HTTP
ASGI scheme. The same snapshot records 200 responses with a Googlebot user-agent
header for the public pages and the three JSON dependencies. That header check
does not simulate a verified Googlebot network source.

The live robots response has `Cache-Control: max-age=14400`. A release must be
checked at the actual edge response; a corrected build alone does not prove the
cached Cloudflare response is current.

MyWebsite's read-only source review found intentional `noindex` on
`src/pages/search.astro:12`, `src/pages/statistics.astro:5` and
`src/pages/404.astro:5`. Production `/search/` and `/statistics/` return 200 with
`noindex, nofollow` and are absent from the sitemap; a genuinely unknown URL
returns 404. No incorrect indexing configuration was established there and no
MyWebsite source was changed. The inaccessible Search Console examples cannot
be assumed to be these intentional exclusions.

Repository evidence agrees with the live policy:

- `public_frontend/index.html` preloads `/public/v1/config` and
  `/public/v1/characters`; `app.js` starts both requests in `boot()` and awaits
  them in `loadConfig()` / `loadCharacters()`.
- `public_frontend/privacy/privacy.js` loads `/public/v1/config` and
  `/public/v1/attributions` to populate privacy/provider/material notices.
- The previous robots policy denies all three requests. An independent Protego
  parse of that policy returned false for each Googlebot request.
- Both public HTML documents already have indexable metadata and canonical
  URLs. The sitemap lists only `/` and `/privacy/`. Production uses
  `OriginEdge.Caddyfile` → `Caddyfile` → `public_main.py`; static unknown paths
  return real 404s. The development nginx SPA fallback is not this production
  route.
- The selected frontend is `current` 1.2.0. `prepare_public_frontend.py` exports
  the committed static tree and the production Dockerfile copies the verified
  bundle, including robots and sitemap files.

This confirms a **crawl/render obstruction**: Google cannot fetch these page
dependencies under the old robots policy. It does not establish why any specific
URL in the Google email returned 404, or establish that either whole site is
unindexed. The email's affected URL examples were unavailable because Search
Console browser access timed out. No 404 route is redirected speculatively.

## Change

Allow only the exact paths `/public/v1/config`, `/public/v1/characters` and
`/public/v1/attributions` in robots. End anchors prevent opening similarly named
paths or descendants. All other v1 API paths remain disallowed; v2 API paths are
also explicitly disallowed, alongside the existing internal API/workspace rules.

The three read-only JSON responses carry `X-Robots-Tag: noindex`. Crawlers can use
them to render HTML, while the JSON endpoints themselves are excluded from search
results. HTML indexability, canonical URLs, sitemap scope, session/credential
boundaries and real 404 behavior stay intact. No data or compatibility format is
changed; baseline `502ec99412bef843c37e4b31a53df8fa9faeb33c` is untouched.

Add an explicit GET/HEAD 308 redirect for `/privacy` to the relative
`Location: /privacy/`, preserving query parameters. This removes the HTTP
downgrade and extra redirect hop without globally trusting forwarding headers or
changing unknown/private URL behavior.

## Validation

- The four new FastAPI regressions passed. They verify sitemap documents return
  200 HTML with the expected canonical and index tags; unknown/internal paths
  return 404; render JSON is readable without cookie, quota mutation or a model
  call, and returns `noindex`; robots exceptions remain exact and limited; GET
  and HEAD redirects remain relative with the internal HTTP scope and a public
  HTTPS forwarding header, including requests with query parameters.
- Final command from `App/`:
  `python -m pytest tests/test_public_api.py tests/test_public_frontend_bundle.py -q`
  returned **83 passed, 2 skipped, 1 warning** in 42.78 seconds. The warning is the
  installed Starlette/httpx test-client deprecation. The initial bundle assertion
  found only Windows CRLF versus Git LF in a text file; it was corrected to
  compare normalized text content.
- The bundle suite verifies the current immutable frontend retains the robots
  policy and sitemap, and preserves the separate compatibility bundle.
- An independent Protego check accepts the home/privacy pages and the three
  render paths, and rejects API descendants, similarly named paths, attribution
  downloads, credential/chat/v2 paths and the internal API/workspace paths. This
  used an already installed tool; no project dependency was added.
- `git diff --check` passes. Windows sandbox execution blocked asyncio's local
  socketpair; the HTTP suites run with reviewed elevated execution and synthetic
  fixtures. No provider calls or production runtime mutations were made.

Browser-only bundle tests require `RUN_PUBLIC_E2E=1`; they were not run locally.

## Release and remaining verification

This branch is a review candidate, not an accepted or deployed release. Before
promotion, run the repository CI and exact candidate acceptance/rollback gates.
After deployment, verify the complete live Cloudflare robots response contains
these exact exceptions, all sitemap documents remain 200/indexable with the
right canonical, and the JSON endpoints have `noindex`. Repeat with normal and
Googlebot user agents; `/privacy` must return one relative 308 to the canonical
HTTPS page for GET/HEAD. Keep private/unknown paths at their intended status.

In Search Console, inspect the rendered home/privacy pages and the actual
affected URLs, then request validation only for confirmed repaired public URLs.
An old/unknown/private 404 can be expected. Automated tests and a successful HTTP
response do not prove indexing or replace human candidate acceptance.

Google references:
[JavaScript rendering and blocked resources](https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics),
[robots.txt longest-match and end-anchor rules](https://developers.google.com/crawling/docs/robots-txt/robots-txt-spec).
