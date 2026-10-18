"""
Tests that the Claude Code plugin matches the code: the skill documents every table and
column a fresh database has (and nothing else), names only real tools, and the manifests
are valid.
"""

import json
import re
import tomllib
from contextlib import closing
from pathlib import Path

from vpop import db
from vpop.config import Config, WhatsAppConfig
from vpop.sources.registry import enabled_sources
from vpop.tools.server import build_toolbox
from vpop.tools.toolbox import tool_schema

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "vpop"


def tools() -> dict[str, set[str]]:
    """Every tool with every source enabled, mapped to its parameters."""
    config = Config(whatsapp=WhatsAppConfig(enabled=True))
    box = build_toolbox(enabled_sources(config), Path("unused.db"))
    return {
        name: set(tool_schema(func)["parameters"]["properties"])
        for name, func in box.functions.items()
    }


def documented_tables() -> dict[str, set[str]]:
    """
    The tables the skill documents (`## Table \\`name\\`` headings) and their columns (the
    names in backticks before the colon of each `- ` item under the heading).
    """
    tables: dict[str, set[str]] = {}
    current: set[str] | None = None
    for path in sorted(SKILL.glob("*.md")):
        for line in path.read_text().splitlines():
            if line.startswith("#"):
                heading = re.fullmatch(r"## Table `(\w+)`", line)
                current = tables.setdefault(heading[1], set()) if heading else None
            elif current is not None and line.startswith("- "):
                names, colon, _ = line[2:].partition(":")
                assert colon, line
                current |= set(re.findall(r"`(\w+)`", names))
    return tables


def test_skill_documents_exactly_the_schema(tmp_path: Path) -> None:
    with closing(db.connect(tmp_path / "vpop.db")) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE '%_fts%' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        schema = {
            name: {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}
            for name in names
        }
    assert documented_tables() == schema


def test_skill_frontmatter() -> None:
    text = (SKILL / "SKILL.md").read_text()
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md starts with --- frontmatter ---"
    fields = dict(line.split(": ", 1) for line in match[1].splitlines())
    assert set(fields) == {"name", "description"}
    assert fields["name"] == "vpop"
    assert 0 < len(fields["description"]) <= 1024
    assert ":" not in fields["description"]  # it isn't quoted, so YAML would split it


def test_skill_names_only_real_tools_and_parameters() -> None:
    text = (SKILL / "SKILL.md").read_text()
    known = tools()
    parameters = set().union(*known.values())
    mentioned = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text))
    assert mentioned <= set(known) | parameters
    assert set(known) <= mentioned  # the workflow covers every tool


def test_plugin_manifests() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert plugin["name"] == "vpop"
    assert plugin["version"] == project["version"]
    assert plugin["mcpServers"] == {"vpop": {"command": "vpop", "args": ["mcp"]}}
    assert [entry["name"] for entry in marketplace["plugins"]] == ["vpop"]
    assert marketplace["plugins"][0]["source"] == "./"
