from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from internal import decoder_control
from internal.logger import create_logger

LOGGER = create_logger(__name__)


class StartBody(BaseModel):
    mode: str  # adsb | ais


class StopBody(BaseModel):
    mode: Optional[str] = None  # omit to stop every decoder


class ApplyBody(BaseModel):
    priority: List[str]  # ordered, e.g. ["adsb", "ais"]


def create_secondary_router():
    router = APIRouter()

    @router.post("/apply")
    async def apply(body: ApplyBody):
        try:
            live = decoder_control.apply(body.priority)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"running": live}

    @router.post("/start")
    async def start(body: StartBody):
        try:
            ok = decoder_control.start(body.mode)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            raise HTTPException(status_code=409, detail="No free SDR for this decoder")
        return {"status": f"secondary SDR started ({body.mode})"}

    @router.post("/stop")
    async def stop(body: Optional[StopBody] = None):
        decoder_control.stop(body.mode if body else None)
        return {"status": "secondary SDR stopped"}

    @router.get("/status")
    async def get_status():
        return decoder_control.status()

    @router.get("/data")
    async def get_data():
        return decoder_control.data()

    return router
