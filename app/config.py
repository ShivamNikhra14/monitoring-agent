"""
app/config.py
─────────────
Central configuration loaded from environment variables / .env file.
All tuneable knobs live here so every other module imports from one place.
"""
from __future__ import annotations

from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Log source ─────────────────────────────────────────────────────────────
    log_file_path: str = Field(
        default="sample_logs/application.log",
        description="Path to the log file watched by TextFileCollector.",
    )

    # ── Correlation ────────────────────────────────────────────────────────────
    correlation_window_seconds: float = Field(
        default=5.0,
        description=(
            "Maximum gap (seconds) between two events that can belong "
            "to the same incident cluster."
        ),
    )
    flush_delay_seconds: float = Field(
        default=3.0,
        description=(
            "Seconds of silence after the last event before a cluster is "
            "flushed to the triage stage."
        ),
    )

    # ── Deduplication ──────────────────────────────────────────────────────────
    dedup_window_hours: float = Field(
        default=10.0,
        description="Suppress duplicate alerts for this many hours.",
    )
    state_db_path: str = Field(
        default="monitoring_state.db",
        description="Path to the SQLite database used for deduplication state.",
    )

    # ── LLM / Triage — provider selection ─────────────────────────────────────
    llm_provider: str = Field(
        default="ollama",
        description=(
            "Which triage provider to use. "
            "Supported values: 'ollama', 'openai'."
        ),
    )
    llm_model: str = Field(
        default="llama3.1:8b",
        description=(
            "Model name passed to the LLM provider. "
            "For Ollama: any model you have pulled (e.g. llama3.1:8b, mistral). "
            "For OpenAI: gpt-4o-mini, gpt-4o, etc."
        ),
    )

    # ── Ollama settings ────────────────────────────────────────────────────────
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description=(
            "Base URL of the Ollama server. "
            "Change this if Ollama is running on a different host or port."
        ),
    )

    # ── OpenAI settings (only needed when llm_provider=openai) ────────────────
    openai_api_key: str = Field(
        default="",
        description="OpenAI API key (required only when llm_provider=openai).",
    )
    openai_base_url: str = Field(
        default="",
        description=(
            "Optional: override the OpenAI API base URL "
            "(e.g. for Azure OpenAI or a custom proxy). "
            "Leave empty to use the official OpenAI endpoint."
        ),
    )

    # ── Custom triage rules ────────────────────────────────────────────────────
    custom_triage_rules: List[str] = Field(
        default_factory=list,
        description=(
            "Codebase-specific rules injected into the triage prompt. "
            "Provide as a JSON array or comma-separated string."
        ),
    )


# Module-level singleton — import `settings` everywhere
settings = Settings()
