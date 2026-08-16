from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ------------------------------------------------------------------
    # OP25_DEBUG_EXPOSE — debugging aid, NOT a deployment mode.
    #
    # False (default): the op25 FastAPI control API (:8001, start/stop/
    # generate-config) and OP25's own HTTP terminal (:8081, live talkgroup
    # metadata) both bind 127.0.0.1. All three Client containers share the
    # host network namespace (network_mode: host), so edge-node still reaches
    # both over localhost with no functional change — nothing off-box can.
    # Neither surface has authentication, so this is the only thing closing
    # that hole.
    #
    # True: both bind 0.0.0.0 — reachable by anything on the node's LAN with
    # NO authentication (start/stop OP25, rewrite its config, raw terminal
    # access). Only ever set this for local development off a real deployed
    # node. A loud warning naming both ports is logged at startup whenever
    # this is true.
    # ------------------------------------------------------------------
    op25_debug_expose: bool = False

    class Config:
        env_file = ".env"


settings = Settings()


def bind_host() -> str:
    """Resolve the single bind address for both :8001 and :8081 from the flag."""
    return "0.0.0.0" if settings.op25_debug_expose else "127.0.0.1"
