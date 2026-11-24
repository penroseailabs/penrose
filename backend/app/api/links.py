"""Link previews for the chat's link cards (app/linkpreview.py): what a page says about itself, read here because
the app's windows cannot read other sites. Nothing is stored here: the answer carries a Cache-Control header, so the
window (or, for the phone app, its own cache) keeps it for a week, and a link with nothing to show is asked again after
five minutes (the site may only have been busy)."""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .. import linkpreview

router = APIRouter(prefix="/api")

WEEK = 7 * 24 * 3600
RETRY = 5 * 60


@router.get("/link-preview")
async def link_preview(url: str = Query(max_length=2048)):
    try:
        found = await linkpreview.preview(url)
    except linkpreview.Refused:
        return JSONResponse({"detail": "No preview"}, 404, headers={"Cache-Control": f"private, max-age={RETRY}"})
    return JSONResponse(found, headers={"Cache-Control": f"private, max-age={WEEK}"})
