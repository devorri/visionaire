"""
Visionaire Web — FastAPI Backend
Handles photo uploads, photogrammetry processing, and model serving.
"""

import os
import uuid
import time
import threading
from datetime import datetime, timezone

import cv2
import numpy as np

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
from media_frames import extract_video_frames
from measurement_calibration import MeasurementCalibration
from progressive_refinement import ProgressivePointCloudRefiner
from live_camera_streaming import LiveSessionManager, LiveCameraProcessor

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

# Measurement calibration per scan
# { scan_id: MeasurementCalibration }
scan_calibrations: dict[str, MeasurementCalibration] = {}

# Progressive refinement per scan
# { scan_id: ProgressivePointCloudRefiner }
scan_refiners: dict[str, ProgressivePointCloudRefiner] = {}

# Live streaming session manager
live_session_manager = LiveSessionManager()

# Raw images cache for progressive triangulation
# { scan_id: list[np.ndarray] }
scan_raw_images: dict[str, list[np.ndarray]] = {}


def _estimate_camera_matrix(image_shape: tuple) -> np.ndarray:
    """Estimate a reasonable camera intrinsic matrix from image dimensions."""
    h, w = image_shape[:2]
    focal_length = max(w, h) * 1.2
    cx, cy = w / 2.0, h / 2.0
    return np.array([
        [focal_length, 0, cx],
        [0, focal_length, cy],
        [0, 0, 1],
    ], dtype=np.float64)


def _triangulate_two_frames(
    img1: np.ndarray,
    img2: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Run SIFT feature detection, FLANN matching, Essential Matrix pose
    recovery, and triangulation on two frames.

    Returns (points_3d, colors) or empty arrays on failure.
    """
    gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY) if len(img1.shape) == 3 else img1
    gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY) if len(img2.shape) == 3 else img2

    sift = cv2.SIFT_create(nfeatures=1500)
    kp1, desc1 = sift.detectAndCompute(gray1, None)
    kp2, desc2 = sift.detectAndCompute(gray2, None)

    if desc1 is None or desc2 is None or len(desc1) < 8 or len(desc2) < 8:
        return np.zeros((0, 3)), np.zeros((0, 3))

    FLANN_INDEX_KDTREE = 1
    flann = cv2.FlannBasedMatcher(
        dict(algorithm=FLANN_INDEX_KDTREE, trees=5),
        dict(checks=80),
    )
    raw_matches = flann.knnMatch(desc1, desc2, k=2)

    good = []
    for pair in raw_matches:
        if len(pair) == 2:
            m, n = pair
            if m.distance < 0.75 * n.distance:
                good.append(m)

    if len(good) < 8:
        return np.zeros((0, 3)), np.zeros((0, 3))

    pts1 = np.float32([kp1[m.queryIdx].pt for m in good])
    pts2 = np.float32([kp2[m.trainIdx].pt for m in good])

    K = _estimate_camera_matrix(img1.shape)

    E, mask = cv2.findEssentialMat(pts1, pts2, K, method=cv2.RANSAC, prob=0.999, threshold=1.0)
    if E is None:
        return np.zeros((0, 3)), np.zeros((0, 3))

    _, R, t, pose_mask = cv2.recoverPose(E, pts1, pts2, K, mask=mask)

    P1 = K @ np.hstack([np.eye(3), np.zeros((3, 1))])
    P2 = K @ np.hstack([R, t])

    points_4d = cv2.triangulatePoints(P1, P2, pts1.T, pts2.T)
    points_3d = (points_4d[:3] / points_4d[3]).T

    # Filter invalid points
    valid = (
        np.isfinite(points_3d).all(axis=1)
        & (np.abs(points_3d[:, 0]) < 50)
        & (np.abs(points_3d[:, 1]) < 50)
        & (np.abs(points_3d[:, 2]) < 50)
        & (points_3d[:, 2] > 0)
    )
    valid_points = points_3d[valid]

    # Extract colors from img1
    colors = []
    valid_pts1 = pts1[valid]
    h, w = img1.shape[:2]
    for pt in valid_pts1:
        x, y = int(np.clip(pt[0], 0, w - 1)), int(np.clip(pt[1], 0, h - 1))
        if len(img1.shape) == 3:
            bgr = img1[y, x]
            colors.append([bgr[2] / 255.0, bgr[1] / 255.0, bgr[0] / 255.0])
        else:
            v = img1[y, x] / 255.0
            colors.append([v, v, v])

    if len(valid_points) == 0:
        return np.zeros((0, 3)), np.zeros((0, 3))

    return valid_points, np.array(colors)



# ── Health check ──────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Visionaire API"}


@app.get("/")
def root():
    return health()


def _is_video_file(path: str = "", content_type: str = "") -> bool:
    return content_type.startswith("video/") or path.lower().rsplit(".", 1)[-1] in {
        "mp4",
        "mov",
        "webm",
        "m4v",
        "avi",
    }


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
                if _is_video_file(photo.get("storage_path", ""), photo.get("content_type", "")):
                    frames = extract_video_frames(data, suffix=os.path.splitext(photo["storage_path"])[1] or ".mp4")
                    image_bytes_list.extend(frames)
                else:
                    image_bytes_list.append(data)
            except Exception as exc:
                print(f"Failed to download {photo['storage_path']}: {exc}")

        if len(image_bytes_list) < 1:
            active_jobs[scan_id] = {"stage": 0, "progress": 0, "status": "failed", "error": "Need at least 1 photo"}
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

        if not model_url and obj_url:
            model_url = obj_url

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


# ── Measurement Calibration ──────────────────────────────────────────

@app.post("/api/scans/{scan_id}/calibration/aruco")
async def api_calibrate_with_aruco(
    scan_id: str,
    marker_size: str = Query("10cm"),
):
    """Calibrate measurement scale using ArUco marker from first photo."""
    try:
        scan = get_scan(scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        photos = get_scan_photos(scan_id)
        if not photos:
            raise HTTPException(status_code=400, detail="No photos found for calibration")

        # Download first photo
        first_photo = photos[0]
        photo_data = download_photo(first_photo["storage_path"])

        # Create calibration
        if scan_id not in scan_calibrations:
            scan_calibrations[scan_id] = MeasurementCalibration()

        calibration = scan_calibrations[scan_id]

        # Detect and calibrate
        import cv2
        import numpy as np
        arr = np.frombuffer(photo_data, np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)

        success = calibration.calibrate_from_aruco(image, marker_size_key=marker_size)

        if not success:
            raise HTTPException(status_code=400, detail="ArUco marker not detected in photo")

        calibration_config = calibration.export_calibration()
        update_scan(scan_id, {"calibration": calibration_config})

        return {
            "calibrated": True,
            "method": "aruco",
            "marker_size": marker_size,
            "scale_factor": calibration.scale_factor,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/scans/{scan_id}/calibration/manual")
def api_calibrate_manual(
    scan_id: str,
    reference_distance_meters: float = Query(..., description="Known distance between two points"),
):
    """
    Calibrate using manually selected reference distance.
    Requires two reference points to be added first via calibration/add-point.
    """
    try:
        if scan_id not in scan_calibrations:
            raise HTTPException(status_code=400, detail="No calibration started for this scan")

        calibration = scan_calibrations[scan_id]

        if len(calibration.reference_points) < 2:
            raise HTTPException(status_code=400, detail="Need at least 2 reference points")

        success = calibration.calibrate_from_manual_distance(reference_distance_meters)

        if not success:
            raise HTTPException(status_code=400, detail="Calibration failed - invalid points")

        calibration_config = calibration.export_calibration()
        update_scan(scan_id, {"calibration": calibration_config})

        return {
            "calibrated": True,
            "method": "manual",
            "reference_distance": reference_distance_meters,
            "scale_factor": calibration.scale_factor,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/scans/{scan_id}/calibration/reset")
def api_reset_calibration(scan_id: str):
    """Reset calibration for a scan."""
    scan_calibrations.pop(scan_id, None)
    try:
        update_scan(scan_id, {"calibration": None})
    except:
        pass

    return {"reset": True}


@app.get("/api/scans/{scan_id}/dimensions")
def api_get_dimensions(scan_id: str):
    """Get bounding box dimensions of the model in meters."""
    try:
        scan = get_scan(scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")

        if scan_id not in scan_calibrations:
            raise HTTPException(status_code=400, detail="Scan not calibrated")

        # Get merged point cloud from refiner if available
        if scan_id in scan_refiners:
            points, colors = scan_refiners[scan_id].get_merged_cloud()
        else:
            raise HTTPException(status_code=400, detail="No point cloud available")

        if points is None or len(points) == 0:
            raise HTTPException(status_code=400, detail="Point cloud is empty")

        calibration = scan_calibrations[scan_id]
        dimensions = calibration.get_bounding_box_dimensions(points)

        return {
            "model_name": scan.get("name", "Model"),
            "dimensions": dimensions,
            "unit": "meters",
            "calibration_type": calibration.calibration_type,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Progressive Refinement ──────────────────────────────────────────

@app.post("/api/scans/{scan_id}/refiner/initialize")
def api_initialize_refiner(scan_id: str):
    """Initialize progressive refinement for a scan."""
    try:
        if scan_id not in scan_refiners:
            scan_refiners[scan_id] = ProgressivePointCloudRefiner()

        return {
            "initialized": True,
            "scan_id": scan_id,
            "status": "ready",
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/scans/{scan_id}/refiner/add-frame")
async def api_refiner_add_frame(
    scan_id: str,
    frame_id: int = Query(...),
    file: UploadFile = File(...),
):
    """
    Add a frame to progressive refinement.

    Decodes the uploaded image, runs SIFT triangulation against the
    previous frame, and feeds the resulting 3D points into the
    ProgressivePointCloudRefiner for Procrustes alignment and
    radius-based deduplication.
    """
    try:
        if scan_id not in scan_refiners:
            raise HTTPException(status_code=400, detail="Refiner not initialized")

        content = await file.read()

        # Decode uploaded image into OpenCV frame
        arr = np.frombuffer(content, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(status_code=400, detail="Could not decode image")

        # Resize large images for processing speed
        max_dim = 800
        h, w = img.shape[:2]
        if max(h, w) > max_dim:
            scale = max_dim / max(h, w)
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

        # Store in the per-scan raw images cache
        if scan_id not in scan_raw_images:
            scan_raw_images[scan_id] = []
        scan_raw_images[scan_id].append(img)

        # Attempt triangulation against the previous frame
        new_point_count = 0
        merged_point_count = 0
        duplicates_removed = 0
        points_base64 = ""
        point_count = 0
        frames_cache = scan_raw_images[scan_id]

        if len(frames_cache) >= 2:
            prev_img = frames_cache[-2]
            curr_img = frames_cache[-1]

            points_3d, colors = _triangulate_two_frames(prev_img, curr_img)

            if len(points_3d) > 0:
                # Encode points to base64 for frontend
                import base64
                points_base64 = base64.b64encode(points_3d.astype(np.float32)).decode()
                point_count = len(points_3d)

                result = scan_refiners[scan_id].add_frame(
                    points_3d=points_3d,
                    colors=colors,
                    frame_id=frame_id,
                    timestamp=time.time(),
                    source="live",
                )
                new_point_count = result.get("new_points_added", 0)
                merged_point_count = result.get("merged_point_count", 0)
                duplicates_removed = result.get("duplicates_removed", 0)

        # Keep only the last 30 frames to bound memory usage
        if len(frames_cache) > 30:
            scan_raw_images[scan_id] = frames_cache[-30:]

        # Also persist the frame to Supabase storage
        try:
            filename = f"{uuid.uuid4().hex[:8]}_{file.filename}"
            upload_photo(scan_id, filename, content, file.content_type or "image/jpeg")
        except Exception as upload_err:
            print(f"Frame storage upload skipped: {upload_err}")

        stats = scan_refiners[scan_id].get_statistics()

        return {
            "frame_added": True,
            "frame_id": frame_id,
            "new_point_count": new_point_count,
            "merged_point_count": merged_point_count,
            "duplicates_removed": duplicates_removed,
            "has_points": len(points_base64) > 0,
            "point_count": point_count,
            "points_base64": points_base64,
            "refiner_stats": stats,
        }

    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/scans/{scan_id}/refiner/stats")
def api_refiner_stats(scan_id: str):
    """Get refinement statistics."""
    try:
        if scan_id not in scan_refiners:
            raise HTTPException(status_code=400, detail="Refiner not initialized")

        stats = scan_refiners[scan_id].get_statistics()
        state = scan_refiners[scan_id].export_state()

        return {
            "scan_id": scan_id,
            "statistics": stats,
            "state": state,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Live Camera Streaming ──────────────────────────────────────────

@app.post("/api/live-sessions")
def api_create_live_session(
    quality: int = Query(60, ge=30, le=100),
):
    """Create a new live streaming session."""
    try:
        session_id = str(uuid.uuid4())
        session = live_session_manager.create_session(session_id, quality)

        return {
            "session_id": session_id,
            "quality": quality,
            "status": "ready",
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/live-sessions/{session_id}")
def api_get_live_session(session_id: str):
    """Get live session status."""
    try:
        session = live_session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        return {"session": session.get_stats()}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/live-sessions/{session_id}/frame")
async def api_process_live_frame(
    session_id: str,
    file: UploadFile = File(...),
):
    """
    Process a frame from live camera.
    
    Client sends JPEG frame, server returns partial point cloud if ready.
    """
    try:
        session = live_session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        processor = live_session_manager.get_processor(session_id)

        # Read frame
        content = await file.read()

        # Process
        result = processor.process_frame(content)

        session.update_stats()
        session.status = "streaming"

        return {
            "session_id": session_id,
            "frame_id": result.get("frame_id"),
            "has_points": result.get("has_points", False),
            "point_count": result.get("point_count", 0),
            "points_base64": result.get("points_base64", ""),
            "fps": session.fps,
            "frame_count": session.frame_count,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/live-sessions/{session_id}/finalize")
def api_finalize_live_session(
    session_id: str,
    scan_id: str = Query(...),
):
    """
    Finalize live session and save the refined scan.
    
    Extracts accumulated frames and runs full reconstruction.
    """
    try:
        session = live_session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        processor = live_session_manager.get_processor(session_id)

        # Get all frames from buffer
        frame_bytes_list = processor.get_buffer_frames()

        if len(frame_bytes_list) == 0:
            raise HTTPException(status_code=400, detail="No frames captured")

        # Save frames to scan
        for i, frame_bytes in enumerate(frame_bytes_list):
            try:
                filename = f"live_frame_{session_id}_{i:03d}.jpg"
                storage_path = upload_photo(scan_id, filename, frame_bytes, "image/jpeg")

                insert_scan_photo({
                    "scan_id": scan_id,
                    "file_name": filename,
                    "storage_path": storage_path,
                })
            except Exception as e:
                print(f"Failed to save frame {i}: {e}")

        # Update scan
        photos = get_scan_photos(scan_id)
        update_scan(scan_id, {"photo_count": len(photos), "status": "uploaded"})

        # Clean up session
        live_session_manager.delete_session(session_id)

        return {
            "finalized": True,
            "session_id": session_id,
            "scan_id": scan_id,
            "frames_saved": len(frame_bytes_list),
            "ready_for_processing": True,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/live-sessions")
def api_list_live_sessions():
    """List all active live sessions."""
    try:
        sessions = live_session_manager.get_all_sessions()
        return {"sessions": sessions}

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Run server ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
