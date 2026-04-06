from fastapi import FastAPI
from contextlib import asynccontextmanager
import os
import routers.op25_controller as op25_controller
from internal.logger import create_logger
from internal.liquidsoap_config_utils import generate_liquid_script
from models import IcecastConfig

LOGGER = create_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        config = IcecastConfig(
            icecast_host=os.getenv("ICECAST_HOST", "localhost"),
            icecast_port=int(os.getenv("ICECAST_PORT", "8000")),
            icecast_mountpoint=os.getenv("ICECAST_MOUNT", "/radio"),
            icecast_password=os.getenv("ICECAST_SOURCE_PASSWORD", "hackme"),
        )
        generate_liquid_script(config)
        LOGGER.info("op25.liq generated from environment variables.")
    except Exception as e:
        LOGGER.error(f"Failed to generate op25.liq: {e}")
    yield


app = FastAPI(lifespan=lifespan)

app.include_router(op25_controller.create_op25_router(), prefix="/op25")
