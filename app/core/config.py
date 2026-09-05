from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class AppConfig(BaseModel):
    name: str = "tvs-ai-gateway"
    env: str = "dev"
    host: str = "0.0.0.0"
    port: int = 8000
    docs_url: str = "/docs"
    openapi_url: str = "/openapi.json"


class ProfilesConfig(BaseModel):
    active: str = "dev"


class SalesAgentConfig(BaseModel):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:7071"
    request_path: str = "/api/assistant/message"
    health_path: str = "/api/admin/dashboard"
    execute_path: str = "/api/v1/agent/requests/{requestId}/execute"
    status_path: str = "/api/v1/agent/requests/{requestId}"
    default_tenant: str = "default"
    connect_timeout_sec: int = Field(default=5, ge=1)
    request_timeout_sec: int = Field(default=30, ge=1)
    execute_timeout_sec: int = Field(default=180, ge=1)
    download_timeout_sec: int = Field(default=300, ge=1)
    api_key: str | None = None
    api_key_header: str = "X-Agent-Api-Key"
    caller: str = "fastapi"


class MinioConfig(BaseModel):
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:9000"
    access_key: str | None = None
    secret_key: str | None = None
    secure: bool = False
    bucket_exports: str = "tvs-agent-exports"
    region: str | None = None
    connect_timeout_seconds: int = Field(default=5, ge=1)
    read_timeout_seconds: int = Field(default=30, ge=1)
    presigned_ttl_seconds: int = Field(default=300, ge=1)
    retention_days: int = Field(default=7, ge=1)


class RequestProjectionConfig(BaseModel):
    enabled: bool = True
    database_url: str = "sqlite:///./data/user_request_projection.db"
    preview_row_limit: int = Field(default=100, ge=0, le=1000)
    admin_roles: list[str] = Field(default_factory=lambda: ["ADMIN", "TVS_ADMIN"])


class SecurityConfig(BaseModel):
    enabled: bool = True
    protected_prefixes: list[str] = Field(
        default_factory=lambda: ["/api/assistant", "/api/requests", "/api/artifacts"]
    )
    exempt_paths: list[str] = Field(
        default_factory=lambda: [
            "/docs", "/openapi.json", "/redoc", "/health", "/health/agent", "/api/health/"
        ]
    )
    api_keys: list[str] = Field(default_factory=list)
    header_name: str = "X-API-Key"


class CorsConfig(BaseModel):
    enabled: bool = True
    allow_origins: list[str] = Field(default_factory=list)
    allow_origin_regex: str | None = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
    allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    allow_headers: list[str] = Field(default_factory=lambda: ["*"])
    expose_headers: list[str] = Field(default_factory=lambda: ["X-Request-Id"])
    allow_credentials: bool = True
    max_age: int = 600


class LoggingConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    level: str = "INFO"
    json_enabled: bool = Field(default=True, alias="json")
    pii_mask_keys: list[str] = Field(
        default_factory=lambda: ["api_key", "authorization", "token", "password", "secret"]
    )


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    profiles: ProfilesConfig = Field(default_factory=ProfilesConfig)
    sales_agent: SalesAgentConfig = Field(default_factory=SalesAgentConfig)
    minio: MinioConfig = Field(default_factory=MinioConfig)
    request_projection: RequestProjectionConfig = Field(default_factory=RequestProjectionConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    cors: CorsConfig = Field(default_factory=CorsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def _load_yaml(path: Path) -> dict[str, Any]:
    raw = Template(path.read_text(encoding="utf-8")).safe_substitute(os.environ)
    loaded = yaml.safe_load(raw) or {}
    if not isinstance(loaded, dict):
        raise ValueError("Config file root must be a dictionary")
    return loaded


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _apply_env(target: dict[str, Any], mapping: dict[str, str]) -> None:
    for env_name, field_name in mapping.items():
        value = os.getenv(env_name)
        if value is not None:
            target[field_name] = value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    config_path = Path(os.getenv("APP_CONFIG_FILE", "configs/app.yaml")).resolve()
    data = _load_yaml(config_path)
    default_env = str(
        data.get("profiles", {}).get("active") or data.get("app", {}).get("env") or "dev"
    )
    app_env = os.getenv("APP_ENV", default_env).strip().lower() or default_env
    override_path = config_path.with_name(f"app.{app_env}.yaml")
    if override_path.exists():
        data = _deep_merge(data, _load_yaml(override_path))
    data.setdefault("app", {})["env"] = app_env
    data.setdefault("profiles", {})["active"] = app_env

    sales_agent = data.setdefault("sales_agent", {})
    _apply_env(sales_agent, {
        "TV_SALES_AGENT_BASE_URL": "base_url",
        "TV_SALES_AGENT_REQUEST_PATH": "request_path",
        "TV_SALES_AGENT_HEALTH_PATH": "health_path",
        "TV_SALES_AGENT_EXECUTE_PATH": "execute_path",
        "TV_SALES_AGENT_STATUS_PATH": "status_path",
        "TV_SALES_AGENT_REQUEST_TIMEOUT": "request_timeout_sec",
        "TV_SALES_AGENT_EXECUTE_TIMEOUT": "execute_timeout_sec",
        "TV_SALES_AGENT_DOWNLOAD_TIMEOUT": "download_timeout_sec",
        "TV_SALES_AGENT_ENABLED": "enabled",
        "TV_SALES_AGENT_API_KEY": "api_key",
        "TV_SALES_DEFAULT_TENANT": "default_tenant",
        "TVSALES_AGENT_BASE_URL": "base_url",
        "TVSALES_AGENT_REQUEST_PATH": "request_path",
        "TVSALES_AGENT_HEALTH_PATH": "health_path",
        "TVSALES_AGENT_EXECUTE_PATH": "execute_path",
        "TVSALES_AGENT_STATUS_PATH": "status_path",
        "TVSALES_AGENT_CONNECT_TIMEOUT": "connect_timeout_sec",
        "TVSALES_AGENT_REQUEST_TIMEOUT": "request_timeout_sec",
        "TVSALES_AGENT_EXECUTE_TIMEOUT": "execute_timeout_sec",
        "TVSALES_AGENT_DOWNLOAD_TIMEOUT": "download_timeout_sec",
        "TVSALES_AGENT_ENABLED": "enabled",
        "TVSALES_AGENT_API_KEY": "api_key",
        "TVSALES_AGENT_CALLER": "caller",
        "TVSALES_AGENT_DEFAULT_TENANT": "default_tenant",
    })
    _apply_env(data.setdefault("minio", {}), {
        "MINIO_ENABLED": "enabled",
        "MINIO_ENDPOINT": "endpoint",
        "MINIO_ACCESS_KEY": "access_key",
        "MINIO_SECRET_KEY": "secret_key",
        "MINIO_SECURE": "secure",
        "MINIO_BUCKET_EXPORTS": "bucket_exports",
        "MINIO_REGION": "region",
        "MINIO_CONNECT_TIMEOUT_SECONDS": "connect_timeout_seconds",
        "MINIO_READ_TIMEOUT_SECONDS": "read_timeout_seconds",
        "MINIO_PRESIGNED_TTL_SECONDS": "presigned_ttl_seconds",
        "MINIO_RETENTION_DAYS": "retention_days",
    })
    _apply_env(data.setdefault("request_projection", {}), {
        "REQUEST_PROJECTION_ENABLED": "enabled",
        "REQUEST_PROJECTION_DATABASE_URL": "database_url",
        "REQUEST_PROJECTION_PREVIEW_ROW_LIMIT": "preview_row_limit",
    })
    api_key = os.getenv("FASTAPI_API_KEY")
    if api_key:
        data.setdefault("security", {})["api_keys"] = [api_key]
    return Settings.model_validate(data)
