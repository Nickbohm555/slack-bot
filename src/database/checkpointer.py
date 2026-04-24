from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool
from sqlalchemy import Engine, create_engine

from config import Settings


@dataclass(frozen=True)
class RuntimeDependencies:
    postgres_pool: ConnectionPool
    postgres_engine: Engine
    checkpointer: Any | None = None
    sqlite_db_path: Path = Path("data/seed/synthetic_startup.sqlite")
    warning_threshold_tokens: int = 24000
    settings: Settings | None = None


def build_postgres_pool(settings: Settings) -> ConnectionPool:
    return ConnectionPool(
        conninfo=settings.postgres.dsn,
        kwargs={"autocommit": True},
        min_size=settings.postgres.min_conn_size,
        max_size=settings.postgres.max_conn_size,
        open=False,
    )


def build_checkpointer(pool: ConnectionPool) -> Any | None:
    return PostgresSaver(pool)


def build_postgres_engine(settings: Settings) -> Engine:
    dsn = settings.postgres.dsn
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(dsn)


def build_runtime_dependencies(settings: Settings) -> RuntimeDependencies:
    pool = build_postgres_pool(settings)
    pool.open()
    engine = build_postgres_engine(settings)
    checkpointer = build_checkpointer(pool)
    if checkpointer is not None:
        checkpointer.setup()
    return RuntimeDependencies(
        postgres_pool=pool,
        postgres_engine=engine,
        checkpointer=checkpointer,
        sqlite_db_path=Path(settings.sqlite.db_path),
        warning_threshold_tokens=settings.memory.warning_threshold_tokens,
        settings=settings,
    )


def close_runtime_dependencies(dependencies: RuntimeDependencies) -> None:
    dependencies.postgres_pool.close()
    dependencies.postgres_engine.dispose()
