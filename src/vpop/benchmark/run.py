"""
Run the benchmark cases against one or more model configurations and report the results.

Each run builds the fixture database in a temporary directory, asks every selected case in a
fresh conversation per (configuration, repeat), grades the answers, writes one JSON line per
answer to a results file, and prints a summary.
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
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from vpop.assistant.claude_harness import ClaudeConversation
from vpop.assistant.conversation import Conversation
from vpop.assistant.errors import AuthError, describe_error
from vpop.assistant.tools import MessageTools
from vpop.benchmark import fixture
from vpop.benchmark.cases import CASES, TAGS, Case
from vpop.benchmark.grading import grade
from vpop.config import ClaudeConfig, OllamaConfig
from vpop.paths import bench_results_dir

REPO_ROOT = Path(__file__).resolve().parents[3]

Provider = Literal["ollama", "claude"]


@dataclass(frozen=True)
class BenchConfig:
    """
    One model configuration to benchmark. `option` is the Ollama `think` setting or the
    Claude `effort`, depending on the provider.
    """

    provider: Provider
    model: str
    option: str

    @property
    def label(self) -> str:
        """A short name, e.g. `qwen3:8b think=off` or `claude-sonnet-5-5 effort=low`."""
        knob = "think" if self.provider == "ollama" else "effort"
        return f"{self.model} {knob}={self.option}"

    def settings(self) -> OllamaConfig | ClaudeConfig:
        """The config section this configuration stands for."""
        if self.provider == "ollama":
            return OllamaConfig(model=self.model, think=self.option)  # type: ignore[arg-type]
        return ClaudeConfig(model=self.model, effort=self.option)  # type: ignore[arg-type]


def new_conversation(
    config: BenchConfig, tools: MessageTools, client: Any = None
) -> Conversation:
    """A fresh conversation for one case, told the fixture's fixed date."""
    settings = config.settings()
    if isinstance(settings, OllamaConfig):
        # pylint: disable=import-outside-toplevel
        from vpop.assistant.ollama_harness import OllamaConversation

        return OllamaConversation(tools, settings, client=client, today=fixture.TODAY)
    return ClaudeConversation(tools, settings, client=client, today=fixture.TODAY)


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
    case: Case, config: BenchConfig, tools: MessageTools, client: Any = None
) -> dict[str, Any]:
    """
    Ask one case in a fresh conversation and return its graded record. A backend failure
    (see `describe_error`) is recorded as the case's error; rejected credentials stop the run.
    """
    conversation = new_conversation(config, tools, client)
    answers: list[str] = []
    error = None
    start = time.monotonic()
    try:
        for question in (case.question, *case.followups):
            answers.append(conversation.ask(question))
    except AuthError:
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if describe_error(exc) is None:
            raise
        error = f"{type(exc).__name__}: {exc}"
    seconds = time.monotonic() - start
    trace = conversation.trace
    result = grade(case, answers[-1] if answers else "", trace)
    cost = None
    if isinstance(conversation, ClaudeConversation) and not conversation.unpriced:
        cost = round(conversation.cost, 6)
    return {
        "case": case.id,
        "tags": list(case.tags),
        "config": config.label,
        "provider": config.provider,
        "settings": asdict(config.settings()),
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
        "cost": cost,
    }


def select_cases(case_ids: Sequence[str], tags: Sequence[str]) -> list[Case]:
    """Cases matching any given id or tag; all cases when neither is given."""
    unknown = set(case_ids) - {case.id for case in CASES}
    if unknown:
        raise SystemExit(f"unknown case id(s): {', '.join(sorted(unknown))}")
    unknown_tags = set(tags) - set(TAGS)
    if unknown_tags:
        raise SystemExit(
            f"unknown tag(s): {', '.join(sorted(unknown_tags))} "
            f"(known: {', '.join(TAGS)})"
        )
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
            "median s | tokens/case | cost"
        ),
    ]
    for config, rs in by_config.items():
        calls = [len(r["tool_calls"]) for r in rs]
        errors = sum(c["is_error"] for r in rs for c in r["tool_calls"])
        costs = [r.get("cost") for r in rs]
        priced = [c for c in costs if c is not None]
        cost = f"${sum(priced):.2f}" if len(priced) == len(costs) else "-"
        lines.append(
            f"{config} | {sum(r['passed'] for r in rs)}/{len(rs)} | "
            f"{statistics.mean(r['score'] for r in rs):.2f} | "
            f"{statistics.mean(calls):.1f} | {errors}/{sum(calls)} | "
            f"{sum(r['hit_round_limit'] for r in rs)} | "
            f"{statistics.median(r['seconds'] for r in rs):.1f} | "
            f"{statistics.mean(r['prompt_tokens'] + r['output_tokens'] for r in rs):.0f} | "
            f"{cost}"
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


def ollama_client(models: Sequence[str]) -> Any:
    """An Ollama client; exits if the server is unreachable, warns about unpulled models."""
    import ollama  # pylint: disable=import-outside-toplevel

    client = ollama.Client()
    try:
        installed = {m.model for m in client.list().models}
    except (ConnectionError, OSError) as exc:
        raise SystemExit(f"can't reach Ollama: {exc}") from exc
    missing = [
        m for m in models if m not in installed and f"{m}:latest" not in installed
    ]
    if missing:
        print(f"warning: not pulled in Ollama: {', '.join(missing)}", file=sys.stderr)
    return client


def claude_client() -> Any:
    """An Anthropic client, exiting with what to do if there are no credentials."""
    # pylint: disable=import-outside-toplevel
    from vpop.assistant.claude_harness import make_client

    try:
        return make_client()[0]
    except AuthError as exc:
        raise SystemExit(f"error: {exc}") from exc


def run(  # pylint: disable=too-many-locals
    configs: Sequence[BenchConfig],
    *,
    case_ids: Sequence[str] = (),
    tags: Sequence[str] = (),
    repeat: int = 1,
    out_dir: Path | None = None,
) -> Path:
    """Run the benchmark, write results, print a summary, and return the results file."""
    cases = select_cases(case_ids, tags)
    providers = {config.provider for config in configs}
    clients: dict[str, Any] = {}
    if "ollama" in providers:
        clients["ollama"] = ollama_client(
            [c.model for c in configs if c.provider == "ollama"]
        )
    if "claude" in providers:
        clients["claude"] = claude_client()

    out_dir = out_dir or bench_results_dir()
    out_path = out_dir / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}.jsonl"
    stamp = {"git_sha": git_sha(), "today": fixture.TODAY}
    total = len(configs) * len(cases) * repeat
    records: list[dict[str, Any]] = []
    with ExitStack() as stack:
        db_file = stack.enter_context(fixture_db())
        stack.enter_context(quiet_tool_calls())
        tools = MessageTools(db_file)
        out = None
        for config in configs:
            for rep in range(repeat):
                for case in cases:
                    record = run_case(case, config, tools, clients[config.provider])
                    record |= stamp | {"repeat": rep}
                    records.append(record)
                    if out is None:
                        # Opened on the first result, so a run that fails at the start
                        # leaves no empty file behind.
                        out_dir.mkdir(parents=True, exist_ok=True)
                        out = stack.enter_context(out_path.open("w"))
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
