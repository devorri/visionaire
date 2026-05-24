# Quick Start Guide - Using the New Features

## 🎯 Measurement Dimensions

### Scenario: Measure dimensions of a room

```javascript
import { calibrateWithAruco, getDimensions } from '@/lib/api'

// 1. Upload photos WITH an ArUco marker visible
await uploadPhotos(scanId, [photo1, photo2, ...])

// 2. Calibrate using marker
const cal = await calibrateWithAruco(scanId, '20cm')
// Returns: { calibrated: true, scale_factor: 0.0015, ... }

// 3. Get dimensions in meters
const dims = await getDimensions(scanId)
console.log(`Room is ${dims.dimensions.width.toFixed(1)}m wide`)
// Output: Room is 4.5m wide
```

**Key Points:**
- Place a **known-size ArUco marker** (5-30cm) in your first photo
- Server auto-detects marker → calculates scale
- All dimensions returned in **meters**
- Works with Meshy API AND local photogrammetry

---

## 🎥 Live Camera Streaming

### Scenario: Real-time interior scanning on mobile

```javascript
import { 
  createScan, 
  createLiveSession, 
  processLiveFrame,
  finalizeLiveSession,
  startProcessing 
} from '@/lib/api'

// 1. Create scan
const scan = await createScan('Kitchen Scan', 'room')

// 2. Start live session
const session = await createLiveSession(quality=75)

// 3. Stream frames (every 500ms)
setInterval(async () => {
  const frameBlob = getCameraFrame()  // From WebRTC/getUserMedia
  const result = await processLiveFrame(session.session_id, frameBlob)
  
  updateUI({
    points: result.point_count,
    fps: result.fps,
    frame: result.frame_id
  })
}, 500)

// 4. Stop after 30 seconds
setTimeout(async () => {
  await finalizeLiveSession(session.session_id, scan.scan.id)
  
  // 5. Process accumulated frames
  await startProcessing(scan.scan.id, quality=75, detail=64)
}, 30000)
```

**Key Points:**
- Streams frames at **2-5 FPS** realistic
- Each frame generates **partial point cloud** (visible in real-time)
- All frames saved to database for batch processing
- Works on **mobile + desktop browsers**
- Uses `navigator.mediaDevices.getUserMedia()`

---

## 🔄 Progressive Refinement

### Scenario: Improve scan by adding more frames

```javascript
import { 
  initializeRefiner,
  addRefinementFrame,
  getRefinementStats
} from '@/lib/api'

// 1. After initial scan is complete
await initializeRefiner(scanId)

// 2. Later, user adds more coverage
for (let i = 1; i <= 5; i++) {
  const newFrame = captureNewFrame()
  await addRefinementFrame(scanId, i, newFrame)
}

// 3. Check improvement
const stats = await getRefinementStats(scanId)
console.log(`${stats.statistics.total_points} total points now`)
// Output: 250000 total points now (vs 150000 before)
```

**Key Points:**
- Incremental merging - **no reprocessing from scratch**
- Automatic **deduplication** (default: 10mm radius)
- Automatic **registration** (alignment) between point clouds
- Statistics show **improvement over time**

---

## 🏠 Full Example: Living Room Scan

```javascript
import * as api from '@/lib/api'

async function scanLivingRoom() {
  // 1️⃣ Create scan
  const scan = await api.createScan('Living Room - Full Scan', 'room')
  const scanId = scan.scan.id

  // 2️⃣ Start live session (30 second scan)
  const session = await api.createLiveSession(quality=75)
  let frameCount = 0
  
  const interval = setInterval(async () => {
    const blob = await captureWebcamFrame()
    const result = await api.processLiveFrame(session.session_id, blob)
    frameCount++
    
    if (frameCount % 5 === 0) {
      console.log(`✓ ${frameCount} frames, ${result.point_count} points`)
    }
  }, 500)

  // Stop streaming
  await sleep(30000)
  clearInterval(interval)

  // 3️⃣ Finalize streaming
  await api.finalizeLiveSession(session.session_id, scanId)
  console.log('✓ Streaming complete')

  // 4️⃣ Calibrate with ArUco marker
  try {
    await api.calibrateWithAruco(scanId, '20cm')
    console.log('✓ Calibrated with ArUco marker')
  } catch (e) {
    console.log('No ArUco detected, skipping calibration')
  }

  // 5️⃣ Initialize progressive refinement
  await api.initializeRefiner(scanId)
  
  // 6️⃣ Process all accumulated frames
  console.log('Processing...')
  await api.startProcessing(scanId, quality=75, detail=64)

  // 7️⃣ Poll for completion
  let isReady = false
  while (!isReady) {
    const status = await api.getStatus(scanId)
    if (status.status === 'ready') {
      isReady = true
      console.log('✓ Processing complete!')
    } else {
      console.log(`  ${status.stage_name}... ${status.progress}%`)
      await sleep(2000)
    }
  }

  // 8️⃣ Get real-world dimensions
  try {
    const dims = await api.getDimensions(scanId)
    console.log('📐 Dimensions:')
    console.log(`  Width:  ${dims.dimensions.width.toFixed(2)}m`)
    console.log(`  Height: ${dims.dimensions.height.toFixed(2)}m`)
    console.log(`  Depth:  ${dims.dimensions.depth.toFixed(2)}m`)
    console.log(`  Volume: ${dims.dimensions.volume.toFixed(1)}m³`)
  } catch (e) {
    console.log('Dimensions not available (scan not calibrated)')
  }

  // 9️⃣ Get scan details
  const finalScan = await api.fetchScan(scanId)
  console.log('✅ Scan complete!')
  console.log(`  Points: ${finalScan.scan.points}`)
  console.log(`  Model URL: ${finalScan.scan.model_url}`)
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms))
}

async function captureWebcamFrame() {
  // Your webcam capture logic
  // Return Blob in JPEG format
}
```

---

## 🛠️ API Reference Quick Lookup

### Measurement Calibration
```
POST   /api/scans/{id}/calibration/aruco?marker_size=10cm
POST   /api/scans/{id}/calibration/manual?reference_distance_meters=3.0
POST   /api/scans/{id}/calibration/reset
GET    /api/scans/{id}/dimensions
```

### Progressive Refinement
```
POST   /api/scans/{id}/refiner/initialize
POST   /api/scans/{id}/refiner/add-frame?frame_id=1   [multipart]
GET    /api/scans/{id}/refiner/stats
```

### Live Streaming
```
POST   /api/live-sessions?quality=70
GET    /api/live-sessions/{session_id}
POST   /api/live-sessions/{session_id}/frame             [multipart]
POST   /api/live-sessions/{session_id}/finalize?scan_id={id}
GET    /api/live-sessions
```

---

## ⚙️ Setup Checklist

- [x] Backend: `server/measurement_calibration.py` - DONE
- [x] Backend: `server/progressive_refinement.py` - DONE
- [x] Backend: `server/live_camera_streaming.py` - DONE
- [x] Backend: `server/main.py` - API endpoints added
- [x] Frontend: `src/lib/api.js` - New client functions
- [x] Frontend: `src/components/LiveCameraScanner.jsx` - React component
- [ ] Database: Add `calibration` JSONB column to scans table
- [ ] Testing: Run through scenarios
- [ ] Frontend: Integrate `LiveCameraScanner` into app
- [ ] Docs: Add feature guide to README

---

## 💡 Pro Tips

1. **For best results with calibration:**
   - Use a **high-contrast ArUco marker** (black/white)
   - Place marker **at object scale** (don't use tiny markers for large spaces)
   - Pre-set marker size in API call (`10cm`, `20cm`, etc.)

2. **For live streaming:**
   - Start with **lower quality** (60-70) for faster processing
   - Scan at **1-2 frames per second** (set interval to 500-1000ms)
   - Move camera **slowly and smoothly** (minimize motion blur)
   - **Circle around** the space rather than straight lines

3. **For progressive refinement:**
   - Add frames from **different angles** for better coverage
   - Don't wait too long between additions (coordinate system drift)
   - Monitor statistics to verify improvement

4. **For interior spaces:**
   - Combine **live streaming** (fast initial scan) + **calibration** (accurate dimensions)
   - Use **room-sized ArUco markers** (20-30cm) for accuracy
   - Scan **multiple passes** for high coverage before finalizing

---

## 🐛 Troubleshooting

**ArUco not detected:**
- Ensure marker is visible and in focus
- Try larger marker size preset (`20cm` instead of `10cm`)
- Check lighting and contrast

**Live streaming slow:**
- Reduce quality parameter (60 instead of 75)
- Lower frame rate (1000ms interval instead of 500ms)
- Check camera resolution (high res = slower processing)

**Dimension mismatch:**
- Verify calibration succeeded (check response)
- Ensure ArUco marker size is correct
- For manual calibration, double-check reference distance

**Memory issues with long sessions:**
- Frame buffer limited to 15 most recent frames
- For longer sessions, add intermediate checkpoints
- Consider finalize + reprocess cycle for very long scans
