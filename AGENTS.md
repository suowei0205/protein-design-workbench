# Repository conventions

Public code and synthetic fixtures only. Never add research inputs, private notebooks, engine weights, environment dumps, credentials, or run outputs. Keep all runtime data outside the repository.

Preserve workflow selection, budgets, seed meanings, and cache contracts. Fixes to process ownership, trust boundaries, and evidence integrity require focused regression tests. Report actual checks and NOT RUN boundaries; fake engines are not deployment acceptance.

Use `src/pwb` for application code and package resources, `tests` for synthetic checks, `docs` for durable design/review evidence, and `scripts` for executable development/deployment entry points. Do not copy implementation into notebooks. Require Python 3.10 compatibility.
