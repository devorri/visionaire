"""
Meshy API reconstruction helper.

This routes photo sets through Meshy's Multi-Image to 3D API when MESHY_API_KEY
is configured. Meshy currently accepts 1-4 images for this endpoint, so the
helper samples a small set from the capture and returns a textured GLB.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request
from typing import Callable

import cv2
import numpy as np


MESHY_API_URL = "https://api.meshy.ai/openapi/v1/multi-image-to-3d"
MAX_MESHY_IMAGES = 4


def is_meshy_configured() -> bool:
    return bool(os.getenv("MESHY_API_KEY"))


def _request_json(method: str, url: str, api_key: str, payload: dict | None = None, timeout: int = 60) -> dict:
    body = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Meshy API error {exc.code}: {detail}") from exc


def _download_bytes(url: str, timeout: int = 120) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _sample_images(image_bytes_list: list[bytes]) -> list[bytes]:
    usable = [raw for raw in image_bytes_list if raw]
    if len(usable) <= MAX_MESHY_IMAGES:
        return usable

    indices = np.linspace(0, len(usable) - 1, MAX_MESHY_IMAGES, dtype=int)
    return [usable[index] for index in indices]


def _to_data_uri(raw: bytes, quality: int) -> str | None:
    arr = np.frombuffer(raw, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None

    max_dim = 1400 if quality >= 80 else 1100
    h, w = img.shape[:2]
    if max(h, w) > max_dim:
        scale = max_dim / max(h, w)
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    ok, encoded = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        return None

    data = base64.b64encode(encoded.tobytes()).decode("ascii")
    return f"data:image/jpeg;base64,{data}"


def reconstruct_with_meshy(
    image_bytes_list: list[bytes],
    quality: int = 78,
    detail: int = 64,
    scan_name: str = "Visionaire Model",
    on_progress: Callable | None = None,
) -> dict:
    api_key = os.getenv("MESHY_API_KEY")
    if not api_key:
        raise RuntimeError("MESHY_API_KEY is not configured")

    image_urls = []
    for raw in _sample_images(image_bytes_list):
        data_uri = _to_data_uri(raw, quality)
        if data_uri:
            image_urls.append(data_uri)

    if not image_urls:
        raise ValueError("Meshy needs at least 1 valid image")

    if on_progress:
        on_progress(0, 100)
        on_progress(1, 100)

    create_response = _request_json(
        "POST",
        MESHY_API_URL,
        api_key,
        {
            "image_urls": image_urls,
            "ai_model": "latest",
            "should_texture": True,
            "enable_pbr": False,
            "target_formats": ["glb"],
        },
    )

    task_id = create_response.get("result")
    if not task_id:
        raise RuntimeError(f"Meshy did not return a task id: {create_response}")

    if on_progress:
        on_progress(3, 10)

    task = None
    deadline = time.monotonic() + 20 * 60
    while time.monotonic() < deadline:
        task = _request_json("GET", f"{MESHY_API_URL}/{task_id}", api_key, timeout=60)
        status = task.get("status", "")
        progress = int(task.get("progress") or 0)

        if on_progress:
            on_progress(5, max(10, min(95, progress)))

        if status == "SUCCEEDED":
            break

        if status in {"FAILED", "EXPIRED", "CANCELED"}:
            message = task.get("task_error", {}).get("message") or status
            raise RuntimeError(f"Meshy reconstruction failed: {message}")

        time.sleep(8)
    else:
        raise TimeoutError("Meshy reconstruction timed out")

    model_url = (task.get("model_urls") or {}).get("glb")
    if not model_url:
        raise RuntimeError(f"Meshy finished without a GLB model URL: {task}")

    glb_bytes = _download_bytes(model_url)

    if on_progress:
        on_progress(6, 100)

    score_base = 72 if len(image_urls) > 1 else 62
    return {
        "points_3d": None,
        "colors": None,
        "obj_bytes": None,
        "glb_bytes": glb_bytes,
        "ply_bytes": None,
        "point_count": 0,
        "quality_score": min(99, int(score_base + quality * 0.2)),
        "coverage_score": min(99, int(45 + len(image_urls) * 10 + detail * 0.12)),
        "texture_score": min(99, int(65 + len(image_urls) * 4 + detail * 0.18)),
        "provider_task_id": task_id,
        "provider": "meshy",
        "scan_name": scan_name,
    }
