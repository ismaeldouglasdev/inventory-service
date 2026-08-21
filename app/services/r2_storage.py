"""Cloudflare R2 (S3-compatible) image storage with hard free-tier guardrails.

Falls back to local filesystem when R2 env vars are not configured.
Guardrails prevent exceeding the R2 free tier (10GB / 1M Class A / 10M Class B).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_s3_client = None
_r2_config: dict = {}
_usage_lock = threading.Lock()

USAGE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "r2_usage.json"


def _get_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data"


# ── Usage tracking ───────────────────────────────────────────────────

def _load_usage() -> dict:
    if USAGE_FILE.exists():
        try:
            return json.loads(USAGE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "storage_bytes": 0,
        "class_a_ops": 0,
        "class_b_ops": 0,
        "month": datetime.now(timezone.utc).strftime("%Y-%m"),
    }


def _save_usage(usage: dict) -> None:
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(usage, indent=2))


def _reset_if_new_month(usage: dict) -> dict:
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if usage.get("month") != current_month:
        logger.info(
            "R2 guardrail: new month detected (%s → %s), resetting operation counters",
            usage.get("month"),
            current_month,
        )
        usage["class_a_ops"] = 0
        usage["class_b_ops"] = 0
        usage["month"] = current_month
    return usage


def _check_storage_limit(new_bytes: int) -> None:
    from app.config import settings

    with _usage_lock:
        usage = _load_usage()
        usage = _reset_if_new_month(usage)

        projected = usage["storage_bytes"] + new_bytes
        limit = settings.r2_max_storage_bytes
        if projected > limit:
            current_gb = usage["storage_bytes"] / (1024**3)
            projected_gb = projected / (1024**3)
            limit_gb = limit / (1024**3)
            raise RuntimeError(
                f"R2 STORAGE LIMIT BLOCKED: would use {projected_gb:.2f}GB "
                f"(current: {current_gb:.2f}GB, limit: {limit_gb:.1f}GB). "
                f"Upload refused. Free tier: 10GB/month."
            )
        _save_usage(usage)


def _record_class_a_ops(count: int = 1) -> None:
    from app.config import settings

    with _usage_lock:
        usage = _load_usage()
        usage = _reset_if_new_month(usage)

        usage["class_a_ops"] += count
        if usage["class_a_ops"] > settings.r2_max_class_a_ops:
            raise RuntimeError(
                f"R2 CLASS A OPS LIMIT BLOCKED: {usage['class_a_ops']} ops "
                f"(limit: {settings.r2_max_class_a_ops}). "
                f"Write/delete refused. Free tier: 1M Class A/month."
            )
        _save_usage(usage)


def _record_class_b_ops(count: int = 1) -> None:
    from app.config import settings

    with _usage_lock:
        usage = _load_usage()
        usage = _reset_if_new_month(usage)

        usage["class_b_ops"] += count
        if usage["class_b_ops"] > settings.r2_max_class_b_ops:
            raise RuntimeError(
                f"R2 CLASS B OPS LIMIT BLOCKED: {usage['class_b_ops']} ops "
                f"(limit: {settings.r2_max_class_b_ops}). "
                f"Read refused. Free tier: 10M Class B/month."
            )
        _save_usage(usage)


def _record_storage_bytes(byte_count: int) -> None:
    with _usage_lock:
        usage = _load_usage()
        usage = _reset_if_new_month(usage)
        usage["storage_bytes"] = max(0, usage["storage_bytes"] + byte_count)
        _save_usage(usage)


def get_usage_stats() -> dict:
    from app.config import settings

    with _usage_lock:
        usage = _load_usage()
        usage = _reset_if_new_month(usage)

    return {
        "storage_bytes": usage["storage_bytes"],
        "storage_gb": round(usage["storage_bytes"] / (1024**3), 3),
        "max_storage_gb": round(settings.r2_max_storage_bytes / (1024**3), 1),
        "class_a_ops": usage["class_a_ops"],
        "max_class_a_ops": settings.r2_max_class_a_ops,
        "class_b_ops": usage["class_b_ops"],
        "max_class_b_ops": settings.r2_max_class_b_ops,
        "month": usage.get("month"),
    }


def _init_r2() -> bool:
    global _s3_client, _r2_config

    if _s3_client is not None:
        return True
    if _s3_client is False:
        return False

    account_id = os.environ.get("R2_ACCOUNT_ID", "")
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    bucket = os.environ.get("R2_BUCKET_NAME", "")
    public_url = os.environ.get("R2_PUBLIC_URL", "")
    s3_endpoint = os.environ.get("R2_S3_ENDPOINT", "")

    if not all([account_id, access_key, secret_key, bucket]):
        logger.info("R2 not configured — using local filesystem for images")
        _s3_client = False
        return False

    try:
        import boto3

        endpoint = s3_endpoint or f"https://{account_id}.r2.cloudflarestorage.com"

        _s3_client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )
        _r2_config = {
            "bucket": bucket,
            "public_url": public_url or f"https://pub-{account_id}.r2.dev/{bucket}",
        }
        logger.info("R2 configured: bucket=%s endpoint=%s", bucket, endpoint)

        _sync_storage_bytes_from_bucket()

        return True
    except Exception as exc:
        logger.warning("Failed to init R2: %s — falling back to local", exc)
        _s3_client = False
        return False


def _sync_storage_bytes_from_bucket() -> None:
    try:
        resp = _s3_client.list_objects_v2(Bucket=_r2_config["bucket"])
        total = 0
        for obj in resp.get("Contents", []):
            total += obj.get("Size", 0)
        with _usage_lock:
            usage = _load_usage()
            usage = _reset_if_new_month(usage)
            old = usage["storage_bytes"]
            usage["storage_bytes"] = total
            _save_usage(usage)
        if total != old:
            logger.info(
                "R2 guardrail: synced storage from bucket: %.2fGB (was %.2fGB)",
                total / (1024**3),
                old / (1024**3),
            )
    except Exception as exc:
        logger.warning("R2 guardrail: failed to sync storage from bucket: %s", exc)


def is_r2_configured() -> bool:
    return _init_r2()


def upload(key: str, data: bytes, content_type: str = "image/png") -> str:
    if not _init_r2():
        local_path = _get_data_dir() / "images" / key
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        return key

    _check_storage_limit(len(data))
    _record_class_a_ops(1)

    _s3_client.put_object(
        Bucket=_r2_config["bucket"],
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    _record_storage_bytes(len(data))

    logger.debug("R2 uploaded: %s (%d bytes)", key, len(data))
    return key


def download(key: str) -> Optional[bytes]:
    if not _init_r2():
        local_path = _get_data_dir() / "images" / key
        if local_path.exists():
            return local_path.read_bytes()
        return None

    _record_class_b_ops(1)

    try:
        resp = _s3_client.get_object(Bucket=_r2_config["bucket"], Key=key)
        return resp["Body"].read()
    except Exception as exc:
        logger.warning("R2 download failed for %s: %s", key, exc)
        local_path = _get_data_dir() / "images" / key
        if local_path.exists():
            return local_path.read_bytes()
        return None


def delete(key: str) -> bool:
    if not _init_r2():
        local_path = _get_data_dir() / "images" / key
        if local_path.exists():
            local_path.unlink()
            return True
        return False

    _record_class_a_ops(1)

    try:
        try:
            head = _s3_client.head_object(Bucket=_r2_config["bucket"], Key=key)
            size = head.get("ContentLength", 0)
        except Exception:
            size = 0

        _s3_client.delete_object(Bucket=_r2_config["bucket"], Key=key)

        if size > 0:
            _record_storage_bytes(-size)
        return True
    except Exception as exc:
        logger.warning("R2 delete failed for %s: %s", key, exc)
        return False


def exists(key: str) -> bool:
    if not _init_r2():
        return (_get_data_dir() / "images" / key).exists()

    _record_class_b_ops(1)

    try:
        _s3_client.head_object(Bucket=_r2_config["bucket"], Key=key)
        return True
    except Exception:
        return False


def get_public_url(key: str) -> str:
    if not _init_r2():
        return f"/v1/store/images/{key}"
    return f"{_r2_config['public_url']}/{key}"


def get_content_type(key: str) -> str:
    ext = Path(key).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")
