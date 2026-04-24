from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SQLiteSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    db_path: str = Field(default="data/seed/synthetic_startup.sqlite", validation_alias="DB_PATH")


class PostgresSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    dsn: str = Field(
        default="postgresql://app:app@postgres:5432/app",
        validation_alias="POSTGRES_DSN",
    )
    min_conn_size: int = Field(default=1, validation_alias="POSTGRES_MIN_CONN_SIZE")
    max_conn_size: int = Field(default=4, validation_alias="POSTGRES_MAX_CONN_SIZE")


class SlackSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    bot_token: str = ""
    app_token: str = ""


class MemorySettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    warning_threshold_tokens: int = Field(
        default=24000,
        validation_alias="MEMORY_WARNING_THRESHOLD_TOKENS",
    )
    new_session_prefix: str = Field(
        default="slack",
        validation_alias="MEMORY_THREAD_ID_PREFIX",
    )


class SingleAgentModelSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str = Field(
        default="openai",
        validation_alias="SINGLE_AGENT_MODEL_PROVIDER",
    )
    model: str = Field(
        default="gpt-4.1-mini",
        validation_alias="SINGLE_AGENT_MODEL",
    )


class EvalScorerSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str = Field(
        default="openai",
        validation_alias="EVAL_SCORER_MODEL_PROVIDER",
    )
    model: str = Field(
        default="gpt-4.1-mini",
        validation_alias="EVAL_SCORER_MODEL",
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    sqlite_db_path: str = Field(
        default="data/seed/synthetic_startup.sqlite",
        validation_alias="DB_PATH",
        exclude=True,
    )
    postgres_dsn: str = Field(
        default="postgresql://app:app@postgres:5432/app",
        validation_alias="POSTGRES_DSN",
        exclude=True,
    )
    postgres_min_conn_size: int = Field(
        default=1,
        validation_alias="POSTGRES_MIN_CONN_SIZE",
        exclude=True,
    )
    postgres_max_conn_size: int = Field(
        default=4,
        validation_alias="POSTGRES_MAX_CONN_SIZE",
        exclude=True,
    )
    slack_bot_token: str = Field(default="", validation_alias="SLACK_BOT_TOKEN", exclude=True)
    slack_app_token: str = Field(default="", validation_alias="SLACK_APP_TOKEN", exclude=True)
    memory_warning_threshold_tokens: int = Field(
        default=24000,
        validation_alias="MEMORY_WARNING_THRESHOLD_TOKENS",
        exclude=True,
    )
    memory_thread_id_prefix: str = Field(
        default="slack",
        validation_alias="MEMORY_THREAD_ID_PREFIX",
        exclude=True,
    )
    single_agent_model_provider: str = Field(
        default="openai",
        validation_alias="SINGLE_AGENT_MODEL_PROVIDER",
        exclude=True,
    )
    single_agent_model: str = Field(
        default="gpt-4.1-mini",
        validation_alias="SINGLE_AGENT_MODEL",
        exclude=True,
    )
    eval_scorer_model_provider: str = Field(
        default="openai",
        validation_alias="EVAL_SCORER_MODEL_PROVIDER",
        exclude=True,
    )
    eval_scorer_model: str = Field(
        default="gpt-4.1-mini",
        validation_alias="EVAL_SCORER_MODEL",
        exclude=True,
    )

    @property
    def sqlite(self) -> SQLiteSettings:
        return SQLiteSettings(
            db_path=self.sqlite_db_path,
        )

    @property
    def postgres(self) -> PostgresSettings:
        return PostgresSettings(
            dsn=self.postgres_dsn,
            min_conn_size=self.postgres_min_conn_size,
            max_conn_size=self.postgres_max_conn_size,
        )

    @property
    def slack(self) -> SlackSettings:
        return SlackSettings(
            bot_token=self.slack_bot_token,
            app_token=self.slack_app_token,
        )

    @property
    def memory(self) -> MemorySettings:
        return MemorySettings(
            warning_threshold_tokens=self.memory_warning_threshold_tokens,
            new_session_prefix=self.memory_thread_id_prefix,
        )

    @property
    def single_agent(self) -> SingleAgentModelSettings:
        return SingleAgentModelSettings(
            provider=self.single_agent_model_provider,
            model=self.single_agent_model,
        )

    @property
    def eval_scorer(self) -> EvalScorerSettings:
        return EvalScorerSettings(
            provider=self.eval_scorer_model_provider,
            model=self.eval_scorer_model,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
