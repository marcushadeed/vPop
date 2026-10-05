"""
Run the benchmark cases against one or more model configurations and report the results.

Each run builds the fixture database in a temporary data directory, asks every selected case
in a fresh conversation per (model, think, repeat), grades the answers, writes one JSON line per
answer to `benchmarks/results/`, and prints a summary.
"""

import json
import logging
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import ollama

from vpop.assistant.ollama_harness import OllamaConversation
from vpop.assistant.tools import MessageTools
from vpop.benchmark import fixture
from vpop.benchmark.cases import CASES, Case
from vpop.benchmark.grading import grade
from vpop.config import OllamaConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"


def config_label(settings: OllamaConfig) -> str:
    """A short name for a model configuration, e.g. `qwen3:8b think=off`."""
    return f"{settings.model} think={settings.think}"


@contextmanager
def fixture_db() -> Iterator[Path]:
    """Build the fixture database in a temporary directory and yield its path."""
    with tempfile.TemporaryDirectory(prefix="vpop-bench-") as tmp:
        path = Path(tmp) / "fixture.db"
        fixture.build_fixture_db(path)
        yield path


@contextmanager
def quiet_tool_calls() -> Iterator[None]:
    """Hide the per-tool-call log lines a conversation prints, for the length of a run."""
    logger = logging.getLogger("vpop.assistant")
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous)


def git_sha() -> str:
    """The current commit, marked dirty if the tree has changes, or `unknown`."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{sha}-dirty" if dirty else sha


def run_case(
    case: Case,
    settings: OllamaConfig,
    tools: MessageTools,
    client: ollama.Client | None = None,
) -> dict[str, Any]:
    """Ask one case in a fresh conversation and return its graded record."""
    conversation = OllamaConversation(
        tools, settings, client=client, today=fixture.TODAY
    )
    answers: list[str] = []
    error = None
    start = time.monotonic()
    try:
        for question in (case.question, *case.followups):
            answers.append(conversation.ask(question))
    except (ollama.ResponseError, ConnectionError, OSError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    seconds = time.monotonic() - start
    trace = conversation.trace
    result = grade(case, answers[-1] if answers else "", trace)
    return {
        "case": case.id,
        "tags": list(case.tags),
        "config": config_label(settings),
        "settings": asdict(settings),
        "passed": result.passed and error is None,
        "score": result.score if error is None else 0.0,
        "checks": [asdict(check) for check in result.checks],
        "error": error,
        "answers": answers,
        "seconds": round(seconds, 2),
        "rounds": trace.rounds,
        "hit_round_limit": trace.hit_round_limit,
        "tool_calls": [
            {"name": c.name, "arguments": c.arguments, "is_error": c.is_error}
            for c in trace.tool_calls
        ],
        "prompt_tokens": trace.prompt_tokens,
        "output_tokens": trace.output_tokens,
    }


def select_cases(case_ids: Sequence[str], tags: Sequence[str]) -> list[Case]:
    """Cases matching any given id or tag; all cases when neither is given."""
    unknown = set(case_ids) - {case.id for case in CASES}
    if unknown:
        raise SystemExit(f"unknown case id(s): {', '.join(sorted(unknown))}")
    if not case_ids and not tags:
        return list(CASES)
    return [c for c in CASES if c.id in case_ids or set(c.tags) & set(tags)]


def print_progress(record: dict[str, Any], done: int, total: int) -> None:
    """One stderr line per finished case."""
    mark = "PASS" if record["passed"] else "FAIL"
    failed = [c["name"] for c in record["checks"] if not c["passed"]]
    reason = record["error"] or "; ".join(failed)
    suffix = f"  ({reason})" if reason and not record["passed"] else ""
    print(
        f"[{done}/{total}] {mark} {record['config']} {record['case']} "
        f"{record['seconds']}s{suffix}",
        file=sys.stderr,
    )


def summarize(records: Sequence[dict[str, Any]]) -> str:
    """Per-config totals, then a case × config matrix of pass counts."""
    by_config: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_config[record["config"]].append(record)

    lines = [
        "",
        (
            "config | pass | score | tool calls/case | tool errors | round limit | "
            "median s | tokens/case"
        ),
    ]
    for config, rs in by_config.items():
        calls = [len(r["tool_calls"]) for r in rs]
        errors = sum(c["is_error"] for r in rs for c in r["tool_calls"])
        lines.append(
            f"{config} | {sum(r['passed'] for r in rs)}/{len(rs)} | "
            f"{statistics.mean(r['score'] for r in rs):.2f} | "
            f"{statistics.mean(calls):.1f} | {errors}/{sum(calls)} | "
            f"{sum(r['hit_round_limit'] for r in rs)} | "
            f"{statistics.median(r['seconds'] for r in rs):.1f} | "
            f"{statistics.mean(r['prompt_tokens'] + r['output_tokens'] for r in rs):.0f}"
        )

    lines += ["", "pass rate by tag:"]
    tags = sorted({t for r in records for t in r["tags"]})
    for config, rs in by_config.items():
        cells = []
        for tag in tags:
            tagged = [r for r in rs if tag in r["tags"]]
            cells.append(f"{tag} {sum(r['passed'] for r in tagged)}/{len(tagged)}")
        lines.append(f"  {config}: " + ", ".join(cells))

    lines += ["", matrix(by_config)]
    return "\n".join(lines)


def matrix(columns: Mapping[str, Sequence[dict[str, Any]]]) -> str:
    """A case × column table of passes/attempts, with `*` on rows where columns disagree."""
    names = list(columns)
    tallies: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for name, records in columns.items():
        for record in records:
            tallies[record["case"]][name].append(record["passed"])

    width = max((len(case) for case in tallies), default=4) + 2
    lines = ["case".ljust(width) + " | ".join(f"[{i}]" for i in range(len(names)))]
    lines += [f"  [{i}] {name}" for i, name in enumerate(names)]
    for case, cells in tallies.items():
        shown = [
            f"{sum(cells[n])}/{len(cells[n])}" if n in cells else "-" for n in names
        ]
        fractions = {sum(cells[n]) / len(cells[n]) for n in names if n in cells}
        flag = " *" if len(fractions) > 1 else ""
        lines.append(case.ljust(width) + " | ".join(s.ljust(3) for s in shown) + flag)
    return "\n".join(lines)


def check_ollama(client: ollama.Client, models: Sequence[str]) -> None:
    """Exit if Ollama is unreachable, and warn about models that aren't pulled."""
    try:
        installed = {m.model for m in client.list().models}
    except (ConnectionError, OSError) as exc:
        raise SystemExit(f"can't reach Ollama: {exc}") from exc
    missing = [
        m for m in models if m not in installed and f"{m}:latest" not in installed
    ]
    if missing:
        print(f"warning: not pulled in Ollama: {', '.join(missing)}", file=sys.stderr)


def run(
    models: Sequence[str],
    thinks: Sequence[str],
    *,
    case_ids: Sequence[str] = (),
    tags: Sequence[str] = (),
    repeat: int = 1,
) -> Path:
    """Run the benchmark, write results, print a summary, and return the results file."""
    cases = select_cases(case_ids, tags)
    configs = [
        OllamaConfig(model=model, think=think)  # type: ignore[arg-type]
        for model in models
        for think in (thinks or ["default"])
    ]
    client = ollama.Client()
    check_ollama(client, models)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}.jsonl"
    stamp = {"git_sha": git_sha(), "today": fixture.TODAY}
    total = len(configs) * len(cases) * repeat
    records: list[dict[str, Any]] = []
    with fixture_db() as db_file, quiet_tool_calls(), out_path.open("w") as out:
        tools = MessageTools(db_file)
        for settings in configs:
            for rep in range(repeat):
                for case in cases:
                    record = run_case(case, settings, tools, client)
                    record |= stamp | {"repeat": rep}
                    records.append(record)
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out.flush()
                    print_progress(record, len(records), total)

    print(summarize(records))
    print(f"\nresults: {out_path}")
    return out_path


def load(path: Path) -> list[dict[str, Any]]:
    """Read a results file."""
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def compare(paths: Sequence[Path]) -> None:
    """Print a case × (file, config) matrix across result files; `*` marks disagreements."""
    columns: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in paths:
        for record in load(path):
            columns[f"{path.name} {record['config']}"].append(record)
    print(matrix(columns))
