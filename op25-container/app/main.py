from fastapi import FastAPI
from contextlib import asynccontextmanager
import os
import routers.op25_controller as op25_controller
from internal.logger import create_logger
from internal.liquidsoap_config_utils import generate_liquid_script
from models import IcecastConfig
from config import settings, bind_host

LOGGER = create_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.op25_debug_expose:
        LOGGER.warning(
            "OP25_DEBUG_EXPOSE=true — the op25 control API (:8001) and OP25's "
            "HTTP terminal (:8081) are bound to 0.0.0.0 and reachable by "
            "ANYTHING on this node's LAN with NO authentication. This is a "
            "debugging aid only; do not leave it set on a deployed node."
        )
    try:
        config = IcecastConfig(
            icecast_host=os.getenv("ICECAST_HOST", "localhost"),
            icecast_port=int(os.getenv("ICECAST_PORT", "8000")),
            icecast_mountpoint=os.getenv("ICECAST_MOUNT", "/radio"),
            icecast_password=os.getenv("ICECAST_SOURCE_PASSWORD", ""),
        )
        generate_liquid_script(config)
        LOGGER.info("op25.liq generated from environment variables.")
    except Exception as e:
        LOGGER.error(f"Failed to generate op25.liq: {e}")
    yield


app = FastAPI(lifespan=lifespan)

app.include_router(op25_controller.create_op25_router(), prefix="/op25")


if __name__ == "__main__":
    # Launched directly (see Dockerfile CMD) instead of via `uvicorn main:app
    # --host ...` so the bind address is driven by OP25_DEBUG_EXPOSE (config.py)
    # rather than a value baked into the image at build time.
    import uvicorn

    uvicorn.run("main:app", host=bind_host(), port=8001, reload=True)
