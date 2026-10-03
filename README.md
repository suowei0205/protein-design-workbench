# Protein Design Workbench

[![CI](https://github.com/suowei0205/protein-design-workbench/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/suowei0205/protein-design-workbench/actions/workflows/ci.yml)

Local execution, professional monitoring, and permanent run archives for protein design and simulation.

**预览版 / Engineering preview.** The executor targets a single-user Linux workstation. The public repository contains general code and synthetic tests, with no private targets, research notebooks, model weights, credentials, or research results. Real GPU and GROMACS validation remains a separate deployment acceptance step.

## Start locally

Python 3.10+ is required. Use an isolated workbench environment; bind existing scientific environments through the UI.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
pwb --data-root "$HOME/ProteinWorkbenchData" serve
```

Open `http://127.0.0.1:8970` on the workstation's remote desktop. The queue starts paused. Bind an absolute Python interpreter, import a trusted notebook or script, inspect configuration, enqueue it, and explicitly resume scheduling. The server runs independently of the browser; keep it in a persistent terminal/session or use the provided background launcher.

```bash
PWB_DATA_ROOT="$HOME/ProteinWorkbenchData" scripts/start_remote.sh
```

Notebook execution needs `ipykernel` in the selected scientific environment. BindCraft and GROMACS must be separately installed; scientific configuration fields are deliberately empty until explicitly supplied. `scripts/update_local.sh INBOX REPORTS` verifies feedback ZIPs and rebuilds a local offline archive.

## What is recorded

- Every new run gets a new card; cloning copies inputs and parameters without copying results.
- Resuming preserves the run and adds an execution attempt. Queue order is adjustable before claiming a task.
- Committed work, cache reuse, actual denominators, returned models, collector activity, and computation progress stay distinct.
- Identity checks bind frozen source, configuration, environment, candidates, and artifacts. Failure preserves partial evidence.
- Supported adapters provide checkpoint-boundary stop and resume; generic scripts do not pretend to support checkpoint recovery.
- Feedback snapshots preserve history. Local imports rebuild active report HTML/JS from data and retain the original ZIP.
- Collapsing the sidebar releases its entire column and centers the content; the preference persists when browser storage is available.

## Workflows and deployment limits

Generic notebooks/scripts, BindCraft, GROMACS MD, constant-velocity pulling, and constant-force pulling have adapters. BindCraft retains its native acceptance rules. GROMACS requires explicit preparation and simulation choices; special chemical systems require separate preparation. CPU command fixtures do **not** prove scientific-engine compatibility.

The optional SR56 adapter and instrumentation code are present, but the six private research notebooks, their helper/report kit, and target data are excluded. Import a separately prepared trusted kit to use that adapter. Legacy feedback requires its separate trusted adapter and is not included in this public distribution.

The workbench executes trusted local code with the selected user's privileges; it is **not an execution sandbox** or a multi-user web service. Do not expose its port through a reverse proxy. See [SECURITY.md](SECURITY.md).

## Development and project structure

```text
src/pwb/           executor, API, adapters, feedback, packaged UI and templates
tests/             CPU lifecycle/security tests and JS/browser checks
scripts/           local launch, archive update, publication checks
docs/              architecture, references, audit and deployment smoke checklist
.github/           CI, dependency updates and issue/PR templates
```

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/ui_selfcheck.js
node tests/ui_forms_selfcheck.js
node tests/ui_offline_selfcheck.js
python scripts/check_publication.py
```

Browser checks use a separately installed Playwright runtime, with `PWB_ASSETS_DIR` optionally selecting the UI directory. See the pinned CI workflow for a reproducible invocation. Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Review evidence and outstanding deployment checks: [docs/AUDIT.md](docs/AUDIT.md). Upstream references: [docs/REFERENCES.md](docs/REFERENCES.md).

Original workbench code is MIT licensed. Bundled 3Dmol and fonts retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). No scientific engine or model weights are redistributed.
