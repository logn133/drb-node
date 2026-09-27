from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from internal import decoder_control
from internal.logger import create_logger

LOGGER = create_logger(__name__)


class StartBody(BaseModel):
    mode: str  # adsb | ais


def create_secondary_router():
    router = APIRouter()

    @router.post("/start")
    async def start(body: StartBody):
        try:
            ok = decoder_control.start(body.mode)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to start secondary SDR decoder")
        return {"status": f"secondary SDR started ({body.mode})"}

    @router.post("/stop")
    async def stop():
        decoder_control.stop()
        return {"status": "secondary SDR stopped"}

    @router.get("/status")
    async def get_status():
        return decoder_control.status()

    @router.get("/data")
    async def get_data():
        return decoder_control.data()

    return router
