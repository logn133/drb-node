from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["ui"])

_TEMPLATE = Path(__file__).parent.parent / "templates" / "index.html"


@router.get("/", response_class=HTMLResponse)
async def index():
    return _TEMPLATE.read_text()
