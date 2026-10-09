import json
from importlib.resources import files

from fastapi import APIRouter, Depends

from ..deps import current_user

router = APIRouter(prefix="/user", tags=["user"])


# Stub until the crawl pipeline exists: same shape as spec §5.3 so the frontend can build now.
@router.get("/projects/{project_id}/moves")
def moves(project_id: int, _=Depends(current_user)):
    return json.loads(files("competitor_moves.api").joinpath("fixtures/moves.json").read_text())
