"""
DigitalOcean Spaces utility for uploading converted files.
"""
import logging
import os
import re
import boto3
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# DO Spaces config from environment
DO_SPACES_KEY = os.getenv("DO_SPACES_KEY", "").strip()
DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET", "").strip()
DO_SPACES_REGION = os.getenv("DO_SPACES_REGION", "").strip()
DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET", "").strip()
DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT", "").strip()

# Environment prefix: "live" for production, "dev" for development
ENV_PREFIX = "live" if os.getenv("ENVIRONMENT") == "production" else "dev"


def extract_filename_from_header(content_disposition: str) -> str:
    """Extract filename from Content-Disposition header."""
    if not content_disposition:
        return "output.bin"

    patterns = [
        r'filename="([^"]+)"',
        r'filename=([^\s;]+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, content_disposition)
        if match:
            return sanitize_filename(match.group(1))

    return "output.bin"


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal and remove unsafe characters."""
    filename = os.path.basename(filename)
    filename = filename.replace("..", "")
    filename = re.sub(r'[<>:"|?*]', '', filename)
    filename = filename.replace(" ", "_")

    if not filename or filename == ".":
        filename = "output.bin"

    return filename


def build_object_key(user_id: str, endpoint: str, original_filename: str) -> str:
    """The Spaces key ``upload_to_gcs`` assigns for these arguments.

    Exposed so a caller can DETERMINISTICALLY re-derive a previously uploaded
    object's key without storing it — /v2/ingest resume reads back each
    completed page's staged JSONL this way. Pure: no network, no client.
    """
    sanitized_filename = sanitize_filename(original_filename)
    clean_endpoint = endpoint.replace("/v1/convert/", "").replace("/", "-")
    # Path: {ENV_PREFIX}/files/user_id/endpoint/filename
    return f"{ENV_PREFIX}/files/{user_id}/{clean_endpoint}/{sanitized_filename}"


def upload_to_gcs(
    file_bytes: bytes,
    user_id: str,
    endpoint: str,
    original_filename: str
) -> Dict[str, Any]:
    """
    Upload converted file to DigitalOcean Spaces.

    Returns dict with: file_url, file_size, filename
    """
    # Initialize S3 client for DO Spaces
    client = boto3.client(
        's3',
        region_name=DO_SPACES_REGION,
        endpoint_url=f"https://{DO_SPACES_REGION}.digitaloceanspaces.com",
        aws_access_key_id=DO_SPACES_KEY,
        aws_secret_access_key=DO_SPACES_SECRET
    )

    sanitized_filename = sanitize_filename(original_filename)
    object_key = build_object_key(user_id, endpoint, original_filename)

    # Content type mapping
    ext = Path(sanitized_filename).suffix.lower()
    content_types = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".xml": "application/xml",
        ".json": "application/json",
        ".html": "text/html",
        ".yaml": "application/x-yaml",
        ".yml": "application/x-yaml",
        ".zip": "application/zip",
        ".jsonl": "application/x-ndjson",
        ".ndjson": "application/x-ndjson",
    }
    content_type = content_types.get(ext, "application/octet-stream")

    # Upload to DO Spaces (private by default - no ACL specified)
    client.put_object(
        Bucket=DO_SPACES_BUCKET,
        Key=object_key,
        Body=file_bytes,
        ContentType=content_type
    )

    return {
        "object_key": object_key,
        "file_size": len(file_bytes),
        "filename": sanitized_filename
    }


def upload_rendered_html(html: str, project_id: str, job_id: str) -> str:
    """Upload the post-render HTML for a Playwright conversion to DO Spaces.

    Stored at `{ENV_PREFIX}/html/{project_id}/{job_id}.html` per the V2
    Phase 0 plan. Returns the object key for persistence on the Activity
    row. Caller is responsible for scheduling the 90-day deletion task
    via `schedule_file_cleanup` — keeping that in the caller lets us
    reuse the existing retention plumbing without coupling layers.
    """
    if not html:
        raise ValueError("upload_rendered_html called with empty html")

    client = boto3.client(
        "s3",
        region_name=DO_SPACES_REGION,
        endpoint_url=f"https://{DO_SPACES_REGION}.digitaloceanspaces.com",
        aws_access_key_id=DO_SPACES_KEY,
        aws_secret_access_key=DO_SPACES_SECRET,
    )

    safe_project = sanitize_filename(str(project_id))
    safe_job = sanitize_filename(str(job_id))
    object_key = f"{ENV_PREFIX}/html/{safe_project}/{safe_job}.html"

    client.put_object(
        Bucket=DO_SPACES_BUCKET,
        Key=object_key,
        Body=html.encode("utf-8"),
        ContentType="text/html; charset=utf-8",
    )
    return object_key


def download_from_storage(object_key: str) -> bytes:
    """Fetch an object's bytes from DigitalOcean Spaces.

    Added for the F.8 batch ZIP bundle: per-URL artifacts are uploaded
    individually by perceive_flow, then pulled back to build the
    archive. Raises on a missing key — the caller decides whether a
    partial ZIP is acceptable.
    """
    client = boto3.client(
        "s3",
        region_name=DO_SPACES_REGION,
        endpoint_url=f"https://{DO_SPACES_REGION}.digitaloceanspaces.com",
        aws_access_key_id=DO_SPACES_KEY,
        aws_secret_access_key=DO_SPACES_SECRET,
    )
    response = client.get_object(Bucket=DO_SPACES_BUCKET, Key=object_key)
    return response["Body"].read()


def delete_from_storage(object_key: str):
    """Delete a file from DigitalOcean Spaces."""
    try:
        client = boto3.client(
            's3',
            region_name=DO_SPACES_REGION,
            endpoint_url=f"https://{DO_SPACES_REGION}.digitaloceanspaces.com",
            aws_access_key_id=DO_SPACES_KEY,
            aws_secret_access_key=DO_SPACES_SECRET
        )
        client.delete_object(Bucket=DO_SPACES_BUCKET, Key=object_key)
    except Exception as e:
        logger.error("Failed to delete %s: %s", object_key, e)


def generate_presigned_url(object_key: str, user_id: str, expires_in: int = 900) -> str:
    """
    Generate a pre-signed URL for secure file download.

    Args:
        object_key: The S3 object key (e.g., "files/user123/conversion/file.pdf")
        user_id: The authenticated user's ID (for access validation)
        expires_in: URL expiration time in seconds (default: 15 minutes)

    Returns:
        Pre-signed URL string

    Raises:
        PermissionError: If user_id doesn't match the file's owner
    """
    # Security check: ensure the object belongs to this user
    expected_prefix = f"{ENV_PREFIX}/files/{user_id}/"
    if not object_key.startswith(expected_prefix):
        raise PermissionError(f"Access denied: user {user_id} cannot access this file")

    client = boto3.client(
        's3',
        region_name=DO_SPACES_REGION,
        endpoint_url=f"https://{DO_SPACES_REGION}.digitaloceanspaces.com",
        aws_access_key_id=DO_SPACES_KEY,
        aws_secret_access_key=DO_SPACES_SECRET
    )

    url = client.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': DO_SPACES_BUCKET,
            'Key': object_key
        },
        ExpiresIn=expires_in
    )

    return url
