# Publication audit — 2026-10-04

The reviewed scope is the public general-purpose workbench. This is an engineering preview, not a claim of zero defects or industrial certification. Research notebooks, structures, target sequences, weights, logs and runtime evidence from real research were excluded by an explicit publication allowlist.

## Findings resolved before publication

| Finding | Correction | Regression evidence |
|---|---|---|
| Reused PGID could register an unrelated process as owned | Once the leader is gone, use only identities recorded while ownership was proven | Mock foreign group receives no signal; known descendants still block overlap |
| Supervisor could release its lock before its thread stopped | Keep the lock until loop exit; check halt before launching | Blocking tick, shutdown race, takeover and launch guards |
| Exception named SafeStop could clear a real error | Require a matching stop request and run/attempt-bound committed marker | Missing, forged, stale and unrelated errors remain failed |
| Stage denominator could inject HTML | Validate plan counts at event/persistence and historical reduction; escape displayed values | Malicious PLAN plus ERROR rejected; corrupt direct-render count shown as text |
| Internal identity fields could reach public create/import APIs | Validate endpoint-specific public fields and boolean controls | Direct checks and actual HTTP 400 responses |
| Large ZIP members could allocate their full size | Stream ordinary member hashes; bound metadata JSON before reading | Read spy forbids ordinary-member whole reads; oversized metadata rejected |
| Consistent imported HTML/JS could still be malicious | Regenerate active modern report code; retain original ZIP evidence | Self-consistent malicious package verifies but active HTML/JS is replaced |
| Inline legacy report could share the mutation API origin | Main reports forbid inline script; legacy render gets opaque-origin sandbox | CSP checks; public distribution excludes the legacy helper |
| `src` migration could break an uninstalled scientific interpreter | Set its PYTHONPATH to the package parent | Clean independent interpreter completes a real CPU script |
| Optional adapter ignored the helper from a separately imported kit | Bind bootstrap to the snapshotted input helper; keep observation changes outside compute AST | Original/already-bound 36-cell layouts, helper execution and frozen-member tamper checks |
| Unused imports and inconsistent public package version | Remove 17 unused imports and use 0.1.0 consistently | Ruff F/E9 and installed package checks |

## Local evidence actually obtained

- Public Python suite: **93 tests, 92 passed, 1 skipped**, 49.006 s. The skip is Linux orphan adoption on the local macOS host. Real NBClient kernel execution/error preservation, loopback HTTP security tests and background CPU scripts ran successfully. Scientific commands in adapter tests are fake local fixtures.
- Three JavaScript suites passed: functions/evidence/security; DOM-mock forms and two-viewer lifecycle; offline embedded structures and pagination. These are not substitutes for a browser.
- Real Chromium **151.0.7922.34** ran the source UI from `file://`, with network disabled: **72 checks passed, zero failures**, 22 screenshots. Viewports 1440/1024/768/390 plus 1920 collapse were checked. Main center differed by 0 px after collapse. Keyboard/focus/storage failure, a natural 30 s production timer and zero HTTP requests were observed. Viewer methods in the persistence check were explicitly stubbed; real WebGL was not tested.
- Ruff F/E9 passed. Bandit found **0 high/medium** issues and **15 low-severity** notices, manually reviewed: subprocess imports and `shell=False` calls to trusted selected tools, plus local system tool lookup. No broad suppression was added; invoking local code is the documented trust model.
- Dependency audit: **70 installed distributions, no known vulnerabilities returned** by pip-audit at the audit time. Unknown-to-PyPI editable workbench code is covered by source review rather than a vulnerability-database claim. Package dependency consistency passed.
- Source distribution and wheel built. The package-data boundary includes UI/fonts/templates and excludes research data and weights. A tracked-file privacy scan rejects common credential shapes, personal machine paths and runtime/research artifacts; this is paired with manual allowlist review.

## Remaining limits and maintenance work

Linux process adoption and Python 3.10 execution are exercised by the repository CI; their eventual workflow result is separate evidence from this local audit. Real BindCraft/RFD3/RF3 GPU runs, GROMACS MD/pulling, peak memory, real WebGL, screen-reader evaluation and visual regression against a baseline are **NOT RUN** here. Use [REMOTE_SMOKE.md](REMOTE_SMOKE.md) on the workstation before accepting a scientific deployment.

Two inherited maintainability issues remain visible: the scientific validation module has long procedures (notably `validate`, 182 lines, and `_execute_md`, 115 lines), and several core/JS functions use dense formatting. Behavioral fixes were kept bounded instead of bundling a broad scientific-logic refactor into this publication. Those are nonblocking preview debt, not a claim of full style compliance. Future refactoring must preserve the existing scientific configuration/receipt tests and receive separate review.

Controller acceptance covered actual diffs, public scope, regression results, reported process/evidence semantics, offline behavior and asset provenance. A Codex child provided independent review and the bounded lifecycle fixes. A separate Broker route (`opencode_go / deepseek-flash / deepseek-v4.1-flash / app_server`) reviewed only generic non-sensitive publication risks; its returned report was truncated and was treated as partial advice. It did not inspect or approve this repository. No Chat child was called. The controller model supplied by session metadata was `gpt-6.1-sol`, provider `openai`; backend metadata was not supplied. Final acceptance belongs to the controller, not to model votes or scanner exit codes.
