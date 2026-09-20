from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Same rationale as op25-container's OP25_DEBUG_EXPOSE: both containers
    # share the host network namespace (network_mode: host), so edge-node
    # reaches this control API over localhost regardless of this flag. False
    # (default) binds 127.0.0.1; true exposes unauthenticated start/stop to
    # the node's LAN and should only ever be set for local development.
    secondary_sdr_debug_expose: bool = False

    class Config:
        env_file = ".env"


settings = Settings()


def bind_host() -> str:
    return "0.0.0.0" if settings.secondary_sdr_debug_expose else "127.0.0.1"
