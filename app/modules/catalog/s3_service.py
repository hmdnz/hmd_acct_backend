
import asyncio
import logging
import uuid

from app.core.s3_client import s3_client
from app.core.config import settings

logger = logging.getLogger(__name__)

PRESIGNED_URL_EXPIRY_SECONDS = 900
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_EXTENSIONS = {"jpeg", "png", "webp"}  # what content_type.split("/")[1] produces
MAX_FILES_PER_REQUEST = 10
S3_DELETE_BATCH_SIZE = 1000  # S3's limit per delete_objects call


def generate_presigned_upload_url(content_type: str, folder: str = "products") -> dict:
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValueError(f"Unsupported image type: {content_type}. Use JPEG, PNG, or WEBP.")

    extension = content_type.split("/")[1]
    key = f"{folder}/{uuid.uuid4()}.{extension}"

    upload_url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.S3_BUCKET_NAME, "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
    )
    return {"upload_url": upload_url, "s3_key": key}


def generate_presigned_upload_urls(content_types: list[str], folder: str = "products") -> list[dict]:
    if len(content_types) > MAX_FILES_PER_REQUEST:
        raise ValueError(f"Cannot request more than {MAX_FILES_PER_REQUEST} uploads at once")
    return [generate_presigned_upload_url(ct, folder) for ct in content_types]


def get_public_url(s3_key: str) -> str:
    return f"https://{settings.S3_BUCKET_NAME}.s3.{settings.AWS_REGION}.amazonaws.com/{s3_key}"


# ---------------------------------------------------------------------------
# NEW: seller-scoped keys + validation
# ---------------------------------------------------------------------------

def seller_folder(seller_id: uuid.UUID) -> str:
    """Pass this as `folder` when issuing presigned URLs for a seller, so every
    key they receive looks like:  products/<seller_id>/<uuid>.<ext>"""
    return f"products/{seller_id}"


def is_valid_product_key(key: str, seller_id: uuid.UUID) -> bool:
    """True only for keys of the exact shape products/<this seller's id>/<uuid>.<ext>.
    Rejects other sellers' keys, arbitrary strings, and path tricks like '..'."""
    parts = key.split("/")
    if len(parts) != 3 or parts[0] != "products" or parts[1] != str(seller_id):
        return False
    name, dot, ext = parts[2].rpartition(".")
    if not dot or ext not in ALLOWED_EXTENSIONS:
        return False
    try:
        return str(uuid.UUID(name)) == name  # canonical lowercase uuid only
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# NEW: deletion (needs s3:DeleteObject on the IAM user/role)
# ---------------------------------------------------------------------------

def delete_objects(keys: list[str]) -> None:
    """Best-effort delete. Logs failures instead of raising, because a leftover
    file must never turn a successful product update into an error."""
    keys = list(dict.fromkeys(keys))
    for i in range(0, len(keys), S3_DELETE_BATCH_SIZE):
        batch = keys[i : i + S3_DELETE_BATCH_SIZE]
        try:
            response = s3_client.delete_objects(
                Bucket=settings.S3_BUCKET_NAME,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
            for err in response.get("Errors", []):
                logger.warning("S3 delete failed for %s: %s", err.get("Key"), err.get("Message"))
        except Exception:
            logger.exception("S3 delete_objects call failed for %d keys", len(batch))


async def delete_objects_async(keys: list[str]) -> None:
    """boto3 is blocking, so run the delete in a thread to keep the event loop free."""
    if keys:
        await asyncio.to_thread(delete_objects, keys)