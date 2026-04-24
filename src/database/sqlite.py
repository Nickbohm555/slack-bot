from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_QUERY_MAX_ROWS = 5
DEFAULT_QUERY_MAX_CELL_CHARS = 800
VISIBLE_VIRTUAL_TABLES = {"artifacts_fts"}
SQL_CLAUSE_BOUNDARIES = (
    "group",
    "order",
    "limit",
    "having",
    "union",
    "intersect",
    "except",
)


def _quote_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def list_visible_tables(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("PRAGMA table_list").fetchall()
    tables = [
        row[1]
        for row in rows
        if row[0] == "main"
        and (
            row[2] not in {"shadow", "virtual"}
            or row[1] in VISIBLE_VIRTUAL_TABLES
        )
        and not row[1].startswith("sqlite_")
    ]
    return sorted(tables)


def get_create_table_sql(db_path: Path, table_name: str) -> str | None:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0]) if row[0] is not None else None


def fetch_sample_rows(db_path: Path, table_name: str, *, limit: int = 3) -> dict[str, object]:
    with sqlite3.connect(db_path) as connection:
        cursor = connection.execute(f"SELECT * FROM {_quote_identifier(table_name)} LIMIT ?", (limit,))
        rows = cursor.fetchall()
        columns = [description[0] for description in cursor.description or ()]
    return {
        "columns": columns,
        "rows": [list(row) for row in rows],
        "row_count": len(rows),
    }


def _truncate_cell(value: object, *, max_chars: int) -> tuple[object, bool]:
    if not isinstance(value, str) or len(value) <= max_chars:
        return value, False
    omitted_chars = len(value) - max_chars
    return (f"{value[:max_chars]}\n\n[truncated {omitted_chars} chars]", True)


def _without_quoted_sql_text(sql: str) -> str:
    result: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    result.extend("  ")
                    index += 2
                    continue
                quote = None
            result.append(" ")
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            result.append(" ")
            index += 1
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _where_clause(sql: str) -> str:
    cleaned = _without_quoted_sql_text(sql)
    lowered = cleaned.lower()
    where_index = lowered.find(" where ")
    if where_index == -1:
        return ""
    start = where_index + len(" where ")
    end = len(cleaned)
    for boundary in SQL_CLAUSE_BOUNDARIES:
        boundary_index = lowered.find(f" {boundary} ", start)
        if boundary_index != -1:
            end = min(end, boundary_index)
    return cleaned[start:end]


def _top_level_boolean_ops(clause: str) -> set[str]:
    ops: set[str] = set()
    depth = 0
    token: list[str] = []

    def flush_token() -> None:
        if depth == 0:
            word = "".join(token).lower()
            if word in {"and", "or"}:
                ops.add(word)
        token.clear()

    for char in clause:
        if char == "(":
            flush_token()
            depth += 1
            continue
        if char == ")":
            flush_token()
            depth = max(0, depth - 1)
            continue
        if char.isalnum() or char == "_":
            token.append(char)
            continue
        flush_token()
    flush_token()
    return ops


def validate_query(query: str) -> None:
    top_level_ops = _top_level_boolean_ops(_where_clause(query))
    if {"and", "or"}.issubset(top_level_ops):
        raise ValueError("Ambiguous WHERE clause: mixed top-level AND/OR requires explicit parentheses.")


def execute_query(db_path: Path, query: str) -> dict[str, object]:
    validate_query(query)
    with sqlite3.connect(db_path) as connection:
        cursor = connection.execute(query)
        rows = cursor.fetchmany(DEFAULT_QUERY_MAX_ROWS + 1)
        columns = [description[0] for description in cursor.description or ()]
    result_rows = rows[:DEFAULT_QUERY_MAX_ROWS]
    row_limited = len(rows) > DEFAULT_QUERY_MAX_ROWS
    cell_truncated = False
    serialized_rows: list[list[object]] = []
    for row in result_rows:
        serialized_row: list[object] = []
        for value in row:
            truncated_value, was_truncated = _truncate_cell(value, max_chars=DEFAULT_QUERY_MAX_CELL_CHARS)
            cell_truncated = cell_truncated or was_truncated
            serialized_row.append(truncated_value)
        serialized_rows.append(serialized_row)
    result: dict[str, object] = {
        "columns": columns,
        "rows": serialized_rows,
        "row_count": len(serialized_rows),
    }
    if row_limited or cell_truncated:
        result["truncated"] = True
        result["truncation"] = {
            "max_rows": DEFAULT_QUERY_MAX_ROWS,
            "max_cell_chars": DEFAULT_QUERY_MAX_CELL_CHARS,
            "row_limit_applied": row_limited,
            "cell_truncation_applied": cell_truncated,
        }
    return result
