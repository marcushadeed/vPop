# vPop

**V**alue **P**roductivity **O**ver **P**rivacy.

Instead of asking you to switch to a new productivity tool, this one sits on top of the information channels you already use. You don't need to change anything.

## Overview

Most productivity tools require you to move your life into them. This one goes the other direction: it pulls from the places your information already lives — messages, notes, calendars, email — normalizes it into a local database, and puts an LLM on top of it.

That gives you two things: the ability to ask questions about your own information in plain language, and a daily briefing assembled from everything the system knows about your day.

## Features

Pre-release. What works today is marked ✅; everything else is planned (see the roadmap).

- ✅ **Text messages** — SMS, MMS and RCS from Android, via SMS Backup & Restore backups on Google Drive, imported into a local SQLite database.
- ✅ **Natural-language questions** — `vpop ask` puts an LLM (a local Ollama model, or Claude) on top of the database through read-only query tools, so it pulls in only the rows it needs.
- ✅ **Benchmark** — fixed questions over a synthetic database with known answers, for comparing models and settings.
- Other sources — notes, email, Google Calendar, reminders, photos of physical journals.
- Daily report — a morning rundown of your schedule and to-dos (text someone back, charge your AirPods), delivered to your phone, with audio output and a single-button trigger for the car.
- Sync dashboard — when each source was last pulled, with sync triggers.

## Architecture

```mermaid
graph LR
    backup["SMS Backup & Restore"] --> drive["Google Drive"]
    drive -->|vpop sync| raw["raw XML backups"]
    raw --> sqlite["SQLite"]
    sqlite --> tools["read-only query tools"]
    tools --> llm["Ollama or Claude"]
    llm -->|vpop ask| you["you"]
```

The downloaded backups are the source of truth; the database is derived from them and can be rebuilt at any time (`vpop sync --rebuild`). The model never sees the database directly: it calls tools that search messages (full-text and substring), read a conversation around a message, and run read-only SQL for counts.

Code layout (`src/vpop/`):

- `cli.py` — the `vpop` command
- `config.py`, `paths.py` — settings and every file location
- `db.py` — schema, migrations and connections
- `sources/` — one package per data source (`android_messages/`: fetch, parse, store, sync), plus the Google Drive client
- `assistant/` — the query tools, the shared tool loop, the Ollama and Claude harnesses, `vpop auth`
- `benchmark/` — the synthetic fixture, cases, grader and runner

## Roadmap

### v1.0 — Ingestion and querying (in progress)
- ✅ Text messages as a source; Obsidian notes next
- ✅ SQLite storage
- Sync-status dashboard with sync triggers
- ✅ LLM tool harness for querying the database

### v2.0 — Daily reports
- Morning job runs on laptop, uploads to local server
- Report generated server-side and sent to phone

### v2.1 — More sources, audio out
- WhatsApp and reminders
- Out-loud podcast-style report

### v2.2 — Remaining sources
- Samsung Notes, journal photos, Google Calendar

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/marcushadeed/vPop.git && cd vPop
uv tool install .        # puts `vpop` on your PATH (or: uv sync, then `uv run vpop`)
vpop config init         # writes ~/.config/vpop/config.toml with every setting documented
```

### Text message backups

1. On your Android phone, install **SMS Backup & Restore** and schedule a daily backup of messages to **Google Drive**. vPop reads XML backups; media is optional.
2. Open the backup folder in Drive and copy the id from its URL (`https://drive.google.com/drive/folders/<id>`) into the config:
   ```toml
   [android_messages]
   drive_folder_id = "<id>"
   ```
3. Create a Google OAuth client so vPop can read that folder: in [Google Cloud Console](https://console.cloud.google.com/), create a project, enable the **Google Drive API**, then *APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop app*. Download the JSON and save it as `~/.config/vpop/oauth-client-credentials.json`.
4. Run `vpop sync`. The first run opens a browser for consent (read-only Drive access); the token is cached beside the config, readable only by you.

### A model

- **Local (default, nothing leaves your machine):** install [Ollama](https://ollama.com/), run `ollama serve`, and pull a model with tool support: `ollama pull qwen3:8b`.
- **Claude:** run `vpop auth login` to save an API key (or log in with the Anthropic CLI), and answer yes when it offers to switch `local_model` off. Message rows the model asks for are then sent to Anthropic.

## Usage

```sh
vpop sync                  # download new backups and import them
vpop sync --offline        # import what's already downloaded, no network
vpop sync --rebuild        # recreate the database from every downloaded backup

vpop ask "Where did Jordan end up moving?"
vpop ask                   # a conversation; Ctrl-C cancels a question, Ctrl-D exits

vpop config show           # the settings in effect
vpop auth status           # which Anthropic credentials are used, and whether they work

vpop bench                                         # benchmark the configured Ollama model
vpop bench --model qwen3:8b --think on --think off
vpop bench --provider claude --effort low --effort medium   # costs API credits
vpop bench --compare FILE1.jsonl FILE2.jsonl
```

`-v` shows debug output and `-q` hides progress. Files live in the XDG directories: settings and secrets in `~/.config/vpop/`, backups, the database and benchmark results in `~/.local/share/vpop/`.

## Development

```sh
uv sync                      # installs vpop in editable mode plus the dev tools
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
uv run mypy                  # strict, for src/
uv run pylint src && uv run pylint --rcfile tests/pylintrc tests
```

CI runs all of these on every push. Tests never touch your real config or data: each one gets its own temporary home and XDG directories.

Changing the database schema: add a function to `db.MIGRATIONS` and bump `db.SCHEMA_VERSION`; never edit an existing migration. If a change alters how message ids (hashes) are computed, existing databases can't be migrated, so make `migrate` refuse them and point the user at `vpop sync --rebuild`, which works because the raw backups are kept.

## Privacy

This project deliberately trades privacy for convenience — that tradeoff is the premise, not an oversight. Your messages, notes, and calendar data are aggregated into a single local store and passed to an LLM. Consider carefully where that model runs and what your threat model is before pointing this at your real accounts.

## License

This project is licensed under the Apache License 2.0 — see the [LICENSE](LICENSE) file for details.