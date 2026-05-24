"""
Advanced texture mapping and blending for photorealistic 3D models.

Implements:
1. Multi-view texture projection
2. Seamless texture blending across overlapping images
3. Automatic color/exposure correction
4. High-resolution texture atlasing
5. Normal map generation from geometry
"""

import numpy as np
import cv2
from typing import Tuple, Optional, List
import tempfile
import os


class TextureBlender:
    """Blends multiple overlapping textures for seamless appearance."""
    
    def __init__(self, atlas_size: int = 4096, blend_width: int = 50):
        self.atlas_size = atlas_size  # Texture atlas resolution
        self.blend_width = blend_width  # Feather width for seamless blending
        self.texture_atlas = np.ones((atlas_size, atlas_size, 3), dtype=np.uint8) * 255
        self.weight_atlas = np.zeros((atlas_size, atlas_size, 1), dtype=np.float32)
    
    def _estimate_illumination(self, image: np.ndarray) -> Tuple[float, float]:
        """Estimate image brightness and contrast for correction."""
        # Convert to grayscale
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
        
        # Calculate mean brightness
        brightness = np.mean(gray) / 255.0
        
        # Calculate contrast (std dev)
        contrast = np.std(gray) / 127.0
        
        return brightness, contrast
    
    def _correct_color_exposure(
        self,
        image: np.ndarray,
        target_brightness: float = 0.5,
        target_contrast: float = 0.6,
    ) -> np.ndarray:
        """Automatically correct color and exposure to match target."""
        current_brightness, current_contrast = self._estimate_illumination(image)
        
        # Avoid division by zero
        if current_contrast < 0.1:
            current_contrast = 0.1
        
        # Scale and shift
        image_float = image.astype(np.float32)
        
        # Adjust contrast
        contrast_scale = target_contrast / current_contrast
        image_float = image_float * contrast_scale
        
        # Adjust brightness
        current_mean = np.mean(image_float)
        target_mean = target_brightness * 255.0
        brightness_shift = target_mean - current_mean
        image_float = image_float + brightness_shift
        
        # Clip and convert back
        image_float = np.clip(image_float, 0, 255)
        return image_float.astype(np.uint8)
    
    def add_textured_region(
        self,
        image: np.ndarray,
        uv_coords: np.ndarray,  # [N, 2] in [0, 1] range
        blend_mode: str = "blend",  # "blend" or "replace"
    ):
        """
        Add textured region to atlas with automatic blending.
        
        Args:
            image: Source image for texture
            uv_coords: UV coordinates for pixels in [0, 1] range
            blend_mode: How to blend (blend=average, replace=overwrite)
        """
        if len(uv_coords) == 0:
            return
        
        # Color correction
        image_corrected = self._correct_color_exposure(image)
        
        # Convert UV to pixel coordinates in atlas
        atlas_coords = (uv_coords * (self.atlas_size - 1)).astype(int)
        atlas_coords = np.clip(atlas_coords, 0, self.atlas_size - 1)
        
        # Sample colors from source image
        u_src = (uv_coords[:, 0] * (image.shape[1] - 1)).astype(int)
        v_src = (uv_coords[:, 1] * (image.shape[0] - 1)).astype(int)
        
        u_src = np.clip(u_src, 0, image.shape[1] - 1)
        v_src = np.clip(v_src, 0, image.shape[0] - 1)
        
        colors = image_corrected[v_src, u_src]
        
        # Blend with atlas
        for i, (x, y) in enumerate(atlas_coords):
            if blend_mode == "blend":
                # Weighted average
                current = self.texture_atlas[y, x].astype(np.float32)
                new_color = colors[i].astype(np.float32)
                
                current_weight = self.weight_atlas[y, x, 0]
                new_weight = 1.0
                
                total_weight = current_weight + new_weight
                blended = (current * current_weight + new_color * new_weight) / total_weight
                
                self.texture_atlas[y, x] = blended.astype(np.uint8)
                self.weight_atlas[y, x, 0] = min(total_weight, 5.0)  # Cap weight
            else:
                self.texture_atlas[y, x] = colors[i]
                self.weight_atlas[y, x, 0] = 1.0
    
    def get_blended_atlas(self) -> np.ndarray:
        """Get final blended texture atlas."""
        return self.texture_atlas.copy()
    
    def apply_bilateral_filter(self, kernel_size: int = 5) -> np.ndarray:
        """
        Apply bilateral filter for seamless transitions.
        Reduces artifacts while preserving edges.
        """
        filtered = cv2.bilateralFilter(
            self.texture_atlas,
            kernel_size,
            sigmaColor=15,
            sigmaSpace=15,
        )
        return filtered


class NormalMapGenerator:
    """Generates normal maps from geometry for realistic lighting."""
    
    @staticmethod
    def from_point_cloud(
        points_3d: np.ndarray,
        grid_size: int = 100,
    ) -> np.ndarray:
        """
        Generate normal map from point cloud.
        
        Returns: [H, W, 3] normal map in range [-1, 1]
        """
        if len(points_3d) < 100:
            # Fallback to flat normals
            return np.ones((grid_size, grid_size, 3), dtype=np.float32) * [0, 0, 1]
        
        # Create a grid-based normal estimation
        # Group points into grid cells
        normals = np.zeros((grid_size, grid_size, 3), dtype=np.float32)
        
        # Simple approach: use local PCA to estimate normals
        x_min, x_max = points_3d[:, 0].min(), points_3d[:, 0].max()
        y_min, y_max = points_3d[:, 1].min(), points_3d[:, 1].max()
        
        for i in range(grid_size):
            for j in range(grid_size):
                # Cell boundaries
                x_lo = x_min + (x_max - x_min) * i / grid_size
                x_hi = x_min + (x_max - x_min) * (i + 1) / grid_size
                y_lo = y_min + (y_max - y_min) * j / grid_size
                y_hi = y_min + (y_max - y_min) * (j + 1) / grid_size
                
                # Find points in cell
                mask = (
                    (points_3d[:, 0] >= x_lo) & (points_3d[:, 0] < x_hi) &
                    (points_3d[:, 1] >= y_lo) & (points_3d[:, 1] < y_hi)
                )
                
                if np.sum(mask) >= 3:
                    # Fit plane to points
                    cell_points = points_3d[mask]
                    
                    # Center points
                    center = np.mean(cell_points, axis=0)
                    centered = cell_points - center
                    
                    # SVD for PCA
                    try:
                        _, _, Vt = np.linalg.svd(centered)
                        # Normal is smallest eigenvector
                        normal = Vt[-1]
                        normal = normal / (np.linalg.norm(normal) + 1e-8)
                    except:
                        normal = np.array([0, 0, 1])
                else:
                    normal = np.array([0, 0, 1])
                
                normals[j, i] = normal
        
        return normals
    
    @staticmethod
    def from_mesh_geometry(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
        """Generate normals from triangle mesh."""
        # Calculate face normals
        v0 = vertices[faces[:, 0]]
        v1 = vertices[faces[:, 1]]
        v2 = vertices[faces[:, 2]]
        
        # Cross product for normals
        e1 = v1 - v0
        e2 = v2 - v0
        face_normals = np.cross(e1, e2)
        
        # Normalize
        face_normals = face_normals / (np.linalg.norm(face_normals, axis=1, keepdims=True) + 1e-8)
        
        return face_normals


class TextureProjector:
    """Projects 2D images onto 3D geometry."""
    
    @staticmethod
    def project_image_to_uv(
        image: np.ndarray,
        camera_matrix: np.ndarray,
        camera_pose: Tuple[np.ndarray, np.ndarray],
        points_3d: np.ndarray,
        image_height: int = 512,
        image_width: int = 512,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Project 3D points onto image plane and get UV coordinates.
        
        Returns: (uv_coords, visibility) where:
            - uv_coords: [N, 2] in [0, 1] range
            - visibility: [N] boolean, True if point is visible in image
        """
        R, t = camera_pose
        
        # Transform points to camera space
        points_cam = R @ points_3d.T + t
        
        # Project to image plane
        points_2d = camera_matrix @ points_cam
        points_2d = points_2d[:2] / points_2d[2]
        
        # Check visibility (in front of camera, within image bounds)
        depth = points_cam[2]
        visibility = (
            (depth > 0.01) &
            (points_2d[0] >= 0) & (points_2d[0] < image_width) &
            (points_2d[1] >= 0) & (points_2d[1] < image_height)
        )
        
        # Normalize to [0, 1]
        uv_coords = points_2d.T / np.array([image_width, image_height])
        uv_coords = np.clip(uv_coords, 0, 1)
        
        return uv_coords, visibility
    
    @staticmethod
    def blend_multiple_views(
        images: List[np.ndarray],
        camera_matrices: List[np.ndarray],
        camera_poses: List[Tuple[np.ndarray, np.ndarray]],
        points_3d: np.ndarray,
        atlas_size: int = 4096,
    ) -> np.ndarray:
        """
        Blend multiple view projections for complete coverage.
        
        Returns: Texture atlas [atlas_size, atlas_size, 3]
        """
        blender = TextureBlender(atlas_size=atlas_size)
        
        for image, K, pose in zip(images, camera_matrices, camera_poses):
            # Project points
            uv_coords, visibility = TextureProjector.project_image_to_uv(
                image, K, pose, points_3d[visibility],
                image_height=image.shape[0],
                image_width=image.shape[1],
            )
            
            # Add to atlas
            blender.add_textured_region(image, uv_coords, blend_mode="blend")
        
        # Apply smoothing for seamless transitions
        atlas = blender.get_blended_atlas()
        atlas = blender.apply_bilateral_filter(kernel_size=5)
        
        return atlas


class PhotorealisticMaterialGenerator:
    """Generates PBR materials (Metallic, Roughness, AO) from images."""
    
    @staticmethod
    def extract_roughness_map(image: np.ndarray) -> np.ndarray:
        """
        Estimate roughness map from image highlights/details.
        
        High-detail areas = rough, smooth areas = glossy
        """
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
        
        # High-pass filter to detect details
        blurred = cv2.GaussianBlur(gray, (25, 25), 0)
        high_pass = cv2.absdiff(gray, blurred)
        
        # Normalize to [0, 1]
        roughness = high_pass.astype(np.float32) / 255.0
        
        # Invert: more details = rougher
        roughness = 1.0 - roughness
        
        return roughness
    
    @staticmethod
    def extract_metallic_map(image: np.ndarray) -> np.ndarray:
        """
        Estimate metallic map from saturation.
        
        Low saturation (grays) = metallic, high saturation = non-metallic
        """
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV) if len(image.shape) == 3 else image
        
        # Saturation channel
        saturation = hsv[:, :, 1].astype(np.float32) / 255.0
        
        # Invert: low saturation = metallic
        metallic = 1.0 - saturation
        
        return metallic
    
    @staticmethod
    def generate_ao_map(
        points_3d: np.ndarray,
        normals: np.ndarray,
        samples: int = 32,
    ) -> np.ndarray:
        """
        Generate ambient occlusion map.
        
        Estimates how occluded each point is.
        """
        if len(points_3d) == 0:
            return np.ones(len(points_3d), dtype=np.float32)
        
        ao = np.ones(len(points_3d), dtype=np.float32)
        
        # Simple AO: check how many nearby points obstruct rays
        for i, (point, normal) in enumerate(zip(points_3d, normals)):
            # Cast rays in hemisphere around normal
            occluded_count = 0
            ray_count = samples
            
            for _ in range(ray_count):
                # Random direction in hemisphere
                theta = np.random.uniform(0, 2 * np.pi)
                phi = np.random.uniform(0, np.pi / 2)
                
                ray_dir = np.array([
                    np.sin(phi) * np.cos(theta),
                    np.sin(phi) * np.sin(theta),
                    np.cos(phi),
                ])
                
                # Check if ray hits nearby points
                ray_start = point + normal * 0.01
                ray_end = ray_start + ray_dir * 0.5
                
                # Simple check: count points near ray
                distances = np.linalg.norm(
                    points_3d - (ray_start + ray_dir * 0.25),
                    axis=1,
                )
                
                if np.any(distances < 0.1):
                    occluded_count += 1
            
            ao[i] = 1.0 - (occluded_count / ray_count) * 0.8
        
        return ao


class PhoturealisticRenderer:
    """Assembles all components for photorealistic 3D model."""
    
    def __init__(self):
        self.texture_atlas = None
        self.normal_map = None
        self.roughness_map = None
        self.metallic_map = None
        self.ao_map = None
    
    def generate_complete_materials(
        self,
        images: List[np.ndarray],
        camera_matrices: List[np.ndarray],
        camera_poses: List[Tuple[np.ndarray, np.ndarray]],
        points_3d: np.ndarray,
        vertices: np.ndarray,
        faces: np.ndarray,
        atlas_size: int = 2048,
    ) -> dict:
        """
        Generate complete material set for photorealistic rendering.
        
        Returns: {
            "albedo": texture_atlas,
            "normal": normal_map,
            "roughness": roughness_map,
            "metallic": metallic_map,
            "ao": ao_map,
        }
        """
        # Generate albedo texture
        self.texture_atlas = TextureProjector.blend_multiple_views(
            images, camera_matrices, camera_poses, points_3d, atlas_size
        )
        
        # Generate normal map
        self.normal_map = NormalMapGenerator.from_mesh_geometry(vertices, faces)
        
        # Generate PBR maps from first image (simplified)
        if len(images) > 0:
            self.roughness_map = PhotorealisticMaterialGenerator.extract_roughness_map(
                images[0]
            )
            self.metallic_map = PhotorealisticMaterialGenerator.extract_metallic_map(
                images[0]
            )
        
        # Generate AO map
        self.ao_map = PhotorealisticMaterialGenerator.generate_ao_map(
            points_3d, self.normal_map[:len(points_3d)]
        )
        
        return {
            "albedo": self.texture_atlas,
            "normal": self.normal_map,
            "roughness": self.roughness_map,
            "metallic": self.metallic_map,
            "ao": self.ao_map,
        }
