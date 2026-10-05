# AGENTS.md

## Git policy

- Never push to remote. Pushing is only for the user to do.
- Commits made by an agent must only happen on a branch, never directly on `master`.
- Never merge a branch into `master`. Merging is only for the user to do.

## Working on the code

- Run `uv run pytest`, `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run mypy` and both pylint commands from the README before calling a change done.
- Sources are synced or live. For a synced source (text messages), the raw backups under the data directory are the source of truth and the database is rebuildable; schema changes go through `db.MIGRATIONS` (append-only) and a `SCHEMA_VERSION` bump. A live source (Google Calendar) is read through its API at question time and stores nothing. Prefer live when the data has an API.
- A new source subclasses `Source` or `SyncedSource` (`sources/base.py`) and is added to `SOURCES` in `sources/registry.py`.
- The query tools are read-only, and live sources ask for read-only API scopes only. Don't give the model a tool with side effects without asking first: message text and calendar invites are untrusted input written by other people.
