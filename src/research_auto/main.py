from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from langchain.chat_models import init_chat_model
from pydantic import AliasChoices, BaseModel, Field

from config import get_settings


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUTORESEARCH_ROOT = PROJECT_ROOT / ".autoresearch"
RUNS_ROOT = AUTORESEARCH_ROOT / "runs"
STATE_PATH = AUTORESEARCH_ROOT / "state.json"
OPENAI_PROVIDER = "openai"
MUTABLE_FILES = (Path("src/agents/builder.py"),)
DEFAULT_VERIFY_COMMAND = ["uv", "run", "python", "-m", "compileall", "src"]
DEFAULT_FAILURE_SAMPLE_LIMIT = 8

ANALYZER_SYSTEM_PROMPT = """
You are optimizing a deep-agent SQLite QA system using eval evidence.

Focus only on improving SINGLE_AGENT_SYSTEM_PROMPT so it fits all supported use cases.
Treat the system prompt as the complete control surface for behavior.
Do not propose skills, skill routing, skill removal, or any other skill-related changes.
Do not default to stricter prompts after retrieval failures. Prefer broader artifact search,
alias/partial matching, and fallback retrieval when the evidence shows recall problems.
Add stricter verification only when hallucination or unsupported claims are the issue.

Preserve these sections exactly:
Scope:
- For SQL or database questions about the synthetic startup dataset, use the database tools and follow the evidence rules below.
- For non-SQL and non-database questions, do not use tools. Answer plainly and directly in natural language.

Answering Style:
- Deliver clear, brief, colloquial answers strictly grounded in evidence.
- Avoid bullet points unless explicitly requested by the user.
- Answers for SQL/database questions must be 2-4 sentences unless more detail is requested.
- Do not over-claim or speculate beyond retrieved evidence.
- If evidence is insufficient, explicitly state that instead of guessing.
- Always cite concrete evidence: artifact names, exact dates, commands, milestone names, or field mappings.
""".strip()

APPLIER_SYSTEM_PROMPT = """
You are applying an approved autoresearch proposal to a deep-agent SQLite QA system.

Return:
- a complete replacement value for SINGLE_AGENT_SYSTEM_PROMPT
- a short change summary

Preserve these sections exactly:
Scope:
- For SQL or database questions about the synthetic startup dataset, use the database tools and follow the evidence rules below.
- For non-SQL and non-database questions, do not use tools. Answer plainly and directly in natural language.

Answering Style:
- Deliver clear, brief, colloquial answers strictly grounded in evidence.
- Avoid bullet points unless explicitly requested by the user.
- Answers for SQL/database questions must be 2-4 sentences unless more detail is requested.
- Do not over-claim or speculate beyond retrieved evidence.
- If evidence is insufficient, explicitly state that instead of guessing.
- Always cite concrete evidence: artifact names, exact dates, commands, milestone names, or field mappings.
""".strip()


@dataclass(frozen=True)
class LoopState:
    best_score: float = 0.0
    best_iteration: int | None = None
    best_commit_sha: str | None = None
    best_single_agent_model: str | None = None
    current_single_agent_model: str | None = None
    last_completed_iteration: int = 0


@dataclass(frozen=True)
class EvalSummary:
    total_rows: int
    average_correctness: float
    average_tool_calls: float
    average_latency_seconds: float


@dataclass(frozen=True)
class EvalResultRow:
    input: str
    output: str
    my_answer: str
    correctness: float
    correctness_reasoning: str
    trajectory: str
    latency_seconds: float


@dataclass(frozen=True)
class FailureExample:
    input: str
    expected_output: str
    generated_answer: str
    correctness: float
    correctness_reasoning: str
    trajectory: str


class Proposal(BaseModel):
    hypothesis: str
    observed_failure_modes: list[str] = Field(default_factory=list)
    system_prompt_changes: list[str | dict[str, str]] = Field(default_factory=list)
    single_agent_model: str | None = None
    risks: list[str] = Field(default_factory=list)


class AppliedSources(BaseModel):
    single_agent_system_prompt: str = Field(
        validation_alias=AliasChoices(
            "single_agent_system_prompt",
            "system_prompt",
            "updated_system_prompt",
        )
    )
    change_summary: str = Field(
        default="",
        validation_alias=AliasChoices("change_summary", "summary"),
    )


def load_state(path: Path = STATE_PATH) -> LoopState:
    if not path.exists():
        return LoopState()
    return LoopState(**json.loads(path.read_text(encoding="utf-8")))


def save_state(state: LoopState, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")


def select_failure_examples(rows: list[EvalResultRow], *, limit: int) -> list[FailureExample]:
    failures = sorted(rows, key=lambda row: row.correctness)[:limit]
    return [
        FailureExample(
            input=row.input,
            expected_output=row.output,
            generated_answer=row.my_answer,
            correctness=row.correctness,
            correctness_reasoning=row.correctness_reasoning,
            trajectory=row.trajectory,
        )
        for row in failures
    ]


def write_analyzer_artifacts(
    run_dir: Path,
    *,
    payload: dict[str, str],
    failure_examples: list[FailureExample],
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    runtime_source_path = PROJECT_ROOT / MUTABLE_FILES[0]
    analyzer_input = dict(payload)
    analyzer_input["failure_examples"] = [asdict(example) for example in failure_examples]
    analyzer_input["runtime_source_path"] = str(runtime_source_path)
    (run_dir / "analyzer_input.json").write_text(
        json.dumps(analyzer_input, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "analyzer_runtime_source.py").write_text(
        payload["runtime_source"],
        encoding="utf-8",
    )


def require_proposal(proposal: Proposal | None) -> Proposal:
    if proposal is None:
        raise RuntimeError("no structured Proposal returned")
    return proposal


def run_command(
    command: list[str],
    *,
    cwd: Path = PROJECT_ROOT,
    env: dict[str, str] | None = None,
    stream_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=not stream_output,
        check=True,
    )


def git_status_paths() -> list[str]:
    result = run_command(["git", "status", "--porcelain"])
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


def assert_only_mutable_files_changed(*, baseline_paths: set[str]) -> None:
    allowed = {str(path) for path in MUTABLE_FILES}
    for path in git_status_paths():
        if path in baseline_paths or path.startswith(".autoresearch/"):
            continue
        if path not in allowed:
            raise RuntimeError(f"changed path outside the allowed set: {path}")


def current_head_sha() -> str:
    return run_command(["git", "rev-parse", "HEAD"]).stdout.strip()


def maybe_commit_new_best(iteration: int, score: float) -> str:
    diff = run_command(["git", "diff", "--name-only", "--", *[str(path) for path in MUTABLE_FILES]])
    if not diff.stdout.strip():
        return current_head_sha()
    message = f"autoresearch: new best score {score:.4f} at iteration {iteration}"
    run_command(["git", "add", *[str(path) for path in MUTABLE_FILES]])
    run_command(["git", "commit", "-m", message])
    return current_head_sha()


def restore_best_mutable_files(state: LoopState) -> bool:
    if not state.best_commit_sha:
        return False
    run_command(["git", "checkout", state.best_commit_sha, "--", *[str(path) for path in MUTABLE_FILES]])
    return True


def runtime_env() -> dict[str, str]:
    env = dict(os.environ)
    env["SINGLE_AGENT_MODEL_PROVIDER"] = OPENAI_PROVIDER
    env["SINGLE_AGENT_MODEL"] = get_settings().single_agent.model
    return env


def autoresearch_model():
    settings = get_settings()
    return init_chat_model(settings.single_agent.model, model_provider=OPENAI_PROVIDER)


def apply_proposal(proposal: Proposal, current_single_agent_model: str) -> AppliedSources:
    del proposal, current_single_agent_model
    return AppliedSources(single_agent_system_prompt="", change_summary="")


def write_applied_sources(applied: AppliedSources) -> None:
    path = PROJECT_ROOT / MUTABLE_FILES[0]
    source = path.read_text(encoding="utf-8")
    marker = 'SINGLE_AGENT_SYSTEM_PROMPT = """'
    start = source.index(marker)
    end = source.index('""".strip()', start) + len('""".strip()')
    replacement = f'SINGLE_AGENT_SYSTEM_PROMPT = """\n{applied.single_agent_system_prompt}\n""".strip()'
    path.write_text(source[:start] + replacement + source[end:], encoding="utf-8")


def apply_and_write_sources(
    proposal: Proposal,
    *,
    current_single_agent_model: str,
) -> AppliedSources:
    applied = apply_proposal(proposal, current_single_agent_model=current_single_agent_model)
    write_applied_sources(applied)
    return applied


def run_eval_container(
    *,
    run_dir: Path,
    cases_path: Path,
    build: bool,
    single_agent_model: str,
) -> None:
    try:
        relative_cases_path = cases_path.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise RuntimeError("cases path must live inside the repo") from exc

    if build:
        run_command(["docker", "compose", "build", "evals"])
    run_command(["docker", "compose", "up", "-d", "postgres"])
    env = runtime_env()
    env["SINGLE_AGENT_MODEL"] = single_agent_model
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = f"/workspace/.autoresearch/runs/{run_dir.name}/results.csv"
    workbook_path = f"/workspace/.autoresearch/runs/{run_dir.name}/results.xlsx"
    summary_path = f"/workspace/.autoresearch/runs/{run_dir.name}/summary.json"
    schema_path = f"/workspace/.autoresearch/runs/{run_dir.name}/schema_snapshot.json"
    run_command(
        [
            "docker",
            "compose",
            "run",
            "--rm",
            "evals",
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "evals.main",
            "--cases",
            f"/workspace/{relative_cases_path}",
            "--output",
            output_path,
            "--workbook-output",
            workbook_path,
            "--summary-output",
            summary_path,
            "--schema-output",
            schema_path,
        ],
        env=env,
        stream_output=True,
    )


def init_autoresearch() -> None:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        save_state(LoopState(), STATE_PATH)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the autoresearch loop.")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("init", help="Create .autoresearch state files.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "init":
        init_autoresearch()
        print(f"Initialized autoresearch state at {STATE_PATH}")


if __name__ == "__main__":
    main()
