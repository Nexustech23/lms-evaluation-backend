from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Secrets that must never sign real tokens. The default value below is
# deliberately in this set, so a production deploy that forgets to set
# JWT_SECRET_KEY fails fast at startup instead of silently accepting
# forgeable tokens (anyone could mint a role=1 / superadmin token).
_WEAK_JWT_SECRETS = {
    "", "change-me-in-production", "changeme", "change_me", "secret",
    "your-secret-key", "dev", "test", "jwt-secret",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    MONGODB_URI: str = "mongodb://localhost:27017"
    DB_NAME: str = "lms_evaluation"

    JWT_SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_DAYS: int = 1
    JWT_COOKIE_NAME: str = "access_token_cookie"

    ENV: str = "development"
    CORS_ORIGINS: str = "http://localhost:3000"

    # Login domain for auto-generated institute-student accounts. Not a real
    # deliverable mailbox — it's only ever used as the student's login id.
    STUDENT_EMAIL_DOMAIN: str = "students.local"

    GEMINI_API_KEY: str = ""
    IMAGEKIT_PUBLIC_KEY: str = ""
    IMAGEKIT_PRIVATE_KEY: str = ""
    IMAGEKIT_URL_ENDPOINT: str = ""

    ANTHROPIC_API_KEY: str = ""
    REDIS_URL: str = "redis://localhost:6379"

    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str = ""

    # Rate limiting (see app/core/rate_limit.py). All windows are 60s.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_GLOBAL_PER_MINUTE: int = 200          # per IP, applied to every request
    RATE_LIMIT_AI_PER_MINUTE: int = 10               # per user, on single-action AI endpoints
    RATE_LIMIT_BULK_GRADING_PER_MINUTE: int = 60      # per user, /evaluate-answer-script only —
    # its "Evaluate All" button fires one request per ungraded answer script at once
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10              # per IP, on public unauthenticated endpoints

    # Set true ONLY when the app sits behind a trusted reverse proxy / load
    # balancer that sets X-Forwarded-For. When false, rate-limit keys use the
    # direct socket peer (correct for direct exposure; behind a proxy it
    # would lump every client under the proxy's IP). Never enable this on a
    # directly-internet-facing deployment — clients could spoof the header.
    TRUST_PROXY_HEADERS: bool = False

    # SSRF guard (see app/utils/net.py). Comma-separated extra hostnames the
    # server is allowed to fetch from, on top of the ImageKit host (always
    # allowed) — e.g. a CDN used for question-paper uploads. Leave empty to
    # rely on the public-IP-only check alone.
    OUTBOUND_FETCH_ALLOWED_HOSTS: str = ""

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() == "production"

    @property
    def outbound_fetch_allowed_hosts_list(self) -> list[str]:
        hosts = [h.strip().lower() for h in self.OUTBOUND_FETCH_ALLOWED_HOSTS.split(",") if h.strip()]
        endpoint = self.IMAGEKIT_URL_ENDPOINT.strip()
        if endpoint:
            from urllib.parse import urlparse

            host = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}").hostname
            if host:
                hosts.append(host.lower())
        return hosts

    @model_validator(mode="after")
    def _reject_weak_jwt_secret_in_production(self) -> "Settings":
        if self.is_production and (
            self.JWT_SECRET_KEY.strip().lower() in _WEAK_JWT_SECRETS
            or len(self.JWT_SECRET_KEY) < 32
        ):
            raise ValueError(
                "JWT_SECRET_KEY must be a strong random value (>= 32 chars) when ENV=production. "
                'Generate one with:  python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return self

    @model_validator(mode="after")
    def _reject_wildcard_cors_with_credentials(self) -> "Settings":
        # app/main.py mounts CORSMiddleware with allow_credentials=True. The
        # CORS spec forbids "*" with credentials, and Starlette would silently
        # reflect the Origin instead — turning it into an any-origin
        # credentialed policy. Fail fast instead.
        if "*" in self.cors_origins_list:
            raise ValueError(
                'CORS_ORIGINS must be an explicit list of origins — "*" is not allowed '
                "because the API sends credentials (cookies)."
            )
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    # Observability (see app/core/observability.py). PERF_LOG logs one line
    # per request (method path status duration_ms); SLOW_QUERY_MS logs any
    # MongoDB command slower than this. Both are cheap; turn PERF_LOG off in
    # steady-state prod if the log volume is unwanted.
    PERF_LOG_ENABLED: bool = True
    PERF_LOG_SLOW_MS: int = 500          # requests slower than this log at WARNING
    SLOW_QUERY_MS: int = 100             # 0 disables the Mongo command monitor

    # Concurrency (Phase 1). THREAD_POOL_WORKERS sizes the executor used by
    # asyncio.to_thread for the blocking SDK calls (Gemini/Claude/Playwright/
    # requests) — per worker process. MONGO_MAX_POOL_SIZE caps Motor's
    # connection pool per worker. gunicorn worker count is set in the
    # Dockerfile via WEB_CONCURRENCY, not here.
    THREAD_POOL_WORKERS: int = 24
    MONGO_MAX_POOL_SIZE: int = 50


settings = Settings()
