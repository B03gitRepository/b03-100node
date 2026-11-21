# Component work-order regression

The accepted parent and four component configurations ran on the same pinned simulator, official input bytes, write scope, seed and measured window. Each row is an actual work-order run. The model rows used frozen parameters.

| Configuration | Active RC choices | U (s) | W (weighted s) | Device activations | Verified device queries | RC selection / solver / charged total (s) |
|---|---|---:|---:|---:|---:|---:|
| Parent · fixed I | I × 1 | 4.663 | 14.326 | 3 | 110 | 0.0000 / 0.3068 / 0.3068 |
| Default · fixed I | I × 1 | 4.663 | 14.326 | 3 | 110 | 0.0000 / 0.3009 / 0.3009 |
| Replacement rule · fixed I | I × 1 | 4.690 | 14.398 | 3 | 110 | 0.0000 / 0.2540 / 0.4061 |
| Parent · frozen model | D × 1 | 6.763 | 20.626 | 3 | 110 | 0.8030 / 0.2079 / 1.0110 |
| Default · frozen model | D × 1 | 6.763 | 20.626 | 3 | 110 | 0.8005 / 0.2117 / 1.0122 |
| Replacement model adapter | D × 1 | 6.763 | 20.626 | 3 | 110 | 0.7983 / 0.2093 / 1.0076 |

The original and default routes had identical initial proposals and identical lists of activated paths, rates and epochs. The two frozen adapters used the same weight digests and selected the same first action from the same input and token probabilities. U and W include the packet clock. Each row records its own measured decision time. Required external component checks are charged before dispatch. Trace export time is recorded separately.

## Measured interface examples

These fields come from the replacement runs above. The README commands reproduce the complete local input and output for the partners holding the original scene.

| Component | Actual input (selected fields) | Actual output (selected fields) |
|---|---|---|
| Model | Schema `b03.packet.rc-choice.v1`, legal tools `A B C D E F G H I J K L` | Tool `D`, policy version `372` |
| Rule | Tool `I`, budget `medium`, a snapshot and its work-order authority | 3 service entries, solver audit `residual-first bounded pair search` |
| Knowledge | ID `K045`, edition `1.0.0` | Packet clock and work-order readback, 3 source references and an applicability field |

The query for K045 in edition 1.1.0 adds the separation of RC compute time, network transit, the score boundary and the drain ledger. It runs without loading model weights.

## Reproduce

Follow [the setup and six configurations](../README.md). For an independent source-only contract check, run `python -m unittest discover -s tests`. Preserve the per-run `CONDITIONS.json`, `RUN_FILES.json` and verifier report to identify the exact input and implementation.
