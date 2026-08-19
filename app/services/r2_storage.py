"""Cloudflare R2 (S3-compatible) image storage.

Falls back to local filesystem when R2 env vars are not configured.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy-loaded boto3 client
_s3_client = None
_r2_config: dict = {}


def _get_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data"


def _init_r2() -> bool:
    """Initialize R2 S3 client from env vars. Returns True if configured."""
    global _s3_client, _r2_config

    if _s3_client is not None:
        return True
    if _s3_client is False:  # already tried and failed
        return False

    import os
    account_id = os.environ.get("R2_ACCOUNT_ID", "")
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    bucket = os.environ.get("R2_BUCKET_NAME", "")
    public_url = os.environ.get("R2_PUBLIC_URL", "")

    if not all([account_id, access_key, secret_key, bucket]):
        logger.info("R2 not configured — using local filesystem for images")
        _s3_client = False
        return False

    try:
        import boto3
        _s3_client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )
        _r2_config = {
            "bucket": bucket,
            "public_url": public_url or f"https://pub-{account_id}.r2.dev/{bucket}",
        }
        logger.info("R2 configured: bucket=%s", bucket)
        return True
    except Exception as exc:
        logger.warning("Failed to init R2: %s — falling back to local", exc)
        _s3_client = False
        return False


def is_r2_configured() -> bool:
    """Check if R2 is available."""
    return _init_r2()


def upload(key: str, data: bytes, content_type: str = "image/png") -> str:
    """Upload image to R2. Returns the key."""
    if not _init_r2():
        # Fallback: save to local filesystem
        local_path = _get_data_dir() / "images" / key
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        return key

    _s3_client.put_object(
        Bucket=_r2_config["bucket"],
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    logger.debug("R2 uploaded: %s (%d bytes)", key, len(data))
    return key


def download(key: str) -> Optional[bytes]:
    """Download image from R2. Returns bytes or None."""
    if not _init_r2():
        local_path = _get_data_dir() / "images" / key
        if local_path.exists():
            return local_path.read_bytes()
        return None

    try:
        resp = _s3_client.get_object(Bucket=_r2_config["bucket"], Key=key)
        return resp["Body"].read()
    except Exception as exc:
        logger.warning("R2 download failed for %s: %s", key, exc)
        # Fallback to local
        local_path = _get_data_dir() / "images" / key
        if local_path.exists():
            return local_path.read_bytes()
        return None


def delete(key: str) -> bool:
    """Delete image from R2. Returns True on success."""
    if not _init_r2():
        local_path = _get_data_dir() / "images" / key
        if local_path.exists():
            local_path.unlink()
            return True
        return False

    try:
        _s3_client.delete_object(Bucket=_r2_config["bucket"], Key=key)
        return True
    except Exception as exc:
        logger.warning("R2 delete failed for %s: %s", key, exc)
        return False


def exists(key: str) -> bool:
    """Check if image exists in R2."""
    if not _init_r2():
        return (_get_data_dir() / "images" / key).exists()

    try:
        _s3_client.head_object(Bucket=_r2_config["bucket"], Key=key)
        return True
    except Exception:
        return False


def get_public_url(key: str) -> str:
    """Get the public URL for an image."""
    if not _init_r2():
        return f"/v1/store/images/{key}"
    return f"{_r2_config['public_url']}/{key}"


def get_content_type(key: str) -> str:
    """Get MIME type from file extension."""
    ext = Path(key).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")
