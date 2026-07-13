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

    # ── Entorno ────────────────────────────────────────────────
    # "production" hace que el arranque FALLE si faltan credenciales
    # (DASHBOARD_PASSWORD / HUGO_API_KEY). En "development" solo warnea.
    hugo_env: str = Field(default="development", description="development | production")

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

    # ── Auth: login del dashboard vía Supabase (mismo pool que Paco) ──
    # Si supabase_url + supabase_anon_key están seteados, el login del dashboard
    # valida email+contraseña contra el Supabase Auth de Cloud_B2BOX (mismos
    # usuarios que Paco). Es el método PREFERIDO; el user/pass local de abajo
    # queda solo como fallback de desarrollo.
    supabase_url: str = Field(default="", description="https://<ref>.supabase.co (Cloud_B2BOX)")
    supabase_anon_key: str = Field(default="", description="anon/publishable key de Cloud_B2BOX")
    # Allowlist opcional de emails con acceso a Hugo (coma-separada). Vacío = todo
    # usuario válido de Cloud_B2BOX puede entrar (igual que Paco).
    supabase_allowed_emails: str = Field(default="", description="emails permitidos, coma-separados")

    # ── Auth: login local (FALLBACK de desarrollo) ─────────────
    # Usuario/contraseña que protegen el dashboard y sus endpoints /api/*.
    # Solo se usa si Supabase NO está configurado. Si ambos están vacíos, el
    # login queda DESHABILITADO (modo dev). En producción, usá Supabase.
    dashboard_user: str = Field(default="admin", description="Usuario del login del dashboard")
    dashboard_password: str = Field(default="", description="Si vacío, el dashboard queda abierto (no recomendado)")
    # Secreto para firmar la cookie de sesión. Si vacío, se deriva de la
    # password (estable entre reinicios/workers mientras la password no cambie).
    dashboard_secret: str = Field(default="", description="Secreto para firmar la cookie de sesión")
    # Duración de la sesión en horas.
    dashboard_session_hours: int = 12

    # ── Integración con Paco (cuando NO es duplicado, le reenviamos) ──
    # Luis / b2box-app  → Paco APP  (paco_url, /api/search/start, JSON).
    # Admin / b2box-pro → Paco PRO  (paco_pro_url, /api/tech/start, form + callback_ctx).
    paco_url: str = Field(default="https://paco-app.b2box.app")
    paco_api_key: str = ""  # X-API-Key: gate PACO_INGEST_API_KEY del endpoint (image_url).
    # Auth máquina-a-máquina del middleware de Paco APP (PACO_SUPABASE_AUTH on):
    # se manda como `Authorization: Bearer <token>`. Es el PACO_ADMIN_TOKEN de Paco APP.
    paco_admin_token: str = ""
    paco_submit_path: str = "/api/search/start"
    paco_status_path: str = "/api/searches"  # se completa con /{id}
    paco_cf_client_id: str = ""
    paco_cf_client_secret: str = ""
    # Paco PRO = b2box_sourcing. Misma URL que usa el edge fn (PACO_PRO_URL).
    # OBLIGATORIO para el flujo PRO — vacío → submit_pro FALLA (ya no cae a Paco APP).
    paco_pro_url: str = Field(default="", description="URL de Paco PRO (b2box_sourcing)")
    paco_pro_submit_path: str = "/api/tech/start"
    # X-API-Key de b2box_sourcing (su PACO_API_KEY). b2box_sourcing NO usa Bearer:
    # el bypass máquina es por X-API-Key. Es un secreto DISTINTO al de Paco APP.
    paco_pro_api_key: str = ""

    # ── DB local ───────────────────────────────────────────────
    database_url: str = Field(default="sqlite:///./hugo.db")

    # ── Dedup thresholds (0-1) ─────────────────────────────────
    dedup_url_threshold: float = 1.0
    dedup_image_threshold: float = 0.92
    dedup_text_threshold: float = 0.88
    # Gate de costo: solo descargamos+hasheamos imágenes (network $) para comparar
    # un par cuando su similitud de texto ya supera este umbral. Corta el O(N²) de
    # descargas de imagen entre productos claramente no relacionados. 0.0 = sin gate.
    dedup_image_text_gate: float = 0.35
    # Tope de la cache in-process de pHash (evita OOM en procesos long-running).
    dedup_image_cache_max: int = 5000

    # ── Cache del catálogo para /verify ────────────────────────
    # Segundos que Hugo cachea el catálogo Vendure entre llamadas de /verify,
    # para no re-descargar todo el catálogo (y sus imágenes) en cada 👍 de Luis.
    verify_catalog_ttl_seconds: int = 300

    # ── Pricing ────────────────────────────────────────────────
    price_drift_threshold: float = 0.05
    price_drift_max_auto: float = 0.30

    # ── Scheduler ──────────────────────────────────────────────
    # Default 336h = 14 días. Las auditorías hacen poco "cambio real" día a día
    # y la de precios consume RapidAPI/OTAPI ($) — convenientemente bajo.
    audit_interval_hours: int = 336

    # Retención de snapshots de precio: se borran los PriceHistory más viejos
    # que esto (días). Evita que la tabla crezca sin techo en Supabase. 0 = nunca.
    price_history_retention_days: int = 120

    # ── Budget diario de calls a OTAPI (RapidAPI) ──────────────
    # Cap defensivo: si llegamos a este número de snapshots 1688_otapi
    # en el día (UTC), los siguientes fetch devuelven None (skip).
    otapi_daily_budget: int = 300

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
