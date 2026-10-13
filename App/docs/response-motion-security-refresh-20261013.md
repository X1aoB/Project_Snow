# Embedding security refresh for the response/motion candidate

Main candidate `e9c0fd4d43b0350df95d2cad00628f023facb55d` passed application,
browser and PostgreSQL checks, but its release job rejected the reused embedding
image `sha256:465a1dbfa32e9c28f1e12cac9b96318ac804923428ff3d25a23eb463c0546471`.
It was not staged or promoted and has no successful release proof.

The [fresh release scan](https://github.com/X1aoB/Project_Snow/actions/runs/38355045381/job/115159237909)
reported four HIGH package findings with available fixes:

| Package | Advisory | Fixed Debian version |
| --- | --- | --- |
| libpcre2-8-0 | CVE-2026-103111 | `10.46-1~deb13u3` |
| libssl3t64 | CVE-2026-84782 | `3.5.7-1~deb13u3` |
| openssl | CVE-2026-84782 | `3.5.7-1~deb13u3` |
| openssl-provider-legacy | CVE-2026-84782 | `3.5.7-1~deb13u3` |

Advancing `SYSTEM_SECURITY_REVISION` to `2026-10-13` invalidates the existing
apt upgrade layer in both builder and serving stages. Model bytes, dimensions,
Python requirements, offline serving and vulnerability thresholds are unchanged.
No vulnerability exception is added. The existing change classifier requires
an embedding rebuild and the PR workflow tests actual 512-dimensional inference.

The [nightly rebuild](https://github.com/X1aoB/Project_Snow/actions/runs/38329679693/job/115076329587)
already installed these patched versions and passed its scan, but that workflow
does not publish its rebuilt image. Its success cannot validate the older digest
reused by the failed release. The new published digest still needs full main CI
and a signed candidate proof.

Ordinary application staging rejects a changed shared embedding digest. A new
image/proof alone does not authorize changing shared service pins or rewriting
historical release records. Independent shared-service maintenance, capacity and
reconstructible rollback gates remain prerequisites to staging this candidate.
