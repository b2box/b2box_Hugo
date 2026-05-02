"""Configuración global de Hugo. Lee de .env vía pydantic-settings.

Usa los nombres de variables del ecosistema B2Box (compartidas con Paco/Luis).
"""

from functools import lru_cache

from pydantic import EmailStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignora vars de otros agentes (img-paco, R2, supabase, etc.)
    )

    # ── Vendure Admin API ──────────────────────────────────────
    vendure_api_url: str = Field(..., description="https://admin.b2-box.com/admin-api")
    # Bearer opcional: si está vacío, Hugo se loguea con user/pass al primer call.
    # El cliente también renueva automáticamente cuando el bearer expira.
    vendure_bearer: str = Field(default="", description="Bearer (opcional, se obtiene con login)")
    vendure_channel_token: str | None = Field(
        default=None,
        description="Header vendure-token: define el canal (e.g. 'ar')",
    )
    # Necesarios para el auto-login (renovación automática del bearer)
    vendure_user: str | None = None
    vendure_pass: str | None = None
    vendure_source_url_field: str = Field(
        default="supplierLink",
        description="Custom field en Vendure con el link del proveedor (Alibaba/AliExpress)",
    )

    # ── RapidAPI (proxy a 1688/Taobao/AliExpress vía OTAPI) ────
    rapidapi_key: str = Field(default="", description="X-RapidAPI-Key compartida")
    otapi_1688_host: str = Field(default="otapi-1688.p.rapidapi.com")

    # ── Auth: API key para que Luis se autentique al pegarle a /verify ──
    hugo_api_key: str = Field(default="", description="Si vacío, /verify queda abierto (no recomendado)")

    # ── Integración con Paco (cuando NO es duplicado, le reenviamos) ──
    paco_url: str = Field(default="https://paco.b2box.app")
    paco_api_key: str = ""
    paco_submit_path: str = "/api/search/start"
    paco_status_path: str = "/api/searches"  # se completa con /{id}
    paco_cf_client_id: str = ""
    paco_cf_client_secret: str = ""

    # ── DB local ───────────────────────────────────────────────
    database_url: str = Field(default="sqlite:///./hugo.db")

    # ── Dedup thresholds (0-1) ─────────────────────────────────
    dedup_url_threshold: float = 1.0
    dedup_image_threshold: float = 0.92
    dedup_text_threshold: float = 0.88

    # ── Pricing ────────────────────────────────────────────────
    price_drift_threshold: float = 0.05
    price_drift_max_auto: float = 0.30

    # ── Scheduler ──────────────────────────────────────────────
    audit_interval_hours: int = 24

    # ── Alertas: email (SMTP) ──────────────────────────────────
    alert_smtp_host: str = "smtp.gmail.com"
    alert_smtp_port: int = 587
    alert_smtp_user: str = ""
    alert_smtp_pass: str = ""
    alert_email_to: EmailStr | str = "tech@b2box.pro"
    alert_email_from: str = ""  # default: usa alert_smtp_user

    # ── Alertas: webhook genérico ──────────────────────────────
    alert_webhook_url: str = ""
    alert_webhook_method: str = "POST"
    alert_webhook_template: str = ""  # vacío → JSON {"text": "subject\nbody"}

    # ── Logging ────────────────────────────────────────────────
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
