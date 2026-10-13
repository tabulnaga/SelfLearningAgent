"""Environment-driven configuration (spec §23). Nothing here holds a credential default."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- runtime ------------------------------------------------------------
    app_env: Literal["sim", "staging", "prod"] = "sim"
    log_level: str = "INFO"
    api_keys: str = Field(
        default="",
        description="Comma-separated 'key:tenant:role1|role2' entries. Empty = auth disabled (sim only).",
    )

    # --- LLM ----------------------------------------------------------------
    llm_provider: Literal["anthropic", "fake"] = "anthropic"
    llm_model: str = "claude-opus-5-5"
    llm_max_tokens: int = 2048
    llm_timeout_s: float = 90.0
    anthropic_api_key: SecretStr | None = None

    # --- database -----------------------------------------------------------
    database_url: str = "postgresql://agent:agent@localhost:5432/agent"
    db_pool_max: int = 10

    # --- Hindsight ----------------------------------------------------------
    hindsight_enabled: bool = True
    hindsight_url: str = "http://localhost:8888"
    hindsight_api_key: SecretStr | None = None
    hindsight_bank_id: str = "support-ops"
    hindsight_timeout_s: float = 8.0
    recall_budget: Literal["low", "mid", "high"] = "low"
    recall_max_tokens: int = 2048
    retain_async: bool = True
    retain_min_confidence: float = 0.5

    # --- RAG ----------------------------------------------------------------
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    rerank_enabled: bool = True
    chunk_tokens: int = 400
    chunk_overlap_tokens: int = 60
    rag_candidates: int = 20
    rag_top_k: int = 8
    rerank_top_k: int = 5
    rag_min_rerank_score: float = -4.0
    rag_min_fused_score: float = 0.0

    # --- sessions / workers -------------------------------------------------
    session_timeout_minutes: int = 30
    monitor_interval_s: int = 30
    memory_job_interval_s: int = 5
    memory_job_max_attempts: int = 6
    memory_job_lease_s: int = 120

    # --- agent --------------------------------------------------------------
    max_tool_rounds: int = 2
    write_tools_auto_approve: bool | None = Field(
        default=None, description="None = auto-approve only when APP_ENV=sim (decision D2)."
    )

    @property
    def write_tools_allowed(self) -> bool:
        if self.write_tools_auto_approve is not None:
            return self.write_tools_auto_approve
        return self.app_env == "sim"


@lru_cache
def get_settings() -> Settings:
    return Settings()
