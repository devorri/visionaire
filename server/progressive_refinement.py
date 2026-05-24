"""
Progressive point cloud refinement system.

Allows incremental improvement of scans by:
1. Storing intermediate point clouds
2. Merging new frames with existing point clouds
3. Deduplicating points
4. Updating quality metrics
"""

import numpy as np
from typing import Optional, Tuple
import json


class PointCloudFrame:
    """A single point cloud frame with metadata."""
    
    def __init__(
        self,
        points_3d: np.ndarray,
        colors: np.ndarray,
        frame_id: int,
        timestamp: float,
        source: str = "camera",
    ):
        self.points_3d = points_3d  # [N, 3] array
        self.colors = colors  # [N, 3] or [N, 4] array (RGB or RGBA)
        self.frame_id = frame_id
        self.timestamp = timestamp
        self.source = source  # "camera", "meshy", "colmap", etc.
        self.point_count = len(points_3d) if points_3d is not None else 0
    
    def to_dict(self) -> dict:
        """Serialize metadata only (not heavy arrays)."""
        return {
            "frame_id": self.frame_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "point_count": self.point_count,
        }


class ProgressivePointCloudRefiner:
    """Manages incremental point cloud refinement."""
    
    def __init__(self, max_frames: int = 50, dedup_radius_mm: float = 10.0):
        self.frames: list[PointCloudFrame] = []
        self.merged_cloud: Optional[np.ndarray] = None
        self.merged_colors: Optional[np.ndarray] = None
        self.max_frames = max_frames
        self.dedup_radius_mm = dedup_radius_mm  # For deduplication
        self.quality_history: list[dict] = []
    
    def add_frame(
        self,
        points_3d: np.ndarray,
        colors: np.ndarray,
        frame_id: int,
        timestamp: float,
        source: str = "camera",
    ) -> dict:
        """
        Add a new point cloud frame and merge with existing cloud.
        
        Returns: {
            "frame_id": int,
            "merged_point_count": int,
            "new_points_added": int,
            "duplicates_removed": int,
        }
        """
        frame = PointCloudFrame(points_3d, colors, frame_id, timestamp, source)
        self.frames.append(frame)
        
        # Keep only recent frames
        if len(self.frames) > self.max_frames:
            self.frames = self.frames[-self.max_frames:]
        
        # Merge with existing cloud
        result = self._merge_frame(points_3d, colors)
        
        return result
    
    def _merge_frame(
        self,
        new_points: np.ndarray,
        new_colors: np.ndarray,
    ) -> dict:
        """Merge new frame with existing point cloud."""
        if self.merged_cloud is None or len(self.merged_cloud) == 0:
            self.merged_cloud = new_points.copy()
            self.merged_colors = new_colors.copy()
            return {
                "merged_point_count": len(new_points),
                "new_points_added": len(new_points),
                "duplicates_removed": 0,
            }
        
        # Combine clouds
        combined_points = np.vstack([self.merged_cloud, new_points])
        combined_colors = np.vstack([self.merged_colors, new_colors])
        
        # Remove duplicates (points within dedup_radius)
        deduplicated_points, keep_indices = self._remove_duplicate_points(
            combined_points,
            self.dedup_radius_mm / 1000.0,  # Convert mm to meters
        )
        
        duplicates_removed = len(combined_points) - len(deduplicated_points)
        new_points_added = len(new_points) - len(
            np.where(keep_indices[len(self.merged_cloud):] == False)[0]
        )
        
        self.merged_cloud = deduplicated_points
        self.merged_colors = combined_colors[keep_indices]
        
        return {
            "merged_point_count": len(deduplicated_points),
            "new_points_added": max(0, new_points_added),
            "duplicates_removed": duplicates_removed,
        }
    
    def _remove_duplicate_points(
        self,
        points: np.ndarray,
        radius: float,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Remove duplicate points within radius using spatial clustering.
        
        Returns: (deduplicated_points, keep_indices_bool_array)
        """
        if len(points) == 0:
            return points, np.array([], dtype=bool)
        
        # Use simple grid-based deduplication for speed
        grid_size = radius
        keep = np.ones(len(points), dtype=bool)
        
        # Group points by grid cell
        grid_indices = (points / grid_size).astype(int)
        
        for i in range(len(points)):
            if not keep[i]:
                continue
            
            # Find all points in same or adjacent cells
            current_grid = grid_indices[i]
            for j in range(i + 1, len(points)):
                if not keep[j]:
                    continue
                
                # Check if within same cell or adjacent
                if np.allclose(grid_indices[j], current_grid, atol=1):
                    # Check actual distance
                    dist = np.linalg.norm(points[i] - points[j])
                    if dist < radius:
                        keep[j] = False
        
        return points[keep], keep
    
    def apply_registration(
        self,
        new_points: np.ndarray,
        prev_points: np.ndarray,
    ) -> np.ndarray:
        """
        Align new point cloud with previous using Procrustes method.
        
        Simple rigid transformation (rotation + translation) without scaling.
        For better results, consider using Open3D's ICP registration.
        """
        if len(new_points) < 3 or len(prev_points) < 3:
            return new_points
        
        # Center both clouds
        new_center = np.mean(new_points, axis=0)
        prev_center = np.mean(prev_points, axis=0)
        
        new_centered = new_points - new_center
        prev_centered = prev_points - prev_center
        
        # Compute SVD for rotation
        try:
            H = new_centered.T @ prev_centered
            U, _, Vt = np.linalg.svd(H)
            R = Vt.T @ U.T
            
            # Ensure proper rotation (det = 1)
            if np.linalg.det(R) < 0:
                Vt[-1, :] *= -1
                R = Vt.T @ U.T
            
            # Apply rotation and translation
            aligned = (R @ new_centered.T).T + prev_center
            return aligned
        except:
            return new_points  # Return unaligned if SVD fails
    
    def get_merged_cloud(self) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """Get current merged point cloud and colors."""
        return self.merged_cloud, self.merged_colors
    
    def get_statistics(self) -> dict:
        """Get statistics about the merged cloud."""
        if self.merged_cloud is None or len(self.merged_cloud) == 0:
            return {
                "total_frames": 0,
                "total_points": 0,
                "avg_points_per_frame": 0,
                "coverage": 0,
            }
        
        total_points = len(self.merged_cloud)
        total_frames = len(self.frames)
        avg_points = total_points / total_frames if total_frames > 0 else 0
        
        # Estimate coverage based on point spread
        if total_points >= 10:
            # Higher density = better coverage estimate
            std_dev = np.std(np.linalg.norm(self.merged_cloud, axis=1))
            coverage = min(100, int((std_dev / np.max(np.abs(self.merged_cloud)) * 100)))
        else:
            coverage = 0
        
        return {
            "total_frames": total_frames,
            "total_points": total_points,
            "avg_points_per_frame": float(avg_points),
            "coverage": int(max(0, coverage)),
        }
    
    def reset(self):
        """Clear all frames and merged cloud."""
        self.frames.clear()
        self.merged_cloud = None
        self.merged_colors = None
        self.quality_history.clear()
    
    def export_state(self) -> dict:
        """Export refinement state for persistence."""
        return {
            "frame_count": len(self.frames),
            "merged_point_count": len(self.merged_cloud) if self.merged_cloud is not None else 0,
            "frames_metadata": [f.to_dict() for f in self.frames],
            "statistics": self.get_statistics(),
        }
