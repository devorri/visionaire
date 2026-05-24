"""
Live camera streaming and real-time 3D reconstruction.

Handles WebSocket connections for streaming video frames from client
and sending back incremental point clouds.
"""

import asyncio
import base64
import json
import time
from typing import Callable, Optional
from datetime import datetime

import numpy as np
import cv2


class LiveStreamSession:
    """Manages a single live streaming session."""
    
    def __init__(self, session_id: str, quality: int = 60):
        self.session_id = session_id
        self.quality = quality  # JPEG quality for frame compression
        self.start_time = time.time()
        self.frame_count = 0
        self.last_frame_time = 0
        self.fps = 0
        self.status = "idle"  # idle, streaming, processing, paused
        self.error: Optional[str] = None
    
    def update_stats(self):
        """Update FPS and other statistics."""
        current_time = time.time()
        delta = current_time - self.last_frame_time
        
        if delta > 0:
            self.fps = 1.0 / delta
        
        self.last_frame_time = current_time
        self.frame_count += 1
    
    def get_stats(self) -> dict:
        """Get session statistics."""
        elapsed = time.time() - self.start_time
        return {
            "session_id": self.session_id,
            "status": self.status,
            "frame_count": self.frame_count,
            "elapsed_seconds": elapsed,
            "fps": round(self.fps, 2),
            "error": self.error,
        }


class FrameBuffer:
    """Circular buffer for storing recent frames."""
    
    def __init__(self, max_frames: int = 30):
        self.max_frames = max_frames
        self.frames: list[np.ndarray] = []
        self.timestamps: list[float] = []
    
    def add_frame(self, frame: np.ndarray, timestamp: Optional[float] = None):
        """Add frame to buffer, dropping oldest if full."""
        if timestamp is None:
            timestamp = time.time()
        
        self.frames.append(frame.copy())
        self.timestamps.append(timestamp)
        
        if len(self.frames) > self.max_frames:
            self.frames.pop(0)
            self.timestamps.pop(0)
    
    def get_frames(self) -> list[np.ndarray]:
        """Get all frames in buffer."""
        return self.frames.copy()
    
    def get_sampled_frames(self, count: int) -> list[np.ndarray]:
        """Get evenly sampled frames from buffer."""
        if len(self.frames) <= count:
            return self.frames.copy()
        
        indices = np.linspace(0, len(self.frames) - 1, count, dtype=int)
        return [self.frames[i] for i in indices]
    
    def clear(self):
        """Clear buffer."""
        self.frames.clear()
        self.timestamps.clear()


class LiveCameraProcessor:
    """
    Processes frames from live camera stream.
    
    Runs a lightweight feature detection + triangulation loop
    to generate partial point clouds in real-time.
    """
    
    def __init__(self, update_interval: float = 0.5):
        self.update_interval = update_interval  # seconds between point cloud updates
        self.last_update = time.time()
        self.frame_buffer = FrameBuffer(max_frames=15)
        self.keypoints_cache: list = []
        self.descriptors_cache: list = []
        self.sift = cv2.SIFT_create(nfeatures=500)  # Fewer features for speed
    
    def process_frame(self, frame_bytes: bytes) -> dict:
        """
        Process a single frame from client.
        
        Returns: {
            "frame_id": int,
            "processed": bool,
            "has_points": bool,
            "point_count": int,
            "points_base64": str,  # If ready to send
        }
        """
        # Decode frame
        arr = np.frombuffer(frame_bytes, np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        
        if frame is None:
            return {
                "processed": False,
                "error": "Failed to decode frame",
            }
        
        # Add to buffer
        self.frame_buffer.add_frame(frame)
        
        # Check if enough time elapsed for processing
        current_time = time.time()
        if current_time - self.last_update < self.update_interval:
            return {
                "processed": True,
                "has_points": False,
                "frame_id": len(self.frame_buffer.frames),
            }
        
        self.last_update = current_time
        
        # Try to extract features and build partial point cloud
        try:
            result = self._extract_and_triangulate()
            result["processed"] = True
            result["frame_id"] = len(self.frame_buffer.frames)
            return result
        except Exception as e:
            return {
                "processed": True,
                "has_points": False,
                "error": str(e),
            }
    
    def _extract_and_triangulate(self) -> dict:
        """Extract features and generate partial point cloud."""
        frames = self.frame_buffer.get_sampled_frames(3)  # Use 3 frames
        
        if len(frames) < 2:
            return {"has_points": False, "point_count": 0}
        
        # Detect features in each frame
        kps, descs = [], []
        for frame in frames:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            kp, desc = self.sift.detectAndCompute(gray, None)
            
            if desc is None or len(desc) < 8:
                return {"has_points": False, "point_count": 0}
            
            kps.append(kp)
            descs.append(desc)
        
        # Match features between frames
        FLANN_INDEX_KDTREE = 1
        flann = cv2.FlannBasedMatcher(
            dict(algorithm=FLANN_INDEX_KDTREE, trees=5),
            dict(checks=50),
        )
        
        matches_01 = flann.knnMatch(descs[0], descs[1], k=2)
        if not matches_01:
            return {"has_points": False, "point_count": 0}
        
        # Lowe's ratio test
        good_matches = []
        for pair in matches_01:
            if len(pair) == 2:
                m, n = pair
                if m.distance < 0.7 * n.distance:
                    good_matches.append(m)
        
        if len(good_matches) < 8:
            return {"has_points": False, "point_count": 0}
        
        # Rough triangulation (simplified for speed)
        h, w = frames[0].shape[:2]
        K = self._estimate_camera_matrix((h, w))
        
        pts1 = np.float32([kps[0][m.queryIdx].pt for m in good_matches])
        pts2 = np.float32([kps[1][m.trainIdx].pt for m in good_matches])
        
        # Compute essential matrix
        E, mask = cv2.findEssentialMat(pts1, pts2, K, cv2.RANSAC, 0.999, 1.0)
        
        if E is None:
            return {"has_points": False, "point_count": 0}
        
        _, R, t, pose_mask = cv2.recoverPose(E, pts1, pts2, K, mask=mask)
        
        # Simple triangulation
        P1 = K @ np.hstack([np.eye(3), np.zeros((3, 1))])
        P2 = K @ np.hstack([R, t])
        
        points_4d = cv2.triangulatePoints(
            P1, P2,
            pts1[mask.ravel() == 1].T,
            pts2[mask.ravel() == 1].T,
        )
        
        points_3d = (points_4d[:3] / points_4d[3]).T
        
        # Filter invalid points
        valid = (points_3d[:, 2] > 0.01) & (points_3d[:, 2] < 100.0)
        points_3d = points_3d[valid]
        
        if len(points_3d) < 10:
            return {"has_points": False, "point_count": 0}
        
        # Encode to base64
        points_base64 = base64.b64encode(points_3d.astype(np.float32)).decode()
        
        return {
            "has_points": True,
            "point_count": len(points_3d),
            "points_base64": points_base64,
            "points_shape": points_3d.shape,
        }
    
    def _estimate_camera_matrix(self, image_shape: tuple) -> np.ndarray:
        """Estimate camera intrinsics."""
        h, w = image_shape[:2]
        focal_length = max(w, h) * 1.2
        cx, cy = w / 2.0, h / 2.0
        return np.array([
            [focal_length, 0, cx],
            [0, focal_length, cy],
            [0, 0, 1],
        ], dtype=np.float64)
    
    def get_buffer_frames(self) -> list[bytes]:
        """Get all frames in buffer as JPEG bytes."""
        frames = self.frame_buffer.get_frames()
        result = []
        
        for frame in frames:
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                result.append(encoded.tobytes())
        
        return result
    
    def clear(self):
        """Clear buffers."""
        self.frame_buffer.clear()
        self.keypoints_cache.clear()
        self.descriptors_cache.clear()


class LiveSessionManager:
    """Manages multiple concurrent live streaming sessions."""
    
    def __init__(self):
        self.sessions: dict[str, LiveStreamSession] = {}
        self.processors: dict[str, LiveCameraProcessor] = {}
    
    def create_session(self, session_id: str, quality: int = 60) -> LiveStreamSession:
        """Create a new streaming session."""
        session = LiveStreamSession(session_id, quality)
        processor = LiveCameraProcessor()
        
        self.sessions[session_id] = session
        self.processors[session_id] = processor
        
        return session
    
    def get_session(self, session_id: str) -> Optional[LiveStreamSession]:
        """Get session by ID."""
        return self.sessions.get(session_id)
    
    def get_processor(self, session_id: str) -> Optional[LiveCameraProcessor]:
        """Get processor by session ID."""
        return self.processors.get(session_id)
    
    def delete_session(self, session_id: str):
        """Delete a session."""
        self.sessions.pop(session_id, None)
        self.processors.pop(session_id, None)
    
    def get_all_sessions(self) -> list[dict]:
        """Get stats for all active sessions."""
        return [s.get_stats() for s in self.sessions.values()]
