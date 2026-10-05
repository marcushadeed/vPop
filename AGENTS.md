# AGENTS.md

## Git policy

- Never push to remote. Pushing is only for the user to do.
- Commits made by an agent must only happen on a branch, never directly on `master`.
- Never merge a branch into `master`. Merging is only for the user to do.

## Working on the code

- Run `uv run pytest`, `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run mypy` and both pylint commands from the README before calling a change done.
- The raw backups under the data directory are the source of truth; the database is rebuildable. Schema changes go through `db.MIGRATIONS` (append-only) and a `SCHEMA_VERSION` bump.
- The query tools are read-only. Don't give the model a tool with side effects without asking first: message text is untrusted input written by other people.
