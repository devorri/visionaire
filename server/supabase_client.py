"""
Supabase client helper for ScanForge backend.
Handles database operations and storage uploads.
"""

import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Database operations ──────────────────────────────────────────────

def create_scan(scan_data: dict) -> dict:
    """Insert a new scan record."""
    result = supabase.table("scans").insert(scan_data).execute()
    return result.data[0] if result.data else {}


def update_scan(scan_id: str, updates: dict) -> dict:
    """Update an existing scan record."""
    result = supabase.table("scans").update(updates).eq("id", scan_id).execute()
    return result.data[0] if result.data else {}


def get_scan(scan_id: str) -> dict:
    """Fetch a single scan by ID."""
    result = supabase.table("scans").select("*").eq("id", scan_id).single().execute()
    return result.data if result.data else {}


def get_all_scans() -> list:
    """Fetch all scans, newest first."""
    result = supabase.table("scans").select("*").order("created_at", desc=True).execute()
    return result.data if result.data else []


def delete_scan_record(scan_id: str):
    """Delete a scan and its photos from the database."""
    supabase.table("scan_photos").delete().eq("scan_id", scan_id).execute()
    supabase.table("scans").delete().eq("id", scan_id).execute()


def insert_scan_photo(photo_data: dict) -> dict:
    """Insert a photo record linked to a scan."""
    result = supabase.table("scan_photos").insert(photo_data).execute()
    return result.data[0] if result.data else {}


def get_scan_photos(scan_id: str) -> list:
    """Get all photos for a scan."""
    result = (
        supabase.table("scan_photos")
        .select("*")
        .eq("scan_id", scan_id)
        .order("created_at", desc=False)
        .execute()
    )
    return result.data if result.data else []


# ── Storage operations ────────────────────────────────────────────────

PHOTO_BUCKET = "gallery"
MODEL_BUCKET = "gallery"



def upload_photo(scan_id: str, filename: str, file_bytes: bytes, content_type: str = "image/jpeg") -> str:
    """Upload a photo to the scan-photos bucket. Returns the storage path."""
    path = f"{scan_id}/{filename}"
    supabase.storage.from_(PHOTO_BUCKET).upload(
        path, file_bytes, {"content-type": content_type}
    )
    return path


def upload_model(scan_id: str, filename: str, file_bytes: bytes, content_type: str = "model/gltf-binary") -> str:
    """Upload a 3D model file to the scan-models bucket. Returns the storage path."""
    path = f"{scan_id}/{filename}"
    supabase.storage.from_(MODEL_BUCKET).upload(
        path, file_bytes, {"content-type": content_type}
    )
    return path


def get_photo_url(path: str) -> str:
    """Get a public URL for a stored photo."""
    result = supabase.storage.from_(PHOTO_BUCKET).get_public_url(path)
    return result


def get_model_url(path: str) -> str:
    """Get a public URL for a stored model."""
    result = supabase.storage.from_(MODEL_BUCKET).get_public_url(path)
    return result


def download_photo(path: str) -> bytes:
    """Download a photo from storage."""
    result = supabase.storage.from_(PHOTO_BUCKET).download(path)
    return result


def delete_scan_storage(scan_id: str):
    """Delete all files for a scan from both buckets."""
    try:
        # List and delete photos
        photos = supabase.storage.from_(PHOTO_BUCKET).list(scan_id)
        if photos:
            paths = [f"{scan_id}/{f['name']}" for f in photos]
            supabase.storage.from_(PHOTO_BUCKET).remove(paths)
    except Exception:
        pass

    try:
        # List and delete models
        models = supabase.storage.from_(MODEL_BUCKET).list(scan_id)
        if models:
            paths = [f"{scan_id}/{f['name']}" for f in models]
            supabase.storage.from_(MODEL_BUCKET).remove(paths)
    except Exception:
        pass
