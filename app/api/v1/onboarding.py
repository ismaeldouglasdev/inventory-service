"""Onboarding API — AI-powered product enrichment.

Endpoints:
  POST   /v1/onboarding/session              → create session
  GET    /v1/onboarding/session/{id}          → get session
  GET    /v1/onboarding/sessions              → list sessions
  POST   /v1/onboarding/session/{id}/images   → upload image(s)
  POST   /v1/onboarding/session/{id}/analyze  → run AI analysis
  POST   /v1/onboarding/session/{id}/apply    → apply to product
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from app.services.onboarding import OnboardingService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _get_service() -> OnboardingService:
    return OnboardingService()


@router.post("/session")
async def create_session(sku: str = Query(..., min_length=1, max_length=64)) -> dict[str, Any]:
    """Create a new onboarding session for a product SKU."""
    svc = _get_service()
    return await svc.create_session(sku)


@router.get("/session/{session_id}")
async def get_session(session_id: int) -> dict[str, Any]:
    """Get onboarding session details."""
    svc = _get_service()
    result = await svc.get_session(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return result


@router.get("/sessions")
async def list_sessions(
    sku: str | None = Query(None),
    status: str | None = Query(None, pattern=r"^(pending|processing|completed|failed)$"),
    limit: int = Query(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """List onboarding sessions."""
    svc = _get_service()
    return await svc.list_sessions(sku=sku, status=status, limit=limit)


@router.post("/session/{session_id}/images")
async def upload_images(
    session_id: int,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    """Upload one or more product images for analysis."""
    svc = _get_service()
    results = []

    for file in files:
        if not file.filename:
            continue
        if file.content_type and file.content_type not in ALLOWED_MIME:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file type {file.content_type}. Allowed: {ALLOWED_MIME}",
            )

        content = await file.read()
        if len(content) > MAX_IMAGE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File too large. Max {MAX_IMAGE_SIZE // (1024*1024)} MB.",
            )

        try:
            result = await svc.upload_image(session_id, file.filename, content)
            results.append(result)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    return {"uploaded": len(results), "images": results}


@router.post("/session/{session_id}/analyze")
async def analyze_session(session_id: int) -> dict[str, Any]:
    """Run AI analysis on uploaded images."""
    svc = _get_service()
    if not svc.enabled:
        logger.info("AI not configured — using fallback analysis")
    try:
        result = await svc.analyze(session_id)
        return {"status": "completed", **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Analysis failed for session %d", session_id)
        raise HTTPException(status_code=502, detail=f"AI analysis failed: {exc}")


@router.post("/session/{session_id}/apply")
async def apply_session(session_id: int) -> dict[str, Any]:
    """Apply AI-extracted attributes to the store product."""
    svc = _get_service()
    try:
        return await svc.apply_to_product(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
