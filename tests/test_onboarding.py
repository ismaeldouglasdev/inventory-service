"""Testes do Onboarding Service — session, image upload, AI analysis, apply.

Cobre:
  - Criacao e consulta de sessao
  - Upload de imagens
  - Analise com fallback (sem AI configurada)
  - Aplicacao de atributos ao produto
  - Parsing de resposta do LLM
"""

from __future__ import annotations

import json

import pytest

from app.models.onboarding import OnboardingSession, OnboardingImage
from app.models.store_product import StoreProduct
from app.services.onboarding import OnboardingService
from app.config import settings


class TestOnboardingService:
    """Testa o onboarding service com SQLite em memoria."""

    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from app.database import Base, engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    @pytest.fixture
    def svc(self):
        settings.ai_api_url = ""
        settings.ai_api_key = ""
        return OnboardingService()

    # ── Session ─────────────────────────────────────────────────────

    async def test_create_session(self, svc):
        result = await svc.create_session("SKU-001")
        assert result["sku"] == "SKU-001"
        assert result["status"] == "pending"
        assert result["id"] > 0
        assert result["images_processed"] == 0

    async def test_get_session(self, svc):
        created = await svc.create_session("SKU-002")
        fetched = await svc.get_session(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["sku"] == "SKU-002"

    async def test_get_session_not_found(self, svc):
        result = await svc.get_session(9999)
        assert result is None

    async def test_list_sessions(self, svc):
        await svc.create_session("SKU-A")
        await svc.create_session("SKU-B")
        results = await svc.list_sessions()
        assert len(results) == 2

    async def test_list_sessions_filter_sku(self, svc):
        await svc.create_session("SKU-A")
        await svc.create_session("SKU-B")
        results = await svc.list_sessions(sku="SKU-A")
        assert len(results) == 1
        assert results[0]["sku"] == "SKU-A"

    # ── Image upload ────────────────────────────────────────────────

    async def test_upload_image(self, svc):
        session = await svc.create_session("SKU-IMG")
        result = await svc.upload_image(session["id"], "test.jpg", b"fake-image-data")
        assert result["session_id"] == session["id"]
        assert result["filename"].endswith(".jpg")

        # Verify session count incremented
        updated = await svc.get_session(session["id"])
        assert updated["images_processed"] == 1

    async def test_upload_image_session_not_found(self, svc):
        with pytest.raises(ValueError, match="not found"):
            await svc.upload_image(9999, "test.jpg", b"data")

    # ── AI Analysis (fallback) ──────────────────────────────────────

    async def test_analyze_without_ai(self, svc):
        session = await svc.create_session("SKU-ANL")
        await svc.upload_image(session["id"], "img.jpg", b"fake")

        result = await svc.analyze(session["id"])
        assert result["category"] == "geral"  # fallback
        assert result["confidence"] == 0.0

        # Session marked completed
        updated = await svc.get_session(session["id"])
        assert updated["status"] == "completed"
        assert updated["result"] is not None

    async def test_analyze_no_images(self, svc):
        session = await svc.create_session("SKU-NOIMG")
        with pytest.raises(ValueError, match="No images"):
            await svc.analyze(session["id"])

    # ── Apply to product ────────────────────────────────────────────

    async def test_apply_to_product(self, svc):
        # Seed product
        from app.database import async_session_factory
        from datetime import datetime, timezone

        async with async_session_factory() as s:
            p = StoreProduct(
                ospos_id=100, sku="SKU-APP", name="Old Name",
                description="", price=10.0, category="old", stock=5,
                store_visible=False,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            s.add(p)
            await s.commit()

        # Create session with result
        session = await svc.create_session("SKU-APP")
        async with async_session_factory() as s:
            from sqlalchemy import select
            obj = (await s.execute(
                select(OnboardingSession).where(OnboardingSession.id == session["id"])
            )).scalar_one()
            obj.status = "completed"
            obj.result = json.dumps({
                "category": "eletronicos",
                "suggested_name": "Produto Novo",
                "description": "Descricao atualizada",
            })
            obj.updated_at = datetime.now(timezone.utc)
            await s.commit()

        result = await svc.apply_to_product(session["id"])
        assert result["sku"] == "SKU-APP"
        assert "suggested_name" in result["applied_fields"]
        assert "description" in result["applied_fields"]
        assert "category" in result["applied_fields"]

        # Verify product updated
        async with async_session_factory() as s:
            from sqlalchemy import select
            p = (await s.execute(
                select(StoreProduct).where(StoreProduct.sku == "SKU-APP")
            )).scalar_one()
            assert p.name == "Produto Novo"
            assert p.description == "Descricao atualizada"
            assert p.category == "eletronicos"

    async def test_apply_not_completed(self, svc):
        session = await svc.create_session("SKU-NOAPP")
        with pytest.raises(ValueError, match="not completed"):
            await svc.apply_to_product(session["id"])

    # ── LLM response parsing ───────────────────────────────────────

    def test_parse_llm_response_plain_json(self, svc):
        raw = '{"category": "teste", "brand": "marca"}'
        result = svc._parse_llm_response(raw)
        assert result["category"] == "teste"
        assert result["brand"] == "marca"

    def test_parse_llm_response_markdown(self, svc):
        raw = "```json\n{\"category\": \"teste\"}\n```"
        result = svc._parse_llm_response(raw)
        assert result["category"] == "teste"

    def test_parse_llm_response_invalid(self, svc):
        raw = "not json at all"
        result = svc._parse_llm_response(raw)
        assert result["category"] == "geral"  # fallback
