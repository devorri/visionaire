# Polycam Web - Feature Implementation Guide

## Overview
This document explains the three major new features added to the Polycam Web system:
1. **Measurement Dimensions** - Real-world scale calibration with bounding box dimensions
2. **Live Camera Streaming** - Real-time continuous scanning from webcam/phone
3. **Progressive Refinement** - Incremental improvement of scans with new frames

---

## 1. Measurement Dimensions & Calibration

### Purpose
Convert point cloud coordinates from camera space to real-world meters, enabling accurate measurement of objects and spaces.

### How It Works

**Two calibration modes:**

#### A) ArUco Marker Detection (Automatic)
- Place a known-size ArUco marker in the scene
- Backend detects marker and calculates scale automatically
- Supports: 5cm, 10cm, 15cm, 20cm, 30cm markers

```javascript
// Frontend usage
import { calibrateWithAruco } from '@/lib/api.js'

// Trigger after uploading photos
const result = await calibrateWithAruco(scanId, '10cm')
// Returns: { calibrated: true, scale_factor: 0.0015, ... }
```

#### B) Manual Reference Distance (User-Specified)
- User identifies two points in the scan
- Provides known distance between them (e.g., "this wall is 3 meters")
- Backend scales entire point cloud

```javascript
import { calibrateManual } from '@/lib/api.js'

// User measures 2 points that are 3 meters apart
await calibrateManual(scanId, 3.0)  // 3 meters
```

### Getting Dimensions

Once calibrated, retrieve real-world dimensions:

```javascript
import { getDimensions } from '@/lib/api.js'

const result = await getDimensions(scanId)
// Returns:
// {
//   "dimensions": {
//     "width": 4.5,      // meters
//     "height": 2.8,     // meters
//     "depth": 6.2,      // meters
//     "volume": 78.1     // cubic meters
//   },
//   "unit": "meters"
// }
```

### Backend Implementation

**File:** `server/measurement_calibration.py`

Key classes:
- `MeasurementCalibration` - Main calibration handler
- `CalibrationPoint` - Represents a point used for manual calibration
- `ARUCO_MARKER_SIZES` - Dict of marker size presets

**Key Methods:**
```python
calibration = MeasurementCalibration()

# ArUco mode
calibration.calibrate_from_aruco(image, marker_size_key="10cm")

# Manual mode
calibration.add_manual_reference_point(point_3d, image_coord)
calibration.calibrate_from_manual_distance(distance_meters=3.0)

# Use calibration
scaled_points = calibration.scale_point_cloud(points_3d)
dimensions = calibration.get_bounding_box_dimensions(points_3d)
distance = calibration.measure_distance(p1, p2)  # In meters
```

### API Endpoints

```
POST   /api/scans/{scan_id}/calibration/aruco
       ?marker_size=10cm
       → Calibrate using ArUco marker from first photo

POST   /api/scans/{scan_id}/calibration/manual
       ?reference_distance_meters=3.0
       → Calibrate using manual reference points

POST   /api/scans/{scan_id}/calibration/reset
       → Clear calibration

GET    /api/scans/{scan_id}/dimensions
       → Get bounding box in real-world meters
```

---

## 2. Live Camera Streaming

### Purpose
Enable real-time continuous scanning - open camera, watch live point cloud build up, stop when satisfied.

### How It Works

**Flow:**
1. User initiates live session on mobile/browser
2. Client sends frames via HTTP POST (not WebSocket for simplicity)
3. Server processes each frame incrementally
4. Returns partial point clouds as they're computed
5. Frontend visualizes in real-time
6. User finalizes session - all frames saved to a scan

### Frontend Implementation

```javascript
import { createLiveSession, processLiveFrame, finalizeLiveSession } from '@/lib/api.js'

// 1. Start session
const session = await createLiveSession(quality=70)
const sessionId = session.session_id

// 2. Stream frames (in a loop)
const canvas = document.getElementById('canvas')
const ctx = canvas.getContext('2d')
const stream = await navigator.mediaDevices.getUserMedia({ video: true })
const video = document.getElementById('video')
video.srcObject = stream

// Send frames
setInterval(async () => {
  canvas.width = video.videoWidth
  canvas.height = video.videoHeight
  ctx.drawImage(video, 0, 0)
  
  const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', 0.7))
  const result = await processLiveFrame(sessionId, blob)
  
  // Update UI with point count, FPS, etc.
  console.log(`Points: ${result.point_count}, FPS: ${result.fps}`)
}, 500)  // Every 500ms

// 3. When done, finalize
const scan = await createScan('Live Scan - Interior')
const finalized = await finalizeLiveSession(sessionId, scan.scan.id)

// 4. Process the accumulated frames
await startProcessing(scan.scan.id, quality=75, detail=64)
```

### Backend Implementation

**File:** `server/live_camera_streaming.py`

Key classes:
- `LiveStreamSession` - Tracks one streaming session
- `FrameBuffer` - Stores recent frames in circular buffer
- `LiveCameraProcessor` - Processes frames, extracts features, triangulates
- `LiveSessionManager` - Manages multiple concurrent sessions

**Key Methods:**
```python
processor = LiveCameraProcessor()

# Process each frame
result = processor.process_frame(frame_bytes)
# Returns: {
#   "processed": bool,
#   "has_points": bool,
#   "point_count": int,
#   "points_base64": str,  # Partial point cloud
#   "frame_id": int
# }

# Get accumulated frames for batch processing
frames = processor.get_buffer_frames()  # list[bytes]
processor.clear()
```

### Key Features

- **Lightweight Processing** - Uses fewer SIFT features (500 vs 3000) for speed
- **Incremental Point Clouds** - Every frame processed returns partial 3D cloud
- **FPS Tracking** - Real-time performance metrics
- **Circular Frame Buffer** - Keeps last 15 frames for memory efficiency
- **Multiple Sessions** - Support concurrent streaming from different users

### API Endpoints

```
POST   /api/live-sessions
       ?quality=70
       → Create new session

GET    /api/live-sessions/{session_id}
       → Get session status, FPS, frame count

POST   /api/live-sessions/{session_id}/frame
       (multipart: file)
       → Process one frame, get partial point cloud

POST   /api/live-sessions/{session_id}/finalize
       ?scan_id={id}
       → Save all frames to scan for processing

GET    /api/live-sessions
       → List all active sessions
```

---

## 3. Progressive Refinement

### Purpose
Continuously improve a scan by adding new frames to the existing point cloud. Instead of reprocessing from scratch, merge new frames to existing cloud.

### How It Works

**Flow:**
1. Initial scan creates point cloud with N points
2. User adds new frames later
3. New frames processed, generating M new points
4. Point clouds registered (aligned) using Procrustes method
5. Merged cloud with deduplication
6. Overall statistics updated

### Frontend Implementation

```javascript
import { 
  initializeRefiner, 
  addRefinementFrame, 
  getRefinementStats 
} from '@/lib/api.js'

// 1. Initialize refiner after initial scan
await initializeRefiner(scanId)

// 2. Later, user adds more frames
const newFrame1 = new File([videoFrame1], 'frame1.jpg', { type: 'image/jpeg' })
await addRefinementFrame(scanId, frameId=1, newFrame1)

const newFrame2 = new File([videoFrame2], 'frame2.jpg', { type: 'image/jpeg' })
await addRefinementFrame(scanId, frameId=2, newFrame2)

// 3. Check improvement
const stats = await getRefinementStats(scanId)
console.log(stats)
// {
//   "statistics": {
//     "total_frames": 5,
//     "total_points": 150000,
//     "avg_points_per_frame": 30000,
//     "coverage": 85
//   }
// }
```

### Backend Implementation

**File:** `server/progressive_refinement.py`

Key classes:
- `PointCloudFrame` - Stores one frame's point cloud + metadata
- `ProgressivePointCloudRefiner` - Main refinement manager

**Key Methods:**
```python
refiner = ProgressivePointCloudRefiner(max_frames=50, dedup_radius_mm=10)

# Add new frames
result = refiner.add_frame(points_3d, colors, frame_id, timestamp)
# Returns: {
#   "merged_point_count": int,
#   "new_points_added": int,
#   "duplicates_removed": int
# }

# Register point clouds (align them)
aligned_points = refiner.apply_registration(new_cloud, prev_cloud)

# Get merged result
points, colors = refiner.get_merged_cloud()

# Statistics
stats = refiner.get_statistics()
```

### Deduplication

Uses grid-based spatial clustering to remove duplicate points:
- Configurable radius (default: 10mm)
- Fast grid-based approach
- Preserves RGB colors from first occurrence

### Registration (Alignment)

Implements simplified rigid transformation (Procrustes):
- No scaling, only rotation + translation
- For better results, can integrate Open3D's ICP algorithm
- Aligns new point cloud to previous coordinate system

### API Endpoints

```
POST   /api/scans/{scan_id}/refiner/initialize
       → Initialize refinement system for scan

POST   /api/scans/{scan_id}/refiner/add-frame
       ?frame_id={n}
       (multipart: file)
       → Add frame to progressive refinement

GET    /api/scans/{scan_id}/refiner/stats
       → Get refinement statistics and history
```

---

## Integration Example: Full Workflow

```javascript
import * as api from '@/lib/api.js'

async function liveScanInterior() {
  // 1. Create scan
  const scanRes = await api.createScan('Living Room - Live', 'room')
  const scanId = scanRes.scan.id

  // 2. Start live streaming
  const sessionRes = await api.createLiveSession(quality=75)
  const sessionId = sessionRes.session_id

  // 3. Initialize calibration (for ArUco)
  // (User needs to have ArUco marker in view)

  // 4. Stream frames for 30 seconds
  let frameCount = 0
  const streamInterval = setInterval(async () => {
    const blob = await captureWebcamFrame()
    const result = await api.processLiveFrame(sessionId, blob)
    frameCount++
    updateUI(`Frames: ${frameCount}, Points: ${result.point_count}`)
  }, 500)

  // Stop after 30 seconds
  setTimeout(() => clearInterval(streamInterval), 30000)

  // 5. Finalize session
  await api.finalizeLiveSession(sessionId, scanId)

  // 6. Calibrate with ArUco
  try {
    await api.calibrateWithAruco(scanId, '20cm')
    console.log('Calibrated!')
  } catch (e) {
    console.log('No ArUco marker found, skipping calibration')
  }

  // 7. Process accumulated frames
  await api.startProcessing(scanId, quality=75, detail=64)

  // 8. Get dimensions (only works if calibrated)
  const dims = await api.getDimensions(scanId)
  console.log(`Room: ${dims.dimensions.width}m x ${dims.dimensions.height}m`)

  // 9. Optional: Add more frames later
  await api.initializeRefiner(scanId)
  const frame1 = new File([...], 'extra1.jpg')
  await api.addRefinementFrame(scanId, 1, frame1)
  const stats = await api.getRefinementStats(scanId)
  console.log(`Refined: ${stats.statistics.total_points} points now`)
}
```

---

## Key Files

| File | Purpose |
|------|---------|
| `server/measurement_calibration.py` | Scale calibration logic |
| `server/live_camera_streaming.py` | Live streaming & frame processing |
| `server/progressive_refinement.py` | Point cloud merging & deduplication |
| `server/main.py` | FastAPI endpoints (added sections) |
| `src/lib/api.js` | Frontend client functions |

---

## Performance Notes

### Measurement Calibration
- ArUco detection: ~100-200ms per image
- Scale factor computation: O(1)
- Dimension calculation: O(n) where n = point count

### Live Streaming
- Frame processing: ~200-500ms per frame (quality-dependent)
- FPS: 2-5 frames/sec realistic
- Memory: ~500MB for 15-frame buffer at 1080p

### Progressive Refinement
- Point cloud merging: O(n + m) where n, m = cloud sizes
- Deduplication: O(n log n) with grid-based approach
- Registration: O(n²) worst case, typically O(n) with SVD optimization

---

## Future Improvements

1. **ICP Registration** - Integrate Open3D's Iterative Closest Point for better alignment
2. **Multi-scale Processing** - Pyramid-based features for robustness
3. **WebSocket Streaming** - Replace HTTP POST with WebSocket for lower latency
4. **Hardware Acceleration** - GPU SIFT feature detection with CUDA
5. **Mesh Refinement** - Incremental mesh updates instead of point clouds
6. **Loop Closure** - Detect when user returns to start point, improve consistency
7. **Semantic Segmentation** - Identify room types/furniture for better calibration
8. **Cloud Streaming** - Stream partial models to frontend in real-time
