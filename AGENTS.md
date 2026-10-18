# AGENTS.md

## Git policy

- Never push to remote. Pushing is only for the user to do.
- Commits made by an agent must only happen on a branch, never directly on `master`.
- Never merge a branch into `master`. Merging is only for the user to do.

## Working on the code

- Run `uv run pytest`, `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run mypy`, both pylint commands from the README and `claude plugin validate --strict .` before calling a change done.
- Where the data comes from: use Claude's connector when one exists (Google Calendar, Gmail), and don't duplicate it in vPop; otherwise read the data live (an API or local files) and store nothing; otherwise sync it. For a synced source (text messages, WhatsApp), the raw backups under the data directory are the source of truth and the database is rebuildable; schema changes go through `db.MIGRATIONS` (append-only) and a `SCHEMA_VERSION` bump.
- A new source subclasses `Source` or `SyncedSource` (`sources/base.py`), describes its data as `Store`s (tools are by kind, so a chat app gives a `MessageStore`), and is added to `SOURCES` in `sources/registry.py`.
- The MCP server exposes only read-only tools, and sources ask for read-only API scopes only. Don't give Claude a tool with side effects without asking first: message text is untrusted input written by other people.
- Tool docstrings are the per-tool docs Claude reads. Table and column docs live in `skills/vpop/`, and a test checks them against the schema, so a schema change updates them too.
