from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ACP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/control-plane.db"
    )
    sql_echo: bool = False
    lease_sweep_interval_seconds: float = Field(default=5.0, gt=0)
    default_retry_delay_seconds: int = Field(default=5, ge=0)
    enable_lease_sweeper: bool = True
    api_token: SecretStr | None = None
    worker_offline_after_seconds: int = Field(default=20, ge=5, le=3600)
    managed_runner_workflow: str = "WORKFLOW.md"
    issue_workspace_root: str = ".workspaces"
    managed_runner_autostart: bool = False
    managed_runner_worker_id: str = "windows-symphony-managed"
