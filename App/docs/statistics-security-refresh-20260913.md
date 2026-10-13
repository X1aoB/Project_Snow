# Embedding system package refresh for the statistics release

The first statistics main candidate `da5c01de20a7cd22a363410bcccbc42b9c06f1c4`
passed functional, browser and PostgreSQL checks, but its release job failed the
fresh embedding-image vulnerability gate. It reused
`ghcr.io/x1aob/project_snow-embedding@sha256:4da53f97333e9a4170950852cc376dac83b0d6dd52445bf90be7a657f8bc0614`.

The scan in [main run 34758551256](https://github.com/X1aoB/Project_Snow/actions/runs/34758551256/job/103728343033)
reported 12 HIGH/CRITICAL findings with available fixes across these installed
Debian packages. Multiple findings may refer to one package.

| Package | Installed in reused image | Fixed version reported by Trivy |
|---|---|---|
| gzip | 1.13-1 | 1.13-1+deb13u1 |
| libpcre2-8-0 | 10.46-1~deb13u1 | 10.46-1~deb13u2 |
| libsqlite3-0 | 3.46.1-7+deb13u1 | 3.46.1-7+deb13u2 |
| perl-base | 5.40.1-6 | 5.40.1-6+deb13u1 |

The Dockerfile now consumes a reviewed system-security revision before its
existing `apt-get update` / `apt-get upgrade` step. This forces a fresh system
layer and, because the Dockerfile changed, an embedding rebuild through the
existing risk classifier. Both builder and serving stages inherit that system
layer. The model ID, immutable model revision, Python dependencies, dimensions,
offline runtime and image scan policy are unchanged. There are no new ignores.

The version label is a cache-refresh input, not a claim that later builds or
older images remain free of vulnerabilities. A new immutable image must pass
the unchanged Trivy gate and full main release checks before it can enter a
candidate manifest. Failed release `da5c01d` was not staged or promoted.
