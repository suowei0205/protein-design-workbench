# Upstream review, 2026-10-04

These are primary-source references actually inspected during this publication review. Reuse is architectural unless explicitly stated as an installed dependency or bundled asset.

| Reference | Applicable idea | Decision and license boundary |
|---|---|---|
| [MultiQC](https://github.com/MultiQC/MultiQC) | Interactive aggregate report plus downloadable machine-readable data | Retain JSON/Markdown/HTML together and independent run history. GPL-3.0; no source copied. |
| [Huey SQLite storage](https://github.com/coleifer/huey/blob/master/huey/storage.py) | Transactional dequeue, priority plus stable ID ordering, explicit rollback | Review local FIFO ordering and supervisor lock lifetime; no new queue framework or source copied. MIT upstream. |
| [NBClient](https://github.com/jupyter/nbclient) / [execution docs](https://nbclient.readthedocs.io/en/latest/client.html) | Explicit kernel binding, timeout configuration, error propagation | Existing dependency; stop on notebook error and preserve executed output. BSD-3-Clause. |
| [3Dmol.js](https://github.com/3dmol/3Dmol.js) | Client-side structure viewers | Existing bundled library, limited to two active viewers with offline resources; BSD-3-Clause notice preserved. |
| [GitHub Actions security](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions) | Least privilege and immutable action references | Read-only CI permissions, exact action commit references, fork PRs use pull_request rather than pull_request_target. |

These projects are not guarantees of this implementation's correctness. GPU compatibility, force-field choices, binding, and experimental conclusions require separate evidence.
