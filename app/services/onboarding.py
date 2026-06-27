"""AI-powered product onboarding — image analysis + attribute extraction.

Flow:
  1. Create session for a SKU
  2. Upload product photos (1-4 images)
  3. Run AI analysis: classifies category, extracts brand/description/attributes
  4. Review & apply extracted data to the store product

Uses any OpenAI-compatible API (configurable via settings).
Default model: gpt-4o (vision).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from app.config import settings
from app.database import async_session_factory
from app.models.onboarding import OnboardingImage, OnboardingSession
from app.models.store_product import StoreProduct

logger = logging.getLogger(__name__)

# ── Image storage ─────────────────────────────────────────────────────────

ONBOARDING_IMAGE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "onboarding"
)

# ── LLM prompt ───────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """You are a product classification AI for a retail store.
Analyze the product image(s) and extract the following attributes as JSON.
Return ONLY valid JSON, no markdown, no explanation.

{
  "category": "best matching category name in Brazilian Portuguese (max 3 words)",
  "brand": "brand name if visible on packaging/label, or null",
  "suggested_name": "concise product name in Brazilian Portuguese (max 8 words)",
  "description": "brief description in Brazilian Portuguese (max 3 sentences)",
  "attributes": {
    "color": "dominant color or null",
    "material": "material if visible or null",
    "size": "size/volume/weight if visible or null",
    "unit": "unidade|kg|g|L|ml|pc|par|caixa or null"
  },
  "confidence": 0.0-1.0
}"""


# ── Service ──────────────────────────────────────────────────────────────

class OnboardingService:
    """AI product onboarding service.

    Uses a vision-capable LLM to classify products from photos.
    Falls back gracefully if no AI endpoint is configured.
    """

    def __init__(self) -> None:
        self._api_url = settings.ai_api_url.rstrip("/") if settings.ai_api_url else ""
        self._api_key = settings.ai_api_key
        self._model = settings.ai_model

    @property
    def enabled(self) -> bool:
        return bool(self._api_url and self._api_key)

    # ── Session management ──────────────────────────────────────────────

    async def create_session(self, sku: str) -> dict[str, Any]:
        """Create a new onboarding session for a product SKU."""
        async with async_session_factory() as session:
            now = datetime.now(timezone.utc)
            obj = OnboardingSession(
                sku=sku,
                status="pending",
                images_processed=0,
                created_at=now,
                updated_at=now,
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            logger.info("Onboarding session created: id=%d sku=%s", obj.id, sku)
            return self._session_to_dict(obj)

    async def get_session(self, session_id: int) -> dict[str, Any] | None:
        """Get session details by ID."""
        async with async_session_factory() as s:
            from sqlalchemy import select
            result = await s.execute(
                select(OnboardingSession).where(OnboardingSession.id == session_id)
            )
            obj = result.scalar_one_or_none()
            return self._session_to_dict(obj) if obj else None

    async def list_sessions(
        self, sku: str | None = None, status: str | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List onboarding sessions with optional filters."""
        async with async_session_factory() as s:
            from sqlalchemy import select
            q = select(OnboardingSession)
            if sku:
                q = q.where(OnboardingSession.sku == sku)
            if status:
                q = q.where(OnboardingSession.status == status)
            q = q.order_by(OnboardingSession.created_at.desc()).limit(limit)
            result = await s.execute(q)
            return [self._session_to_dict(obj) for obj in result.scalars().all()]

    # ── Image upload ────────────────────────────────────────────────────

    async def upload_image(
        self, session_id: int, filename: str, content: bytes,
    ) -> dict[str, Any]:
        """Upload a product image for an onboarding session."""
        ONBOARDING_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

        ext = Path(filename).suffix.lower() or ".jpg"
        stored_name = f"onb_{session_id}_{uuid4().hex[:8]}{ext}"
        filepath = ONBOARDING_IMAGE_DIR / stored_name

        with open(filepath, "wb") as f:
            f.write(content)

        async with async_session_factory() as s:
            from sqlalchemy import select
            result = await s.execute(
                select(OnboardingSession).where(OnboardingSession.id == session_id)
            )
            session_obj = result.scalar_one_or_none()
            if not session_obj:
                filepath.unlink(missing_ok=True)
                raise ValueError(f"Session {session_id} not found")

            img = OnboardingImage(
                session_id=session_id,
                filename=stored_name,
                storage_path=str(filepath),
                mime_type=f"image/{ext.lstrip('.')}",
                created_at=datetime.now(timezone.utc),
            )
            s.add(img)
            session_obj.images_processed += 1
            session_obj.updated_at = datetime.now(timezone.utc)
            await s.commit()
            await s.refresh(img)

            logger.info("Image uploaded: session=%d file=%s", session_id, stored_name)
            return {"id": img.id, "filename": stored_name, "session_id": session_id}

    # ── AI Analysis ─────────────────────────────────────────────────────

    async def analyze(self, session_id: int) -> dict[str, Any]:
        """Run AI analysis on all images in a session.

        Sends images to the vision LLM and stores the result.
        """
        async with async_session_factory() as s:
            from sqlalchemy import select
            result = await s.execute(
                select(OnboardingSession).where(OnboardingSession.id == session_id)
            )
            session_obj = result.scalar_one_or_none()
            if not session_obj:
                raise ValueError(f"Session {session_id} not found")

            # Fetch images
            img_result = await s.execute(
                select(OnboardingImage).where(
                    OnboardingImage.session_id == session_id
                )
            )
            images = list(img_result.scalars().all())

            if not images:
                raise ValueError("No images uploaded for this session")

            session_obj.status = "processing"
            session_obj.updated_at = datetime.now(timezone.utc)
            await s.commit()

        # Run analysis
        try:
            if self.enabled:
                result_data = await self._call_llm(images)
            else:
                result_data = self._fallback_analysis()

            # Store result
            async with async_session_factory() as s:
                result = await s.execute(
                    select(OnboardingSession).where(
                        OnboardingSession.id == session_id
                    )
                )
                session_obj = result.scalar_one()
                session_obj.status = "completed"
                session_obj.result = json.dumps(result_data)
                session_obj.updated_at = datetime.now(timezone.utc)
                await s.commit()

            return result_data

        except Exception as exc:
            logger.exception("AI analysis failed for session %d", session_id)
            async with async_session_factory() as s:
                result = await s.execute(
                    select(OnboardingSession).where(
                        OnboardingSession.id == session_id
                    )
                )
                session_obj = result.scalar_one()
                session_obj.status = "failed"
                session_obj.updated_at = datetime.now(timezone.utc)
                await s.commit()
            raise

    # ── Apply to product ────────────────────────────────────────────────

    async def apply_to_product(self, session_id: int) -> dict[str, Any]:
        """Apply the AI-extracted attributes to the store product."""
        async with async_session_factory() as s:
            from sqlalchemy import select
            result = await s.execute(
                select(OnboardingSession).where(OnboardingSession.id == session_id)
            )
            session_obj = result.scalar_one_or_none()
            if not session_obj or session_obj.status != "completed":
                raise ValueError(
                    f"Session {session_id} not completed (status={getattr(session_obj, 'status', 'not_found')})"
                )

            result_data = json.loads(session_obj.result) if session_obj.result else {}

            # Find the store product by SKU
            prod_result = await s.execute(
                select(StoreProduct).where(StoreProduct.sku == session_obj.sku)
            )
            product = prod_result.scalar_one_or_none()
            if not product:
                raise ValueError(f"Product SKU {session_obj.sku!r} not found in store")

            # Apply changes
            now = datetime.now(timezone.utc)
            if result_data.get("suggested_name"):
                product.name = result_data["suggested_name"]
            if result_data.get("description"):
                product.description = result_data["description"]
            if result_data.get("category"):
                product.category = result_data["category"]
            product.updated_at = now

            await s.commit()
            logger.info(
                "Applied onboarding session %d to product SKU %s",
                session_id, session_obj.sku,
            )
            return {
                "session_id": session_id,
                "sku": session_obj.sku,
                "applied_fields": [
                    k for k in ("suggested_name", "description", "category")
                    if result_data.get(k)
                ],
            }

    # ── LLM call ────────────────────────────────────────────────────────

    async def _call_llm(self, images: list[OnboardingImage]) -> dict[str, Any]:
        """Call the vision LLM with product images."""
        max_images = settings.ai_max_images
        selected = images[:max_images]

        content: list[dict[str, Any]] = [
            {"type": "text", "text": ANALYSIS_PROMPT}
        ]

        for img in selected:
            with open(img.storage_path, "rb") as f:
                b64 = __import__("base64").b64encode(f.read()).decode()
            ext = Path(img.filename).suffix.lstrip(".") or "jpeg"
            if ext == "jpg":
                ext = "jpeg"
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/{ext};base64,{b64}"},
            })

        body = {
            "model": self._model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 1024,
            "temperature": 0.1,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        url = f"{self._api_url}/v1/chat/completions"
        logger.info("Calling LLM: model=%s images=%d", self._model, len(selected))

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        raw = data["choices"][0]["message"]["content"]
        return self._parse_llm_response(raw)

    def _parse_llm_response(self, raw: str) -> dict[str, Any]:
        """Parse LLM response, extracting JSON from possible markdown."""
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Failed to parse LLM response as JSON: %.200s", raw)
            return self._fallback_analysis()

    def _fallback_analysis(self) -> dict[str, Any]:
        """Return stub data when AI is unavailable."""
        return {
            "category": "geral",
            "brand": None,
            "suggested_name": None,
            "description": "Produto importado do sistema de PDV.",
            "attributes": {"color": None, "material": None, "size": None, "unit": None},
            "confidence": 0.0,
        }

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _session_to_dict(obj: OnboardingSession) -> dict[str, Any]:
        return {
            "id": obj.id,
            "sku": obj.sku,
            "status": obj.status,
            "images_processed": obj.images_processed,
            "result": json.loads(obj.result) if obj.result else None,
            "created_at": obj.created_at.isoformat() if obj.created_at else None,
            "updated_at": obj.updated_at.isoformat() if obj.updated_at else None,
        }
