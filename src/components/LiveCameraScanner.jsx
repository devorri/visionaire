/**
 * LiveCameraScanner.jsx
 *
 * Real-time 3D scanning component with side-by-side live camera video
 * preview and a dynamic Three.js point-cloud viewport.
 *
 * Points stream in from the backend as base64-encoded Float32Arrays and
 * accumulate in the viewer in real time while the user scans.
 */

import { useState, useRef, useEffect, useCallback } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import {
  createLiveSession,
  processLiveFrame,
  finalizeLiveSession,
} from '../lib/api.js'

// ── Helpers ──────────────────────────────────────────────────────────

/** Decode a base64-encoded Float32 blob into a flat Float32Array. */
function decodeBase64Points(base64String) {
  if (!base64String) return null
  try {
    const binary = atob(base64String)
    const bytes = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i)
    }
    return new Float32Array(bytes.buffer)
  } catch {
    return null
  }
}

// ── Component ────────────────────────────────────────────────────────

export default function LiveCameraScanner({ scanId, onScanFinalized, quality = 70 }) {
  const videoRef = useRef(null)
  const canvasRef = useRef(null)
  const viewerContainerRef = useRef(null)

  const streamRef = useRef(null)
  const streamIntervalRef = useRef(null)
  const startTimeRef = useRef(null)
  const sessionIdRef = useRef(null)

  // Three.js refs
  const sceneRef = useRef(null)
  const cameraRef = useRef(null)
  const rendererRef = useRef(null)
  const controlsRef = useRef(null)
  const pointsObjRef = useRef(null)
  const geometryRef = useRef(null)
  const accumulatedPointsRef = useRef([])
  const frameIdRef = useRef(0)

  const [isStreaming, setIsStreaming] = useState(false)
  const [stats, setStats] = useState({
    frameCount: 0,
    pointCount: 0,
    fps: 0,
    timeLapsed: '0.0',
  })
  const [error, setError] = useState(null)

  // ── Initialize Three.js Scene ──────────────────────────────────────

  useEffect(() => {
    const container = viewerContainerRef.current
    if (!container) return

    const w = container.clientWidth || 450
    const h = container.clientHeight || 340

    // Scene
    const scene = new THREE.Scene()
    scene.background = new THREE.Color('#0d0f12')
    sceneRef.current = scene

    // Camera
    const camera = new THREE.PerspectiveCamera(50, w / h, 0.01, 500)
    camera.position.set(2, 1.5, 3)
    cameraRef.current = camera

    // Renderer
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.setSize(w, h)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    container.appendChild(renderer.domElement)
    rendererRef.current = renderer

    // Controls
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.08
    controls.autoRotate = true
    controls.autoRotateSpeed = 0.6
    controls.minDistance = 0.5
    controls.maxDistance = 30
    controlsRef.current = controls

    // Lights
    scene.add(new THREE.AmbientLight('#ffffff', 0.6))

    // Floor grid
    const floor = new THREE.GridHelper(8, 32, '#2a4a4e', '#1a2a2e')
    floor.position.y = -1.5
    floor.material.transparent = true
    floor.material.opacity = 0.35
    scene.add(floor)

    // Point cloud geometry & material
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute(
      'position',
      new THREE.Float32BufferAttribute(new Float32Array(0), 3),
    )
    geometry.setAttribute(
      'color',
      new THREE.Float32BufferAttribute(new Float32Array(0), 3),
    )
    geometryRef.current = geometry

    const material = new THREE.PointsMaterial({
      size: 3.5,
      sizeAttenuation: true,
      vertexColors: true,
      transparent: true,
      opacity: 0.92,
      depthWrite: false,
    })

    const points = new THREE.Points(geometry, material)
    scene.add(points)
    pointsObjRef.current = points

    // Resize observer
    const ro = new ResizeObserver(([entry]) => {
      const nw = entry.contentRect.width || w
      const nh = entry.contentRect.height || h
      camera.aspect = nw / nh
      camera.updateProjectionMatrix()
      renderer.setSize(nw, nh)
    })
    ro.observe(container)

    // Animation loop
    let animId = 0
    const animate = () => {
      controls.update()
      renderer.render(scene, camera)
      animId = requestAnimationFrame(animate)
    }
    animate()

    return () => {
      cancelAnimationFrame(animId)
      ro.disconnect()
      controls.dispose()
      geometry.dispose()
      material.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [])

  // ── Push points into Three.js ──────────────────────────────────────

  const pushPointsToViewer = useCallback((flatFloat32Array) => {
    if (!flatFloat32Array || flatFloat32Array.length < 3) return
    if (!geometryRef.current) return

    const newPosArray = flatFloat32Array
    const pointCount = newPosArray.length / 3

    // Generate per-point mint-teal-cyan gradient colours
    const newColors = new Float32Array(newPosArray.length)
    for (let i = 0; i < pointCount; i++) {
      // Slight hue variation based on Y coordinate for visual interest
      const y = newPosArray[i * 3 + 1]
      const t = Math.abs(y) * 0.2 + 0.5
      newColors[i * 3] = 0.25 + t * 0.08     // R
      newColors[i * 3 + 1] = 0.72 + t * 0.10 // G
      newColors[i * 3 + 2] = 0.68 + t * 0.15 // B
    }

    // Accumulate
    const prev = accumulatedPointsRef.current
    const prevPositions = prev.length > 0 ? prev[0] : new Float32Array(0)
    const prevColors = prev.length > 1 ? prev[1] : new Float32Array(0)

    const mergedPositions = new Float32Array(prevPositions.length + newPosArray.length)
    mergedPositions.set(prevPositions, 0)
    mergedPositions.set(newPosArray, prevPositions.length)

    const mergedColors = new Float32Array(prevColors.length + newColors.length)
    mergedColors.set(prevColors, 0)
    mergedColors.set(newColors, prevColors.length)

    accumulatedPointsRef.current = [mergedPositions, mergedColors]

    // Update Three.js geometry
    const geom = geometryRef.current
    geom.setAttribute(
      'position',
      new THREE.Float32BufferAttribute(mergedPositions, 3),
    )
    geom.setAttribute(
      'color',
      new THREE.Float32BufferAttribute(mergedColors, 3),
    )
    geom.attributes.position.needsUpdate = true
    geom.attributes.color.needsUpdate = true
    geom.computeBoundingSphere()
  }, [])

  // ── Camera & Streaming ─────────────────────────────────────────────

  const startStreaming = useCallback(async () => {
    try {
      setError(null)

      const stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: { ideal: 'environment' },
          width: { ideal: 1280 },
          height: { ideal: 720 },
        },
        audio: false,
      })
      streamRef.current = stream

      if (videoRef.current) {
        videoRef.current.srcObject = stream
        await videoRef.current.play()
      }

      // Create live session
      const session = await createLiveSession(quality)
      sessionIdRef.current = session.session_id
      setIsStreaming(true)
      startTimeRef.current = Date.now()

      // Reset accumulated points
      accumulatedPointsRef.current = []
      if (geometryRef.current) {
        geometryRef.current.setAttribute(
          'position',
          new THREE.Float32BufferAttribute(new Float32Array(0), 3),
        )
        geometryRef.current.setAttribute(
          'color',
          new THREE.Float32BufferAttribute(new Float32Array(0), 3),
        )
      }

      // Start streaming frames at ~2 fps
      const interval = setInterval(() => {
        sendFrame()
      }, 500)
      streamIntervalRef.current = interval
    } catch (err) {
      setError(`Camera error: ${err.message}`)
      console.error('LiveCameraScanner startStreaming error:', err)
    }
  }, [quality, pushPointsToViewer])

  const sendFrame = useCallback(async () => {
    const canvas = canvasRef.current
    const video = videoRef.current
    const sid = sessionIdRef.current
    if (!canvas || !video || !sid) return

    try {
      canvas.width = video.videoWidth || 1280
      canvas.height = video.videoHeight || 720
      const ctx = canvas.getContext('2d')
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height)

      const blob = await new Promise((resolve) =>
        canvas.toBlob(resolve, 'image/jpeg', 0.72),
      )
      if (!blob) return

      const result = await processLiveFrame(sid, blob)

      // Update stats
      setStats((prev) => ({
        frameCount: result.frame_count || result.frame_id || prev.frameCount + 1,
        pointCount: result.point_count || prev.pointCount,
        fps: result.fps ? result.fps.toFixed(1) : prev.fps,
        timeLapsed: startTimeRef.current
          ? ((Date.now() - startTimeRef.current) / 1000).toFixed(1)
          : '0.0',
      }))

      // Decode and push points to Three.js
      if (result.has_points && result.points_base64) {
        const decoded = decodeBase64Points(result.points_base64)
        if (decoded) {
          pushPointsToViewer(decoded)
        }
      }
    } catch (err) {
      console.error('Frame processing error:', err)
    }
  }, [pushPointsToViewer])

  const stopStreaming = useCallback(async () => {
    if (streamIntervalRef.current) {
      clearInterval(streamIntervalRef.current)
      streamIntervalRef.current = null
    }

    // Stop camera tracks
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop())
      streamRef.current = null
    }
    if (videoRef.current) videoRef.current.srcObject = null

    setIsStreaming(false)

    // Finalize session on backend
    const sid = sessionIdRef.current
    if (sid && scanId) {
      try {
        await finalizeLiveSession(sid, scanId)
      } catch (err) {
        console.error('Finalize error:', err)
        setError(`Failed to save scan: ${err.message}`)
      }
      sessionIdRef.current = null
    }

    if (onScanFinalized) onScanFinalized()
  }, [scanId, onScanFinalized])

  // Clean-up on unmount
  useEffect(() => {
    return () => {
      if (streamIntervalRef.current) clearInterval(streamIntervalRef.current)
      if (streamRef.current) {
        streamRef.current.getTracks().forEach((t) => t.stop())
      }
    }
  }, [])

  // ── Accumulated point count for display ────────────────────────────

  const totalPoints = accumulatedPointsRef.current.length > 0
    ? accumulatedPointsRef.current[0].length / 3
    : 0

  // ── Render ─────────────────────────────────────────────────────────

  return (
    <div className="live-scanner-root">
      <style>{`
        .live-scanner-root {
          display: grid;
          grid-template-columns: 1fr 1fr;
          grid-template-rows: 1fr auto;
          gap: 0;
          width: 100%;
          height: 100%;
          background: #0d0f12;
          overflow: hidden;
        }

        /* ── Video pane ─────────────────────────── */
        .ls-video-pane {
          position: relative;
          min-height: 0;
          overflow: hidden;
          border-right: 1px solid rgba(255,255,255,0.06);
        }
        .ls-video-pane video {
          width: 100%;
          height: 100%;
          object-fit: cover;
          display: block;
          background: #000;
        }
        .ls-video-pane canvas { display: none; }

        .ls-video-label {
          position: absolute;
          top: 14px;
          left: 14px;
          display: inline-flex;
          align-items: center;
          gap: 7px;
          padding: 5px 12px;
          font-size: 12px;
          font-weight: 700;
          color: #fff;
          background: rgba(0,0,0,0.55);
          border: 1px solid rgba(255,255,255,0.12);
          border-radius: 8px;
          backdrop-filter: blur(8px);
          letter-spacing: 0.5px;
          text-transform: uppercase;
        }
        .ls-video-label .ls-rec-dot {
          width: 8px;
          height: 8px;
          border-radius: 50%;
          background: #ff4d4d;
          box-shadow: 0 0 6px 2px rgba(255,77,77,0.5);
          animation: ls-pulse 1.2s ease-in-out infinite;
        }
        @keyframes ls-pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }

        /* ── 3D viewport pane ──────────────────── */
        .ls-viewer-pane {
          position: relative;
          min-height: 0;
          overflow: hidden;
        }
        .ls-viewer-pane canvas {
          display: block;
          width: 100%;
          height: 100%;
        }
        .ls-viewer-label {
          position: absolute;
          top: 14px;
          left: 14px;
          display: inline-flex;
          align-items: center;
          gap: 6px;
          padding: 5px 12px;
          font-size: 12px;
          font-weight: 700;
          color: #4fd1c5;
          background: rgba(0,0,0,0.55);
          border: 1px solid rgba(79,209,197,0.25);
          border-radius: 8px;
          backdrop-filter: blur(8px);
          letter-spacing: 0.5px;
          text-transform: uppercase;
        }

        /* ── Stat overlay (video pane bottom) ─── */
        .ls-stats-overlay {
          position: absolute;
          bottom: 14px;
          left: 14px;
          right: 14px;
          display: flex;
          gap: 8px;
          justify-content: space-between;
        }
        .ls-stat {
          flex: 1;
          padding: 8px 0;
          text-align: center;
          background: rgba(0,0,0,0.55);
          border: 1px solid rgba(255,255,255,0.10);
          border-radius: 8px;
          backdrop-filter: blur(8px);
        }
        .ls-stat-value {
          display: block;
          font-size: 16px;
          font-weight: 800;
          color: #fff;
          line-height: 1.2;
        }
        .ls-stat-label {
          display: block;
          font-size: 10px;
          color: rgba(255,255,255,0.5);
          text-transform: uppercase;
          letter-spacing: 0.6px;
          margin-top: 2px;
        }

        /* ── Point count overlay (viewer pane) ── */
        .ls-points-overlay {
          position: absolute;
          bottom: 14px;
          right: 14px;
          padding: 6px 14px;
          font-size: 13px;
          font-weight: 700;
          color: #4fd1c5;
          background: rgba(0,0,0,0.55);
          border: 1px solid rgba(79,209,197,0.25);
          border-radius: 8px;
          backdrop-filter: blur(8px);
        }

        /* ── Bottom bar ────────────────────────── */
        .ls-bottom-bar {
          grid-column: 1 / -1;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 14px;
          padding: 16px 20px;
          background: rgba(13,15,18,0.92);
          border-top: 1px solid rgba(255,255,255,0.08);
          backdrop-filter: blur(12px);
        }

        .ls-btn {
          min-height: 44px;
          padding: 0 28px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 9px;
          font-size: 15px;
          font-weight: 700;
          border: none;
          border-radius: 10px;
          cursor: pointer;
          transition: all 0.15s ease;
          letter-spacing: 0.3px;
        }
        .ls-btn-start {
          color: #0f1417;
          background: linear-gradient(135deg, #4fd1c5, #38b2ac);
          box-shadow: 0 2px 12px rgba(79,209,197,0.35);
        }
        .ls-btn-start:hover {
          box-shadow: 0 4px 20px rgba(79,209,197,0.5);
          transform: translateY(-1px);
        }
        .ls-btn-stop {
          color: #fff;
          background: linear-gradient(135deg, #ff4d4d, #e53e3e);
          box-shadow: 0 2px 12px rgba(255,77,77,0.3);
        }
        .ls-btn-stop:hover {
          box-shadow: 0 4px 20px rgba(255,77,77,0.45);
          transform: translateY(-1px);
        }

        .ls-error {
          grid-column: 1 / -1;
          padding: 10px 18px;
          color: #feb2b2;
          background: rgba(255,77,77,0.12);
          border-top: 1px solid rgba(255,77,77,0.25);
          font-size: 13px;
          text-align: center;
        }

        /* ── Empty state message in the viewer ─── */
        .ls-empty {
          position: absolute;
          inset: 0;
          display: grid;
          place-items: center;
          padding: 24px;
          text-align: center;
          color: rgba(255,255,255,0.25);
          font-size: 14px;
          pointer-events: none;
        }

        /* ── Responsive: stack on narrow viewports */
        @media (max-width: 900px) {
          .live-scanner-root {
            grid-template-columns: 1fr;
            grid-template-rows: 1fr 1fr auto;
          }
          .ls-video-pane {
            border-right: 0;
            border-bottom: 1px solid rgba(255,255,255,0.06);
          }
        }
      `}</style>

      {/* ── Video Pane ─────────────────────────────────────────────── */}
      <div className="ls-video-pane">
        <video ref={videoRef} playsInline muted />
        <canvas ref={canvasRef} />
        {isStreaming && (
          <>
            <div className="ls-video-label">
              <span className="ls-rec-dot" />
              Live
            </div>
            <div className="ls-stats-overlay">
              <div className="ls-stat">
                <span className="ls-stat-value">{stats.frameCount}</span>
                <span className="ls-stat-label">Frames</span>
              </div>
              <div className="ls-stat">
                <span className="ls-stat-value">{stats.fps}</span>
                <span className="ls-stat-label">FPS</span>
              </div>
              <div className="ls-stat">
                <span className="ls-stat-value">{stats.timeLapsed}s</span>
                <span className="ls-stat-label">Elapsed</span>
              </div>
            </div>
          </>
        )}
        {!isStreaming && (
          <div className="ls-empty">
            <span>Press Start to begin live 3D scanning</span>
          </div>
        )}
      </div>

      {/* ── Three.js Viewport Pane ────────────────────────────────── */}
      <div className="ls-viewer-pane" ref={viewerContainerRef}>
        {isStreaming && (
          <div className="ls-viewer-label">⬡ 3D Point Cloud</div>
        )}
        {totalPoints > 0 && (
          <div className="ls-points-overlay">
            {totalPoints.toLocaleString()} points
          </div>
        )}
        {!isStreaming && totalPoints === 0 && (
          <div className="ls-empty">
            <span>Points will appear here during scanning</span>
          </div>
        )}
      </div>

      {/* ── Error banner ──────────────────────────────────────────── */}
      {error && <div className="ls-error">{error}</div>}

      {/* ── Bottom action bar ─────────────────────────────────────── */}
      <div className="ls-bottom-bar">
        {!isStreaming ? (
          <button className="ls-btn ls-btn-start" type="button" onClick={startStreaming}>
            🎥 Start Live Scan
          </button>
        ) : (
          <button className="ls-btn ls-btn-stop" type="button" onClick={stopStreaming}>
            ⏹ Stop &amp; Save Scan
          </button>
        )}
      </div>
    </div>
  )
}
