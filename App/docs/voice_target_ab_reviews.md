# Target A/B listening review receipts

`voice_target_ab_review_ops.py` records the exact human selection for the
existing Vidya and Chenxing natural-versus-compacted comparison packages.

The receipt is intentionally narrower than a training or publication approval:

- all four slots select the compacted candidate after human listening;
- Vidya B records that natural and compacted bytes are identical;
- every displayed text file, natural WAV, compacted WAV, review page, package
  manifest, and comparison-batch manifest is hash-pinned and revalidated;
- A/B candidate, transcript, input audio, selected audio, and source ranges must
  remain disjoint;
- training, cloning, rights, provider enrollment, publication, and public
  rollout remain false.

Dry-run first, using exact byte hashes for the immutable inputs:

```powershell
$vidyaPackage = 'voice-recording-dialogue-comparison-a62af942ac96e7bc059e'
$vidyaSha = 'd96b962db1596ccbf44d18d682975842eca7dca098685deee8d88f7bd5b30dcc'
$chenxingPackage = 'voice-recording-dialogue-comparison-9eaa4601e56ff9917069'
$chenxingSha = 'ae2983cdd236c32eedd89285fae0e16609f5dffaf3ab3a9a180dec0034054305'
uv run --no-project --python 3.12 python `
  App/scripts/voice_target_ab_review_ops.py record `
  --voice-root C:\Users\25685\Desktop\Myprojects\Project_Snow\Data\Voice `
  --reviewed-at 2026-09-02T13:25:58.6728955+08:00 `
  --expect-batch-sha256 00791972cb016993eef95b4b7a81b54a0512375a5d498d1b72e846a36b1c9769 `
  --expect-package-sha256 "$vidyaPackage=$vidyaSha" `
  --expect-package-sha256 "$chenxingPackage=$chenxingSha"
```

Append `--confirm-four-compacted-selections --execute` only after the exact
four-slot user statement has been received. Repeating the same command is
idempotent. Validation reconstructs the receipt from the still-pinned source
assets instead of trusting the stored summary.
