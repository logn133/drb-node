from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["ui"])

_TEMPLATE = Path(__file__).parent.parent / "templates" / "index.html"
_SCANNER_TEMPLATE = Path(__file__).parent.parent / "templates" / "scanner.html"


@router.get("/", response_class=HTMLResponse)
async def index():
    return _TEMPLATE.read_text()


@router.get("/scanner", response_class=HTMLResponse)
async def scanner():
    return _SCANNER_TEMPLATE.read_text()
