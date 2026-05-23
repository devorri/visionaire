"""
Visionaire Web — FastAPI Backend
Handles photo uploads, photogrammetry processing, and model serving.
"""

import os
import uuid
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from dotenv import load_dotenv

load_dotenv()

from supabase_client import (
    create_scan,
    update_scan,
    get_scan,
    get_all_scans,
    delete_scan_record,
    insert_scan_photo,
    get_scan_photos,
    upload_photo,
    upload_model,
    get_model_url,
    get_photo_url,
    download_photo,
    delete_scan_storage,
)
from photogrammetry import reconstruct, STAGES

# ── App setup ─────────────────────────────────────────────────────────

app = FastAPI(title="Visionaire API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory progress tracking for active jobs
# { scan_id: { stage: int, progress: int, status: str, error: str|None } }
active_jobs: dict[str, dict] = {}


# ── Health check ──────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Visionaire API"}


# ── Scan CRUD ─────────────────────────────────────────────────────────

@app.post("/api/scans")
def api_create_scan(
    name: str = Query("Untitled Scan"),
    mode: str = Query("object"),
):
    """Create a new scan session."""
    scan_id = str(uuid.uuid4())
    scan_data = {
        "id": scan_id,
        "name": name,
        "mode": mode,
        "quality": 0,
        "detail": 0,
        "coverage": 0,
        "texture_score": 0,
        "points": 0,
        "photo_count": 0,
        "status": "uploading",
        "model_url": None,
        "thumbnail_url": None,
    }

    try:
        record = create_scan(scan_data)
        return {"scan": record}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/scans")
def api_list_scans():
    """List all scans."""
    try:
        scans = get_all_scans()
        return {"scans": scans}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/scans/{scan_id}")
def api_get_scan(scan_id: str):
    """Get a single scan by ID."""
    try:
        scan = get_scan(scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")
        return {"scan": scan}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/scans/{scan_id}")
def api_delete_scan(scan_id: str):
    """Delete a scan and all associated data."""
    try:
        delete_scan_storage(scan_id)
        delete_scan_record(scan_id)
        active_jobs.pop(scan_id, None)
        return {"deleted": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Photo upload ──────────────────────────────────────────────────────

@app.post("/api/scans/{scan_id}/photos")
async def api_upload_photos(scan_id: str, files: list[UploadFile] = File(...)):
    """Upload photos for a scan."""
    try:
        scan = get_scan(scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        uploaded = []
        for f in files:
            content = await f.read()
            filename = f"{uuid.uuid4().hex[:8]}_{f.filename}"
            content_type = f.content_type or "image/jpeg"

            # Upload to Supabase storage
            storage_path = upload_photo(scan_id, filename, content, content_type)

            # Record in database
            photo_record = insert_scan_photo({
                "scan_id": scan_id,
                "file_name": f.filename,
                "storage_path": storage_path,
            })
            uploaded.append(photo_record)

        # Update photo count
        photos = get_scan_photos(scan_id)
        update_scan(scan_id, {"photo_count": len(photos), "status": "uploaded"})

        return {"uploaded": len(uploaded), "total_photos": len(photos)}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Processing ────────────────────────────────────────────────────────

def _process_scan_background(scan_id: str, quality: int, detail: int):
    """Background thread that runs the photogrammetry pipeline."""
    try:
        # Initialize progress tracking
        active_jobs[scan_id] = {"stage": 0, "progress": 0, "status": "processing", "error": None}

        # Download all photos from Supabase
        photos = get_scan_photos(scan_id)
        if not photos:
            active_jobs[scan_id] = {"stage": 0, "progress": 0, "status": "failed", "error": "No photos found"}
            update_scan(scan_id, {"status": "failed"})
            return

        image_bytes_list = []
        for photo in photos:
            try:
                data = download_photo(photo["storage_path"])
                image_bytes_list.append(data)
            except Exception as exc:
                print(f"Failed to download {photo['storage_path']}: {exc}")

        if len(image_bytes_list) < 2:
            active_jobs[scan_id] = {"stage": 0, "progress": 0, "status": "failed", "error": "Need at least 2 photos"}
            update_scan(scan_id, {"status": "failed"})
            return

        # Progress callback
        def on_progress(stage: int, percent: int):
            active_jobs[scan_id]["stage"] = stage
            active_jobs[scan_id]["progress"] = percent

        # Run the actual reconstruction
        scan = get_scan(scan_id)
        scan_name = scan.get("name", "Visionaire Model")

        result = reconstruct(
            image_bytes_list=image_bytes_list,
            quality=quality,
            detail=detail,
            scan_name=scan_name,
            on_progress=on_progress,
        )

        if result.error:
            active_jobs[scan_id] = {"stage": 0, "progress": 0, "status": "failed", "error": result.error}
            update_scan(scan_id, {"status": "failed"})
            return

        # Upload generated models to Supabase storage
        model_url = None
        obj_url = None

        if result.glb_bytes:
            try:
                path = upload_model(scan_id, f"{scan_name}.glb", result.glb_bytes, "model/gltf-binary")
                model_url = get_model_url(path)
            except Exception as exc:
                print(f"GLB upload failed: {exc}")

        if result.obj_bytes:
            try:
                path = upload_model(scan_id, f"{scan_name}.obj", result.obj_bytes, "application/octet-stream")
                obj_url = get_model_url(path)
            except Exception as exc:
                print(f"OBJ upload failed: {exc}")

        if result.ply_bytes:
            try:
                upload_model(scan_id, f"{scan_name}.ply", result.ply_bytes, "application/octet-stream")
            except Exception as exc:
                print(f"PLY upload failed: {exc}")

        # Update scan record with results
        update_data = {
            "status": "ready",
            "quality": result.quality_score,
            "coverage": result.coverage_score,
            "texture_score": result.texture_score,
            "points": result.point_count,
            "model_url": model_url,
            "detail": detail,
        }
        update_scan(scan_id, update_data)

        active_jobs[scan_id] = {"stage": len(STAGES) - 1, "progress": 100, "status": "ready", "error": None}

    except Exception as exc:
        print(f"Background processing error: {exc}")
        import traceback
        traceback.print_exc()
        active_jobs[scan_id] = {"stage": 0, "progress": 0, "status": "failed", "error": str(exc)}
        try:
            update_scan(scan_id, {"status": "failed"})
        except Exception:
            pass


@app.post("/api/scans/{scan_id}/process")
def api_start_processing(
    scan_id: str,
    quality: int = Query(78, ge=40, le=100),
    detail: int = Query(64, ge=35, le=100),
):
    """Start photogrammetry processing for a scan."""
    try:
        scan = get_scan(scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        if scan.get("status") == "processing":
            raise HTTPException(status_code=409, detail="Already processing")

        update_scan(scan_id, {"status": "processing"})

        # Launch processing in background thread
        thread = threading.Thread(
            target=_process_scan_background,
            args=(scan_id, quality, detail),
            daemon=True,
        )
        thread.start()

        return {"status": "processing", "message": "Reconstruction started"}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/scans/{scan_id}/status")
def api_scan_status(scan_id: str):
    """Get processing status for a scan (used for polling)."""
    # Check in-memory job status first
    job = active_jobs.get(scan_id)
    if job:
        return {
            "status": job["status"],
            "stage": job["stage"],
            "stage_name": STAGES[job["stage"]] if job["stage"] < len(STAGES) else "Done",
            "progress": job["progress"],
            "stages": STAGES,
            "error": job.get("error"),
        }

    # Fall back to database
    try:
        scan = get_scan(scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        return {
            "status": scan.get("status", "unknown"),
            "stage": len(STAGES) - 1 if scan.get("status") == "ready" else 0,
            "stage_name": "Done" if scan.get("status") == "ready" else "Pending",
            "progress": 100 if scan.get("status") == "ready" else 0,
            "stages": STAGES,
            "error": None,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Run server ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
