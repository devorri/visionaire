"""
Measurement calibration system for converting point clouds to real-world scale.

Supports two calibration modes:
1. ArUco Marker Detection — automatic scale from known marker size
2. Manual Reference — user-provided reference distance between two points
"""

import numpy as np
import cv2
from typing import Tuple, Optional


# Standard ArUco marker sizes (in meters) — customize as needed
ARUCO_MARKER_SIZES = {
    "5cm": 0.05,
    "10cm": 0.10,
    "15cm": 0.15,
    "20cm": 0.20,
    "30cm": 0.30,
}


class CalibrationPoint:
    """Represents a 3D point selected for manual calibration."""
    def __init__(self, point_3d: np.ndarray, image_coord: Tuple[int, int], label: str = ""):
        self.point_3d = point_3d  # [x, y, z] in camera space
        self.image_coord = image_coord  # (u, v) in image
        self.label = label


class MeasurementCalibration:
    """Handles scale calibration and real-world dimension computation."""
    
    def __init__(self):
        self.scale_factor = 1.0  # multiplier to convert from camera units to meters
        self.calibration_type = None  # "aruco" or "manual"
        self.reference_points = []  # List of CalibrationPoint objects
        self.aruco_dict = cv2.getPredefinedDictionary(cv2.aruco.getPredefinedDictionary_7X7_50)
        self.aruco_detector = cv2.aruco.ArucoDetector(self.aruco_dict)
    
    def detect_aruco_markers(self, image: np.ndarray) -> dict:
        """
        Detect ArUco markers in image for automatic scale calibration.
        
        Returns: {
            "detected": bool,
            "marker_id": int,
            "corners": np.ndarray,  # Marker corners in image
            "distance_pixels": float,  # Marker edge length in pixels
        }
        """
        corners, ids, rejected = self.aruco_detector.detectMarkers(image)
        
        result = {"detected": False}
        
        if ids is not None and len(ids) > 0:
            # Use first detected marker
            marker_corners = corners[0][0]
            marker_id = ids[0][0]
            
            # Calculate marker size in pixels (diagonal for robustness)
            distances = []
            for i in range(4):
                p1 = marker_corners[i]
                p2 = marker_corners[(i + 1) % 4]
                dist = np.linalg.norm(p2 - p1)
                distances.append(dist)
            
            avg_edge_pixels = np.mean(distances)
            
            result = {
                "detected": True,
                "marker_id": int(marker_id),
                "corners": marker_corners,
                "distance_pixels": float(avg_edge_pixels),
            }
        
        return result
    
    def calibrate_from_aruco(
        self, 
        image: np.ndarray, 
        marker_size_key: str = "10cm",
        focal_length: float = 1200.0,
        image_distance_mm: float = 100.0,
    ) -> bool:
        """
        Calibrate scale using ArUco marker detection.
        
        Args:
            image: RGB or BGR image
            marker_size_key: Key from ARUCO_MARKER_SIZES dict (e.g., "10cm")
            focal_length: Camera focal length in pixels
            image_distance_mm: Distance from camera to marker (in mm, estimated)
        
        Returns: True if calibration successful
        """
        detection = self.detect_aruco_markers(image)
        
        if not detection["detected"]:
            return False
        
        marker_size_real = ARUCO_MARKER_SIZES.get(marker_size_key, 0.1)  # meters
        marker_size_pixels = detection["distance_pixels"]
        
        # Compute scale: pixels-to-meters conversion
        # scale_factor = (real_size_meters) / (size_in_pixels)
        self.scale_factor = marker_size_real / marker_size_pixels
        self.calibration_type = "aruco"
        
        return True
    
    def add_manual_reference_point(
        self,
        point_3d: np.ndarray,
        image_coord: Tuple[int, int],
        label: str = ""
    ):
        """Add a manually selected calibration point."""
        self.reference_points.append(
            CalibrationPoint(point_3d, image_coord, label)
        )
    
    def calibrate_from_manual_distance(
        self,
        distance_real_meters: float
    ) -> bool:
        """
        Calibrate using two manually selected reference points.
        
        Args:
            distance_real_meters: Known distance between the two points in meters
        
        Returns: True if calibration successful
        """
        if len(self.reference_points) < 2:
            return False
        
        # Use first two points
        p1 = self.reference_points[0].point_3d
        p2 = self.reference_points[1].point_3d
        
        # Distance in camera space
        distance_camera_units = np.linalg.norm(p2 - p1)
        
        if distance_camera_units < 1e-6:
            return False
        
        # Scale factor
        self.scale_factor = distance_real_meters / distance_camera_units
        self.calibration_type = "manual"
        
        return True
    
    def scale_point_cloud(self, points_3d: np.ndarray) -> np.ndarray:
        """Apply calibration scale to point cloud."""
        return points_3d * self.scale_factor
    
    def get_bounding_box_dimensions(self, points_3d: np.ndarray) -> dict:
        """
        Compute real-world dimensions of point cloud.
        
        Returns: {
            "width": float,  # X-axis extent in meters
            "height": float,  # Y-axis extent in meters
            "depth": float,  # Z-axis extent in meters
            "volume": float,  # Approximate volume in cubic meters
        }
        """
        scaled_points = self.scale_point_cloud(points_3d)
        
        if len(scaled_points) == 0:
            return {"width": 0, "height": 0, "depth": 0, "volume": 0}
        
        mins = np.min(scaled_points, axis=0)
        maxs = np.max(scaled_points, axis=0)
        dims = maxs - mins
        
        return {
            "width": float(dims[0]),
            "height": float(dims[1]),
            "depth": float(dims[2]),
            "volume": float(np.prod(dims)),
        }
    
    def measure_distance(self, point1_3d: np.ndarray, point2_3d: np.ndarray) -> float:
        """Measure real-world distance between two 3D points in meters."""
        p1_scaled = point1_3d * self.scale_factor
        p2_scaled = point2_3d * self.scale_factor
        return float(np.linalg.norm(p2_scaled - p1_scaled))
    
    def export_calibration(self) -> dict:
        """Export calibration parameters for saving/restoration."""
        return {
            "scale_factor": float(self.scale_factor),
            "calibration_type": self.calibration_type,
            "reference_count": len(self.reference_points),
        }
    
    def import_calibration(self, config: dict):
        """Restore calibration from saved parameters."""
        self.scale_factor = config.get("scale_factor", 1.0)
        self.calibration_type = config.get("calibration_type")
