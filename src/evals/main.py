from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from langchain.chat_models import init_chat_model
from langchain_core.prompts import ChatPromptTemplate
from openpyxl import Workbook
from pydantic import BaseModel, Field

from agents import invoke_agent_runtime
from config import Settings, get_settings
from database import build_runtime_dependencies, close_runtime_dependencies


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CASES_PATH = PROJECT_ROOT / "benchmarks" / "example_queries.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "benchmarks" / "example_queries_results.csv"


@dataclass(frozen=True)
class EvalCase:
    input: str
    output: str


class CorrectnessScore(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str


@dataclass(frozen=True)
class EvalRow:
    input: str
    output: str
    my_answer: str
    correctness: float
    correctness_reasoning: str
    trajectory: str
    latency_seconds: float


@dataclass(frozen=True)
class EvalSummary:
    total_rows: int
    average_correctness: float
    average_tool_calls: float
    average_latency_seconds: float


@dataclass(frozen=True)
class TableColumnSnapshot:
    name: str
    data_type: str
    not_null: bool
    default_value: str | None
    primary_key_position: int


@dataclass(frozen=True)
class TableSchemaSnapshot:
    table_name: str
    row_count: int
    columns: list[TableColumnSnapshot]


@dataclass(frozen=True)
class SQLiteSchemaSnapshot:
    db_path: str
    tables: list[TableSchemaSnapshot]


def load_eval_cases(path: Path) -> list[EvalCase]:
    payload = json.loads(path.read_text())
    return [EvalCase(input=item["input"], output=item["output"]) for item in payload]


def build_correctness_scorer(
    settings: Settings,
) -> Callable[[dict[str, str]], CorrectnessScore]:
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "You are grading a database QA answer. Score correctness from 0.0 to 1.0.\n"
                    "Use 1.0 only when the generated answer is materially equivalent to the expected answer.\n"
                    "Penalize factual errors, missed required details, and unsupported claims.\n"
                    "Return only the structured score and a brief reasoning string."
                ),
            ),
            (
                "human",
                (
                    "Question:\n{input}\n\n"
                    "Expected answer:\n{output}\n\n"
                    "Generated answer:\n{my_answer}"
                ),
            ),
        ]
    )
    model = init_chat_model(
        settings.eval_scorer.model,
        model_provider=settings.eval_scorer.provider,
    )
    chain = prompt | model.with_structured_output(CorrectnessScore)

    def score(payload: dict[str, str]) -> CorrectnessScore:
        return chain.invoke(payload)

    return score


def _format_tool_args(args: object) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    parts: list[str] = []
    for key, value in args.items():
        rendered = json.dumps(value, ensure_ascii=True, default=str)
        parts.append(f"{key}={rendered}")
    return ", ".join(parts)


def build_trajectory(messages: Sequence[dict[str, object]]) -> str:
    steps: list[str] = []
    for message in messages:
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for index, tool_call in enumerate(tool_calls, start=len(steps) + 1):
            if not isinstance(tool_call, dict):
                continue
            name = str(tool_call.get("name") or "unknown_tool")
            args = _format_tool_args(tool_call.get("args", {}))
            rendered = f"{name}({args})" if args else f"{name}()"
            steps.append(f"{index}. {rendered}")
    return "\n".join(steps)


def count_tool_calls(trajectory: str) -> int:
    return sum(1 for line in trajectory.splitlines() if line.strip())


def run_eval_cases(
    cases: Sequence[EvalCase],
    *,
    dependencies: object,
    scorer: Callable[[dict[str, str]], CorrectnessScore],
) -> list[EvalRow]:
    results: list[EvalRow] = []
    run_id = f"eval-run-{uuid4().hex}"
    for index, case in enumerate(cases, start=1):
        started_at = time.perf_counter()
        runtime_answer = invoke_agent_runtime(
            text=case.input,
            thread_id=f"{run_id}-case-{index}",
            slack_user_id="eval-user",
            conversation_key=f"{run_id}-case-{index}",
            sqlite_db_path=dependencies.sqlite_db_path,
            checkpointer=dependencies.checkpointer,
            settings=dependencies.settings,
        )
        latency_seconds = time.perf_counter() - started_at
        score = scorer(
            {
                "input": case.input,
                "output": case.output,
                "my_answer": runtime_answer.answer,
            }
        )
        results.append(
            EvalRow(
                input=case.input,
                output=case.output,
                my_answer=runtime_answer.answer,
                correctness=score.score,
                correctness_reasoning=score.reasoning,
                trajectory=build_trajectory(runtime_answer.messages),
                latency_seconds=latency_seconds,
            )
        )
    return results


def write_eval_results(path: Path, rows: Sequence[EvalRow], summary: EvalSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "input",
        "output",
        "my_answer",
        "correctness",
        "correctness_reasoning",
        "trajectory",
        "latency_seconds",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
        writer.writerow({key: "" for key in fieldnames})
        writer.writerow({"input": "summary", "output": "value"})
        writer.writerow({"input": "total_rows", "output": str(summary.total_rows)})
        writer.writerow({"input": "average_correctness", "output": str(summary.average_correctness)})
        writer.writerow({"input": "average_tool_calls", "output": str(summary.average_tool_calls)})
        writer.writerow({"input": "average_latency_seconds", "output": str(summary.average_latency_seconds)})


def write_eval_workbook(path: Path, rows: Sequence[EvalRow], summary: EvalSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()

    details_sheet = workbook.active
    details_sheet.title = "results"
    detail_headers = [
        "input",
        "output",
        "my_answer",
        "correctness",
        "correctness_reasoning",
        "trajectory",
        "latency_seconds",
    ]
    details_sheet.append(detail_headers)
    for row in rows:
        payload = asdict(row)
        details_sheet.append([payload[header] for header in detail_headers])
    details_sheet.append([])
    details_sheet.append(["summary", "value"])
    details_sheet.append(["total_rows", summary.total_rows])
    details_sheet.append(["average_correctness", summary.average_correctness])
    details_sheet.append(["average_tool_calls", summary.average_tool_calls])
    details_sheet.append(["average_latency_seconds", summary.average_latency_seconds])

    summary_sheet = workbook.create_sheet("summary")
    summary_sheet.append(["metric", "value"])
    summary_sheet.append(["total_rows", summary.total_rows])
    summary_sheet.append(["average_correctness", summary.average_correctness])
    summary_sheet.append(["average_tool_calls", summary.average_tool_calls])
    summary_sheet.append(["average_latency_seconds", summary.average_latency_seconds])

    workbook.save(path)


def write_summary_json(path: Path, summary: EvalSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(summary), indent=2) + "\n", encoding="utf-8")


def snapshot_sqlite_schema(db_path: Path) -> SQLiteSchemaSnapshot:
    tables: list[TableSchemaSnapshot] = []
    with sqlite3.connect(db_path) as connection:
        cursor = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        table_names = [str(row[0]) for row in cursor.fetchall()]

        for table_name in table_names:
            row_count = connection.execute(
                f'SELECT COUNT(*) FROM "{table_name}"'
            ).fetchone()[0]
            pragma_rows = connection.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
            columns = [
                TableColumnSnapshot(
                    name=str(column[1]),
                    data_type=str(column[2] or ""),
                    not_null=bool(column[3]),
                    default_value=None if column[4] is None else str(column[4]),
                    primary_key_position=int(column[5]),
                )
                for column in pragma_rows
            ]
            tables.append(
                TableSchemaSnapshot(
                    table_name=table_name,
                    row_count=int(row_count),
                    columns=columns,
                )
            )

    return SQLiteSchemaSnapshot(db_path=str(db_path), tables=tables)


def write_schema_snapshot(path: Path, snapshot: SQLiteSchemaSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(snapshot), indent=2) + "\n", encoding="utf-8")


def summarize_eval_results(rows: Sequence[EvalRow]) -> EvalSummary:
    total_rows = len(rows)
    if total_rows == 0:
        return EvalSummary(
            total_rows=0,
            average_correctness=0.0,
            average_tool_calls=0.0,
            average_latency_seconds=0.0,
        )

    total_correctness = sum(row.correctness for row in rows)
    total_tool_calls = sum(count_tool_calls(row.trajectory) for row in rows)
    total_latency_seconds = sum(row.latency_seconds for row in rows)
    return EvalSummary(
        total_rows=total_rows,
        average_correctness=total_correctness / total_rows,
        average_tool_calls=total_tool_calls / total_rows,
        average_latency_seconds=total_latency_seconds / total_rows,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline evals against the single-agent runtime.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--workbook-output", type=Path, default=None)
    parser.add_argument("--summary-output", type=Path, default=None)
    parser.add_argument("--schema-output", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    cases = load_eval_cases(args.cases)
    dependencies = build_runtime_dependencies(settings)
    try:
        scorer = build_correctness_scorer(settings)
        rows = run_eval_cases(cases, dependencies=dependencies, scorer=scorer)
    finally:
        close_runtime_dependencies(dependencies)

    summary = summarize_eval_results(rows)
    workbook_output = args.workbook_output or args.output.with_suffix(".xlsx")
    summary_output = args.summary_output or args.output.with_suffix(".summary.json")
    schema_output = args.schema_output or args.output.with_suffix(".schema.json")
    write_eval_results(args.output, rows, summary)
    write_eval_workbook(workbook_output, rows, summary)
    write_summary_json(summary_output, summary)
    write_schema_snapshot(
        schema_output,
        snapshot_sqlite_schema(Path(dependencies.sqlite_db_path)),
    )
    print(f"Wrote {summary.total_rows} eval rows to {args.output}")
    print("Eval summary:")
    print(f"- Average correctness: {summary.average_correctness:.3f}")
    print(f"- Average tool calls: {summary.average_tool_calls:.2f}")
    print(f"- Average latency (seconds): {summary.average_latency_seconds:.2f}")


if __name__ == "__main__":
    main()
