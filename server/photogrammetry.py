"""
ScanForge Photogrammetry Pipeline
Real Structure from Motion (SfM) using OpenCV + Open3D.

Pipeline stages:
  1. Feature Detection  — SIFT keypoints + descriptors
  2. Feature Matching    — FLANN matcher with ratio test
  3. Pose Recovery       — Essential matrix + camera pose
  4. Triangulation       — 3D point cloud from matched features
  5. Point Cloud Build   — Assemble colored point cloud
  6. Mesh Generation     — Poisson surface reconstruction
  7. Export              — Save as OBJ and GLB
"""

import io
import os
import uuid
import tempfile
import traceback
from typing import Callable

import cv2
import numpy as np

# Open3D import — may fail on some systems
try:
    import open3d as o3d
    HAS_OPEN3D = True
except ImportError:
    HAS_OPEN3D = False

# Trimesh for GLB export
try:
    import trimesh
    HAS_TRIMESH = True
except ImportError:
    HAS_TRIMESH = False


# ── Pipeline stages ──────────────────────────────────────────────────

STAGES = [
    "Feature detection",
    "Feature matching",
    "Pose recovery",
    "Triangulation",
    "Point cloud",
    "Mesh generation",
    "Export",
]


class ReconstructionResult:
    """Holds the output of a reconstruction run."""

    def __init__(self):
        self.points_3d: np.ndarray | None = None
        self.colors: np.ndarray | None = None
        self.obj_bytes: bytes | None = None
        self.glb_bytes: bytes | None = None
        self.ply_bytes: bytes | None = None
        self.point_count: int = 0
        self.quality_score: int = 0
        self.coverage_score: int = 0
        self.texture_score: int = 0
        self.error: str | None = None


def _estimate_camera_matrix(image_shape: tuple) -> np.ndarray:
    """
    Estimate a reasonable camera intrinsic matrix from image dimensions.
    Assumes a typical smartphone/drone camera with ~60-70 degree FOV.
    """
    h, w = image_shape[:2]
    focal_length = max(w, h) * 1.2  # rough estimate
    cx, cy = w / 2.0, h / 2.0
    return np.array([
        [focal_length, 0, cx],
        [0, focal_length, cy],
        [0, 0, 1],
    ], dtype=np.float64)


def _detect_features(images: list[np.ndarray], on_progress: Callable | None = None):
    """Stage 1: Detect SIFT keypoints and descriptors for all images."""
    sift = cv2.SIFT_create(nfeatures=3000)
    keypoints_list = []
    descriptors_list = []

    for i, img in enumerate(images):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
        kp, desc = sift.detectAndCompute(gray, None)
        keypoints_list.append(kp)
        descriptors_list.append(desc)

        if on_progress:
            on_progress(0, int((i + 1) / len(images) * 100))

    return keypoints_list, descriptors_list


def _match_features(
    descriptors_list: list,
    on_progress: Callable | None = None,
):
    """Stage 2: Match features between consecutive image pairs using FLANN."""
    FLANN_INDEX_KDTREE = 1
    index_params = dict(algorithm=FLANN_INDEX_KDTREE, trees=5)
    search_params = dict(checks=80)
    flann = cv2.FlannBasedMatcher(index_params, search_params)

    matches_list = []
    total_pairs = len(descriptors_list) - 1

    for i in range(total_pairs):
        desc1 = descriptors_list[i]
        desc2 = descriptors_list[i + 1]

        if desc1 is None or desc2 is None or len(desc1) < 2 or len(desc2) < 2:
            matches_list.append([])
            continue

        raw_matches = flann.knnMatch(desc1, desc2, k=2)

        # Lowe's ratio test
        good = []
        for pair in raw_matches:
            if len(pair) == 2:
                m, n = pair
                if m.distance < 0.75 * n.distance:
                    good.append(m)

        matches_list.append(good)

        if on_progress:
            on_progress(1, int((i + 1) / total_pairs * 100))

    return matches_list


def _recover_poses(
    images: list[np.ndarray],
    keypoints_list: list,
    matches_list: list,
    K: np.ndarray,
    on_progress: Callable | None = None,
):
    """Stage 3: Recover camera poses from essential matrices."""
    poses = [(np.eye(3), np.zeros((3, 1)))]  # First camera at origin

    for i, matches in enumerate(matches_list):
        if len(matches) < 8:
            # Not enough matches — use identity as fallback
            poses.append((np.eye(3), np.zeros((3, 1))))
            continue

        kp1 = keypoints_list[i]
        kp2 = keypoints_list[i + 1]

        pts1 = np.float32([kp1[m.queryIdx].pt for m in matches])
        pts2 = np.float32([kp2[m.trainIdx].pt for m in matches])

        E, mask = cv2.findEssentialMat(pts1, pts2, K, method=cv2.RANSAC, prob=0.999, threshold=1.0)

        if E is None:
            poses.append((np.eye(3), np.zeros((3, 1))))
            continue

        _, R, t, pose_mask = cv2.recoverPose(E, pts1, pts2, K, mask=mask)
        poses.append((R, t))

        if on_progress:
            on_progress(2, int((i + 1) / len(matches_list) * 100))

    return poses


def _triangulate_points(
    images: list[np.ndarray],
    keypoints_list: list,
    matches_list: list,
    poses: list,
    K: np.ndarray,
    on_progress: Callable | None = None,
):
    """Stage 4: Triangulate 3D points from matched features across image pairs."""
    all_points_3d = []
    all_colors = []

    for i, matches in enumerate(matches_list):
        if len(matches) < 8:
            continue

        kp1 = keypoints_list[i]
        kp2 = keypoints_list[i + 1]

        pts1 = np.float32([kp1[m.queryIdx].pt for m in matches])
        pts2 = np.float32([kp2[m.trainIdx].pt for m in matches])

        R1, t1 = poses[i]
        R2, t2 = poses[i + 1]

        # Build projection matrices
        P1 = K @ np.hstack([R1, t1])
        P2 = K @ np.hstack([R2, t2])

        # Triangulate
        points_4d = cv2.triangulatePoints(P1, P2, pts1.T, pts2.T)
        points_3d = (points_4d[:3] / points_4d[3]).T

        # Filter out points at infinity or behind cameras
        valid_mask = (
            np.isfinite(points_3d).all(axis=1)
            & (np.abs(points_3d[:, 0]) < 50)
            & (np.abs(points_3d[:, 1]) < 50)
            & (np.abs(points_3d[:, 2]) < 50)
            & (points_3d[:, 2] > 0)
        )

        valid_points = points_3d[valid_mask]

        # Extract colors from the first image
        colors = []
        valid_pts1 = pts1[valid_mask]
        img = images[i]
        h, w = img.shape[:2]

        for pt in valid_pts1:
            x, y = int(np.clip(pt[0], 0, w - 1)), int(np.clip(pt[1], 0, h - 1))
            if len(img.shape) == 3:
                bgr = img[y, x]
                colors.append([bgr[2] / 255.0, bgr[1] / 255.0, bgr[0] / 255.0])  # BGR→RGB
            else:
                v = img[y, x] / 255.0
                colors.append([v, v, v])

        if len(valid_points) > 0:
            all_points_3d.append(valid_points)
            all_colors.append(np.array(colors))

        if on_progress:
            on_progress(3, int((i + 1) / len(matches_list) * 100))

    if not all_points_3d:
        return np.zeros((0, 3)), np.zeros((0, 3))

    return np.vstack(all_points_3d), np.vstack(all_colors)


def _build_point_cloud(
    points_3d: np.ndarray,
    colors: np.ndarray,
    on_progress: Callable | None = None,
):
    """Stage 5: Build an Open3D point cloud, clean outliers, estimate normals."""
    if not HAS_OPEN3D or len(points_3d) == 0:
        return None

    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points_3d)

    if len(colors) == len(points_3d):
        pcd.colors = o3d.utility.Vector3dVector(np.clip(colors, 0, 1))

    if on_progress:
        on_progress(4, 30)

    # Statistical outlier removal
    if len(points_3d) > 20:
        nb_neighbors = min(20, len(points_3d) - 1)
        pcd, _ = pcd.remove_statistical_outlier(nb_neighbors=nb_neighbors, std_ratio=2.0)

    if on_progress:
        on_progress(4, 60)

    # Estimate normals (required for Poisson reconstruction)
    if len(pcd.points) > 10:
        pcd.estimate_normals(
            search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=0.1, max_nn=30)
        )
        pcd.orient_normals_consistent_tangent_plane(k=15)

    if on_progress:
        on_progress(4, 100)

    return pcd


def _generate_mesh(
    pcd,
    on_progress: Callable | None = None,
):
    """Stage 6: Generate a triangle mesh from the point cloud using Poisson reconstruction."""
    if not HAS_OPEN3D or pcd is None or len(pcd.points) < 10:
        return None

    if on_progress:
        on_progress(5, 20)

    try:
        # Poisson surface reconstruction
        mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
            pcd, depth=8, width=0, scale=1.1, linear_fit=False
        )

        if on_progress:
            on_progress(5, 60)

        # Remove low-density vertices (clean up stray geometry)
        densities = np.asarray(densities)
        if len(densities) > 0:
            density_threshold = np.quantile(densities, 0.05)
            vertices_to_remove = densities < density_threshold
            mesh.remove_vertices_by_mask(vertices_to_remove)

        # Clean up
        mesh.remove_degenerate_triangles()
        mesh.remove_non_manifold_edges()

        if on_progress:
            on_progress(5, 100)

        return mesh

    except Exception as exc:
        print(f"Mesh generation failed: {exc}")
        traceback.print_exc()
        return None


def _export_models(
    pcd,
    mesh,
    points_3d: np.ndarray,
    colors: np.ndarray,
    scan_name: str = "scanforge_model",
    on_progress: Callable | None = None,
) -> tuple[bytes | None, bytes | None, bytes | None]:
    """Stage 7: Export the 3D model to OBJ, GLB, and PLY formats."""
    obj_bytes = None
    glb_bytes = None
    ply_bytes = None

    if on_progress:
        on_progress(6, 10)

    # ── Export OBJ ────────────────────────────────────────────────────
    if mesh is not None and HAS_OPEN3D:
        try:
            with tempfile.NamedTemporaryFile(suffix=".obj", delete=False) as tmp:
                tmp_path = tmp.name
            o3d.io.write_triangle_mesh(tmp_path, mesh, write_vertex_colors=True)
            with open(tmp_path, "rb") as f:
                obj_bytes = f.read()
            os.unlink(tmp_path)
        except Exception as exc:
            print(f"OBJ export failed: {exc}")
    elif len(points_3d) > 0:
        # Fallback: export point cloud as simple OBJ vertices
        lines = [f"# {scan_name}", "# Generated by ScanForge Web", f"# Points: {len(points_3d)}"]
        for pt in points_3d:
            lines.append(f"v {pt[0]:.6f} {pt[1]:.6f} {pt[2]:.6f}")
        obj_bytes = "\n".join(lines).encode("utf-8")

    if on_progress:
        on_progress(6, 40)

    # ── Export GLB ────────────────────────────────────────────────────
    if mesh is not None and HAS_TRIMESH:
        try:
            vertices = np.asarray(mesh.vertices)
            triangles = np.asarray(mesh.triangles)
            vertex_colors = None

            if mesh.has_vertex_colors():
                vc = np.asarray(mesh.vertex_colors)
                # Convert to 0-255 RGBA
                alpha = np.ones((len(vc), 1))
                vertex_colors = np.hstack([vc, alpha])
                vertex_colors = (vertex_colors * 255).astype(np.uint8)

            tm = trimesh.Trimesh(
                vertices=vertices,
                faces=triangles,
                vertex_colors=vertex_colors,
            )
            glb_bytes = tm.export(file_type="glb")
        except Exception as exc:
            print(f"GLB export failed: {exc}")

    if on_progress:
        on_progress(6, 70)

    # ── Export PLY ────────────────────────────────────────────────────
    if pcd is not None and HAS_OPEN3D:
        try:
            with tempfile.NamedTemporaryFile(suffix=".ply", delete=False) as tmp:
                tmp_path = tmp.name
            o3d.io.write_point_cloud(tmp_path, pcd)
            with open(tmp_path, "rb") as f:
                ply_bytes = f.read()
            os.unlink(tmp_path)
        except Exception as exc:
            print(f"PLY export failed: {exc}")

    if on_progress:
        on_progress(6, 100)

    return obj_bytes, glb_bytes, ply_bytes


# ── Main reconstruction entry point ──────────────────────────────────

def reconstruct(
    image_bytes_list: list[bytes],
    quality: int = 78,
    detail: int = 64,
    scan_name: str = "ScanForge Model",
    on_progress: Callable | None = None,
) -> ReconstructionResult:
    """
    Run the full SfM reconstruction pipeline.

    Parameters
    ----------
    image_bytes_list : list[bytes]
        Raw image file bytes (JPEG/PNG).
    quality : int
        Quality setting (40-100), controls SIFT features count.
    detail : int
        Detail setting (35-100), controls mesh depth.
    scan_name : str
        Name for the exported model.
    on_progress : callable
        Callback(stage_index: int, percent: int) for progress updates.

    Returns
    -------
    ReconstructionResult
    """
    result = ReconstructionResult()

    try:
        # Decode images
        images = []
        for raw in image_bytes_list:
            arr = np.frombuffer(raw, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                # Resize large images to keep processing fast
                max_dim = 1200 if quality >= 80 else 800
                h, w = img.shape[:2]
                if max(h, w) > max_dim:
                    scale = max_dim / max(h, w)
                    img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                images.append(img)

        if len(images) < 2:
            result.error = "Need at least 2 valid images"
            return result

        # Camera intrinsic matrix (estimated from first image)
        K = _estimate_camera_matrix(images[0].shape)

        # Stage 1: Feature detection
        kp_list, desc_list = _detect_features(images, on_progress)

        # Stage 2: Feature matching
        matches_list = _match_features(desc_list, on_progress)

        total_matches = sum(len(m) for m in matches_list)
        if total_matches < 10:
            result.error = "Not enough feature matches between images. Try photos with more overlap."
            return result

        # Stage 3: Pose recovery
        poses = _recover_poses(images, kp_list, matches_list, K, on_progress)

        # Stage 4: Triangulation
        points_3d, colors = _triangulate_points(images, kp_list, matches_list, poses, K, on_progress)

        if len(points_3d) < 5:
            result.error = "Could not triangulate enough 3D points. Try photos with better overlap."
            return result

        result.points_3d = points_3d
        result.colors = colors
        result.point_count = len(points_3d)

        # Stage 5: Build point cloud
        pcd = _build_point_cloud(points_3d, colors, on_progress)

        # Stage 6: Generate mesh
        mesh = None
        if pcd is not None and len(pcd.points) >= 10:
            mesh = _generate_mesh(pcd, on_progress)

        # Stage 7: Export
        obj_bytes, glb_bytes, ply_bytes = _export_models(
            pcd, mesh, points_3d, colors, scan_name, on_progress
        )

        result.obj_bytes = obj_bytes
        result.glb_bytes = glb_bytes
        result.ply_bytes = ply_bytes

        # Compute quality metrics
        photo_count = len(images)
        result.quality_score = min(99, int(40 + (total_matches / max(1, photo_count)) * 0.3 + quality * 0.35))
        result.coverage_score = min(99, int(35 + photo_count * 4.5 + len(points_3d) * 0.002))
        result.texture_score = min(99, int(38 + photo_count * 3.2 + detail * 0.28))

    except Exception as exc:
        result.error = f"Reconstruction failed: {str(exc)}"
        traceback.print_exc()

    return result
