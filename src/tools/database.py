from __future__ import annotations

from pathlib import Path
from typing import Annotated

from langchain.tools import tool
from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolArg

from agents.schemas import AgentWorkerContext
from config import get_settings
from database.sqlite import execute_query, fetch_sample_rows, get_create_table_sql, list_visible_tables


def _default_db_path() -> Path:
    return Path(get_settings().sqlite.db_path)


def _resolve_db_path(
    *, db_path: Path | None = None, runtime: ToolRuntime[AgentWorkerContext] | None = None
) -> Path:
    if runtime is not None:
        return runtime.context.db_path
    return db_path or _default_db_path()


def list_sqlite_tables(
    *, db_path: Path | None = None, runtime: ToolRuntime[AgentWorkerContext] | None = None
) -> str:
    resolved_db_path = _resolve_db_path(db_path=db_path, runtime=runtime)
    return ", ".join(list_visible_tables(resolved_db_path))


list_tables = list_sqlite_tables


def inspect_table_schema(
    table_names: str,
    *,
    db_path: Path | None = None,
    runtime: ToolRuntime[AgentWorkerContext] | None = None,
) -> str:
    resolved_db_path = _resolve_db_path(db_path=db_path, runtime=runtime)
    available_tables = set(list_visible_tables(resolved_db_path))
    requested_tables = [name.strip() for name in table_names.split(",") if name.strip()]
    if not requested_tables:
        return "No tables provided."

    parts: list[str] = []
    for table_name in requested_tables:
        if table_name not in available_tables:
            parts.append(f"Unknown table: {table_name}")
            continue

        create_sql = get_create_table_sql(resolved_db_path, table_name) or f"CREATE TABLE {table_name} (...)"
        sample = fetch_sample_rows(resolved_db_path, table_name, limit=3)
        sample_lines = ["\t".join(sample["columns"])]
        for row in sample["rows"]:
            sample_lines.append("\t".join("" if value is None else str(value) for value in row))

        parts.append(
            "\n".join(
                [
                    create_sql,
                    "",
                    "/*",
                    f'{sample["row_count"]} rows from {table_name} table:',
                    *sample_lines,
                    "*/",
                ]
            )
        )

    return "\n\n".join(parts)


inspect_columns = inspect_table_schema


def execute_sql_query(query: str, runtime: ToolRuntime[AgentWorkerContext]) -> dict[str, object]:
    """Execute a SQLite query and return structured rows."""
    try:
        return execute_query(runtime.context.db_path, query)
    except Exception as exc:
        return {
            "error": str(exc),
            "details": (
                "execution_failed: before retrying, make sure table names and column "
                "names are valid by using sql_db_list_tables and sql_db_schema."
            ),
        }


execute_sql = execute_sql_query


@tool("sql_db_list_tables")
def list_tables_tool(
    runtime: Annotated[ToolRuntime[AgentWorkerContext], InjectedToolArg],
) -> str:
    """sql_db_list_tables: Input is an empty string, output is a comma-separated list of tables in the database."""
    return list_sqlite_tables(runtime=runtime)


@tool("sql_db_schema")
def inspect_columns_tool(
    table_names: str,
    runtime: Annotated[ToolRuntime[AgentWorkerContext], InjectedToolArg],
) -> str:
    """sql_db_schema: Input to this tool is a comma-separated list of tables, output is the schema and sample rows for those tables. Be sure that the tables actually exist by calling sql_db_list_tables first! Example Input: table1, table2, table3"""
    return inspect_table_schema(table_names, runtime=runtime)


@tool("sql_db_query")
def execute_sql_tool(
    query: str,
    runtime: Annotated[ToolRuntime[AgentWorkerContext], InjectedToolArg],
) -> dict[str, object]:
    """sql_db_query: Input to this tool is a detailed and correct SQL query using only tables and columns already confirmed by sql_db_list_tables or sql_db_schema in the current conversation. The backend returns at most 5 rows and truncates large text cells. If the query is not correct, an error message will be returned. If an error is returned, rewrite the query, check the query, and try again. If you encounter an issue with Unknown column 'xxxx' in 'field list', use sql_db_schema to query the correct table fields."""
    return execute_sql_query(query, runtime)
