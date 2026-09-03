"""Nota fiscal parsing via Gemini Vision.

Sends the photos of a supplier note (NF-e DANFE or plain paper invoice)
to the Gemini API and gets structured JSON back: supplier header, line
items (supplier ref / EAN / name / qty / unit price / discount / line
total), grand total and payment terms.

The provider is isolated here so it can be swapped (OpenAI, local OCR…)
without touching the router or the matcher.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_PROMPT = """\
Você extrai dados de notas de compra/fornecimento brasileiras (NF-e DANFE \
impressa ou nota simples de papel). Você recebe uma ou mais fotos das \
páginas da MESMA nota.

Devolve EXCLUSIVAMENTE um JSON válido neste formato:

{
  "supplier": {"name": string|null, "cnpj": string|null, "phone": string|null, "email": string|null},
  "note_number": string|null,
  "date": "YYYY-MM-DD"|null,
  "payment_terms": string|null,
  "total": number|null,
  "items": [
    {
      "ref": string|null,
      "ean": string|null,
      "name": string,
      "qty": number,
      "unit_price": number,
      "discount_percent": number|null,
      "discount_value": number|null,
      "line_total": number|null
    }
  ]
}

Regras:
- Números em formato brasileiro (1.234,56) viram número JSON (1234.56).
- "ref" é o código interno do produto NA NOTA (coluna Código/Cod./Ref.). \
Se houver código de barras EAN/GTIN separado, coloque em "ean".
- "name" é a descrição do produto exatamente como está na nota.
- qty = quantidade; unit_price = valor unitário; discount_percent OU \
discount_value = desconto da linha (um dos dois); line_total = total da linha.
- Ignore linhas riscadas/canceladas/duplicadas. NÃO invente itens.
- Se um campo não estiver visível na foto, use null.
- O total geral e as condições/prazos de pagamento ficam normalmente no fim \
da última página.
"""


class NotaParseError(Exception):
    """Raised when the AI cannot be reached or returns unusable output."""


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(str(value).replace(".", "", str(value).count(".") - 1).replace(",", ".")), 3) if isinstance(value, str) and "," in value else round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_parsed(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce the model output into strict types the app can rely on."""
    supplier = raw.get("supplier") or {}
    items_out: list[dict[str, Any]] = []
    for item in raw.get("items") or []:
        name = _coerce_str(item.get("name"))
        if not name:
            continue
        qty = _coerce_float(item.get("qty")) or 0.0
        if qty <= 0:
            continue
        items_out.append(
            {
                "ref": _coerce_str(item.get("ref")),
                "ean": _coerce_str(item.get("ean")),
                "name": name[:255],
                "qty": qty,
                "unit_price": _coerce_float(item.get("unit_price")) or 0.0,
                "discount_percent": _coerce_float(item.get("discount_percent")),
                "discount_value": _coerce_float(item.get("discount_value")),
                "line_total": _coerce_float(item.get("line_total")),
            }
        )
    date_raw = _coerce_str(raw.get("date"))
    if date_raw:
        m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", date_raw)
        if m:
            date_raw = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return {
        "supplier": {
            "name": _coerce_str(supplier.get("name")),
            "cnpj": _coerce_str(supplier.get("cnpj")),
            "phone": _coerce_str(supplier.get("phone")),
            "email": _coerce_str(supplier.get("email")),
        },
        "note_number": _coerce_str(raw.get("note_number")),
        "date": date_raw,
        "payment_terms": _coerce_str(raw.get("payment_terms")),
        "total": _coerce_float(raw.get("total")),
        "items": items_out,
    }


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise NotaParseError("Resposta da IA sem JSON reconhecível")
    import json

    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise NotaParseError(f"JSON inválido da IA: {exc}") from exc


async def parse_note_images(images: list[tuple[bytes, str]]) -> dict[str, Any]:
    """Parse supplier-note photos into structured data.

    ``images`` is a list of ``(bytes, mime_type)`` tuples, one per page.
    """
    api_key = settings.gemini_api_key
    if not api_key:
        raise NotaParseError(
            "GEMINI_API_KEY não configurada no .env do inventory-service"
        )

    parts: list[dict[str, Any]] = [{"text": _PROMPT}]
    for content, mime in images[: settings.gemini_max_images]:
        parts.append(
            {
                "inline_data": {
                    "mime_type": mime or "image/jpeg",
                    "data": base64.b64encode(content).decode("ascii"),
                }
            }
        )

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }

    url = _GEMINI_URL.format(model=settings.gemini_model)

    def _call() -> dict[str, Any]:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                url,
                json=payload,
                headers={"x-goog-api-key": api_key},
            )
            if resp.status_code != 200:
                detail = resp.text[:300]
                logger.error("Gemini HTTP %s: %s", resp.status_code, detail)
                raise NotaParseError(f"Erro da IA (HTTP {resp.status_code})")
            data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise NotaParseError("Resposta da IA vazia/bloqueada") from exc

    raw_text = await asyncio.to_thread(_call)
    parsed = _normalize_parsed(_extract_json(raw_text))
    logger.info(
        "Nota parseada: %s itens, fornecedor=%s, total=%s",
        len(parsed["items"]),
        parsed["supplier"]["name"],
        parsed["total"],
    )
    return parsed
