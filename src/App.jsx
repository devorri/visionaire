import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { OBJLoader } from 'three/examples/jsm/loaders/OBJLoader.js'
import {
  Aperture,
  Box,
  Camera,
  CheckCircle2,
  CircleDot,
  Download,
  Gauge,
  Grid3X3,
  ImagePlus,
  Layers,
  LoaderCircle,
  Play,
  Ruler,
  RotateCcw,
  ScanLine,
  SlidersHorizontal,
  Sparkles,
  Square,
  Trash2,
  Upload,
  Video,
  AlertTriangle,
} from 'lucide-react'
import {
  createScan,
  uploadPhotos,
  startProcessing,
  getStatus,
  fetchScans,
  checkApiHealth,
  deleteScan as apiDeleteScan,
} from './lib/api.js'
import LiveCameraScanner from './components/LiveCameraScanner.jsx'
import './App.css'

const MAX_PHOTOS = 30
const VIDEO_FRAME_COUNT = 12
const SCAN_RECORDING_FRAME_COUNT = 20

const PIPELINE = ['Feature detection', 'Feature matching', 'Pose recovery', 'Triangulation', 'Point cloud', 'Mesh generation', 'Export']


function makeId() {
  return window.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function readImageFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      resolve({
        id: makeId(),
        name: file.name,
        src: reader.result,
        file,
        origin: 'upload',
      })
    }
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

function loadVideoMetadata(video) {
  return new Promise((resolve, reject) => {
    video.onloadedmetadata = resolve
    video.onerror = reject
  })
}

function seekVideo(video, time) {
  return new Promise((resolve, reject) => {
    video.onseeked = resolve
    video.onerror = reject
    video.currentTime = time
  })
}

async function extractVideoFrames(file, frameCount = VIDEO_FRAME_COUNT) {
  const url = URL.createObjectURL(file)
  const video = document.createElement('video')
  video.src = url
  video.muted = true
  video.playsInline = true
  video.preload = 'metadata'

  try {
    await loadVideoMetadata(video)

    const duration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 1
    const canvas = document.createElement('canvas')
    canvas.width = video.videoWidth || 1280
    canvas.height = video.videoHeight || 720
    const context = canvas.getContext('2d')
    const frames = []

    for (let index = 0; index < frameCount; index += 1) {
      const time = ((index + 0.5) / frameCount) * duration
      await seekVideo(video, Math.min(duration - 0.05, Math.max(0, time)))
      context.drawImage(video, 0, 0, canvas.width, canvas.height)

      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9))
      if (!blob) continue

      const frameFile = new File([blob], `${file.name.replace(/\.[^.]+$/, '')}-frame-${index + 1}.jpg`, {
        type: 'image/jpeg',
      })

      frames.push({
        id: makeId(),
        name: frameFile.name,
        src: URL.createObjectURL(blob),
        file: frameFile,
        origin: 'video',
      })
    }

    return frames
  } finally {
    URL.revokeObjectURL(url)
  }
}

function formatDate(dateString) {
  if (!dateString) {
    return new Intl.DateTimeFormat('en', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    }).format(new Date())
  }
  return new Intl.DateTimeFormat('en', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(dateString))
}

function slugify(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '')
}

function formatMeasurement(value, unit = 'model units') {
  if (!Number.isFinite(value)) return '—'
  const formatted = value >= 10 ? value.toFixed(1) : value.toFixed(2)
  return `${formatted} ${unit}`
}


function ScanViewer({ scan, autoRotate, renderMode, onDimensions }) {
  const containerRef = useRef(null)
  const [loadedUrl, setLoadedUrl] = useState('')
  const [errorUrl, setErrorUrl] = useState('')

  useEffect(() => {
    const container = containerRef.current
    if (!container) return undefined

    if (!scan?.model_url || scan.status !== 'ready') return undefined

    const modelUrl = scan.model_url
    const isSupportedModel = modelUrl.endsWith('.glb') || modelUrl.includes('.glb') || modelUrl.endsWith('.obj') || modelUrl.includes('.obj')
    if (!isSupportedModel) return undefined

    const scene = new THREE.Scene()
    scene.background = new THREE.Color('#0d0f12')

    const width = container.clientWidth || 900
    const height = container.clientHeight || 640
    const camera = new THREE.PerspectiveCamera(42, width / height, 0.1, 100)
    camera.position.set(3.5, 2.35, 4.15)

    const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.setSize(width, height)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    container.appendChild(renderer.domElement)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.07
    controls.autoRotate = autoRotate
    controls.autoRotateSpeed = 0.8
    controls.minDistance = 1.0
    controls.maxDistance = 12

    const group = new THREE.Group()
    scene.add(group)

    scene.add(new THREE.HemisphereLight('#f4f0e8', '#203033', 2.2))
    const keyLight = new THREE.DirectionalLight('#ffffff', 2.6)
    keyLight.position.set(4, 5, 3)
    scene.add(keyLight)

    const floor = new THREE.GridHelper(5.5, 22, '#39565c', '#1f292c')
    floor.position.y = -1.28
    floor.material.transparent = true
    floor.material.opacity = 0.46
    scene.add(floor)

    const color = scan?.color ?? '#4fd1c5'

    const fitModel = (model) => {
      const box = new THREE.Box3().setFromObject(model)
      const center = box.getCenter(new THREE.Vector3())
      const size = box.getSize(new THREE.Vector3())
      const maxDim = Math.max(size.x, size.y, size.z)
      const scale = 2.5 / Math.max(maxDim, 0.001)

      onDimensions?.({
        width: size.x,
        height: size.y,
        depth: size.z,
        longest: maxDim,
      })
      model.position.sub(center)
      model.scale.setScalar(scale)
      group.add(model)
      setLoadedUrl(modelUrl)
    }

    if (modelUrl.endsWith('.glb') || modelUrl.includes('.glb')) {
      const loader = new GLTFLoader()
      loader.load(
        modelUrl,
        (gltf) => fitModel(gltf.scene),
        undefined,
        (err) => {
          console.warn('GLB load failed:', err)
          setErrorUrl(modelUrl)
        },
      )
    } else if (modelUrl.endsWith('.obj') || modelUrl.includes('.obj')) {
      const loader = new OBJLoader()
      loader.load(
        modelUrl,
        (obj) => {
          obj.traverse((child) => {
            if (child.isMesh) {
              child.material = new THREE.MeshStandardMaterial({
                color,
                roughness: 0.5,
                metalness: 0.08,
                wireframe: renderMode === 'mesh',
              })
            }
          })
          fitModel(obj)
        },
        undefined,
        (err) => {
          console.warn('OBJ load failed:', err)
          setErrorUrl(modelUrl)
        },
      )
    }

    const resizeObserver = new ResizeObserver(([entry]) => {
      const nextWidth = entry.contentRect.width || width
      const nextHeight = entry.contentRect.height || height
      camera.aspect = nextWidth / nextHeight
      camera.updateProjectionMatrix()
      renderer.setSize(nextWidth, nextHeight)
    })
    resizeObserver.observe(container)

    let frameId = 0
    const animate = () => {
      if (autoRotate) group.rotation.y += 0.0025
      controls.autoRotate = autoRotate
      controls.update()
      renderer.render(scene, camera)
      frameId = window.requestAnimationFrame(animate)
    }
    animate()

    return () => {
      window.cancelAnimationFrame(frameId)
      resizeObserver.disconnect()
      controls.dispose()
      scene.traverse((node) => {
        if (node.geometry) node.geometry.dispose()
        if (node.material) {
          const materials = Array.isArray(node.material) ? node.material : [node.material]
          materials.forEach((material) => {
            if (material.map) material.map.dispose()
            material.dispose()
          })
        }
      })
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [autoRotate, onDimensions, renderMode, scan])

  const modelUrl = scan?.model_url || ''
  const hasModel = Boolean(modelUrl && scan?.status === 'ready')
  const isSupportedModel = modelUrl.endsWith('.glb') || modelUrl.includes('.glb') || modelUrl.endsWith('.obj') || modelUrl.includes('.obj')
  const displayState = !hasModel ? 'empty' : !isSupportedModel || errorUrl === modelUrl ? 'error' : loadedUrl === modelUrl ? 'loaded' : 'loading'

  return (
    <div className="scan-viewer" ref={containerRef} aria-label="Interactive 3D scan viewer">
      {displayState !== 'loaded' && (
        <div className="scan-viewer-message">
          {displayState === 'loading' && 'Loading reconstructed model'}
          {displayState === 'error' && 'No valid reconstructed model was produced'}
          {displayState === 'empty' && 'Process photos to generate a real model'}
        </div>
      )}
    </div>
  )
}


function App() {
  const fileInputRef = useRef(null)
  const videoRef = useRef(null)
  const streamRef = useRef(null)
  const recorderRef = useRef(null)
  const recordedChunksRef = useRef([])
  const pollTimerRef = useRef(null)

  const [mode, setMode] = useState('object')
  const [photos, setPhotos] = useState([])
  const [scans, setScans] = useState([])
  const [selectedScanId, setSelectedScanId] = useState(null)
  const [quality, setQuality] = useState(78)
  const [detail, setDetail] = useState(64)
  const [progress, setProgress] = useState(0)
  const [stage, setStage] = useState(0)
  const [stageName, setStageName] = useState('')
  const [isProcessing, setIsProcessing] = useState(false)
  const [isUploading, setIsUploading] = useState(false)
  const [cameraOpen, setCameraOpen] = useState(false)
  const [cameraError, setCameraError] = useState('')
  const [isRecording, setIsRecording] = useState(false)
  const [recordingSeconds, setRecordingSeconds] = useState(0)
  const [autoRotate, setAutoRotate] = useState(true)
  const [renderMode, setRenderMode] = useState('surface')
  const [modelDimensionState, setModelDimensionState] = useState(null)
  const [calibrationByScan, setCalibrationByScan] = useState({})
  const [errorMessage, setErrorMessage] = useState('')
  const [isLiveScanning, setIsLiveScanning] = useState(false)
  const [activeLiveScanId, setActiveLiveScanId] = useState(null)
  const [apiStatus, setApiStatus] = useState({
    state: 'checking',
    message: 'Checking backend',
    label: '',
  })
  const selectedScan = scans.find((scan) => scan.id === selectedScanId) ?? scans[0]
  const activeScanId = selectedScan?.id ?? null
  const modelDimensions = modelDimensionState?.scanId === activeScanId ? modelDimensionState.dimensions : null
  const calibrationLength = activeScanId ? calibrationByScan[activeScanId] ?? '' : ''

  const calibratedDimensions = useMemo(() => {
    if (!modelDimensions?.longest) return null

    const knownLength = Number(calibrationLength)
    const scale = Number.isFinite(knownLength) && knownLength > 0
      ? knownLength / modelDimensions.longest
      : 1
    const unit = scale === 1 ? 'model units' : 'm'

    return {
      width: modelDimensions.width * scale,
      height: modelDimensions.height * scale,
      depth: modelDimensions.depth * scale,
      unit,
      calibrated: scale !== 1,
    }
  }, [calibrationLength, modelDimensions])

  const handleModelDimensions = useCallback((dimensions) => {
    setModelDimensionState({
      scanId: activeScanId,
      dimensions,
    })
  }, [activeScanId])

  const updateCalibrationLength = useCallback((value) => {
    if (!activeScanId) return
    setCalibrationByScan((current) => ({
      ...current,
      [activeScanId]: value,
    }))
  }, [activeScanId])

  const sourceStats = useMemo(() => {
    const count = photos.length
    return {
      count,
      coverage: Math.min(98, Math.round(34 + count * 5.2 + quality * 0.18)),
      points: Math.round(22000 + count * 3600 + quality * 300 + detail * 520),
    }
  }, [detail, photos.length, quality])


  // ── Load scans from Supabase on mount ──
  useEffect(() => {
    async function loadScans() {
      try {
        const data = await fetchScans()
        if (data.scans?.length) {
          setScans(data.scans)
          setSelectedScanId(data.scans[0].id)
        }
      } catch (err) {
        console.error('Failed to load scans:', err)
      }
    }
    loadScans()
  }, [])

  useEffect(() => {
    let isMounted = true

    async function refreshApiStatus() {
      try {
        const health = await checkApiHealth()
        if (!isMounted) return
        setApiStatus({
          state: 'online',
          message: health.message,
          label: health.label,
        })
      } catch (err) {
        if (!isMounted) return
        setApiStatus({
          state: 'offline',
          message: 'Backend unreachable',
          label: err.message,
        })
      }
    }

    refreshApiStatus()
    const timer = window.setInterval(refreshApiStatus, 15000)

    return () => {
      isMounted = false
      window.clearInterval(timer)
    }
  }, [])


  // ── Photo handling ──
  const addFiles = useCallback(async (fileList) => {
    const files = Array.from(fileList ?? [])
    const imageFiles = files.filter((file) => file.type.startsWith('image/'))
    const videoFiles = files.filter((file) => file.type.startsWith('video/'))

    if (!imageFiles.length && !videoFiles.length) return

    setErrorMessage('')

    try {
      const imagePhotos = await Promise.all(imageFiles.map(readImageFile))
      const videoPhotos = (await Promise.all(videoFiles.map((file) => extractVideoFrames(file)))).flat()
      const nextPhotos = [...imagePhotos, ...videoPhotos].slice(0, MAX_PHOTOS)

      setPhotos((current) => [...nextPhotos, ...current].slice(0, MAX_PHOTOS))
    } catch (err) {
      console.error('Video frame extraction failed:', err)
      setErrorMessage('Could not read that video. Try MP4/MOV or upload photos instead.')
    }
  }, [])

  const handleDrop = useCallback(
    (event) => {
      event.preventDefault()
      addFiles(event.dataTransfer.files)
    },
    [addFiles],
  )

  const stopCamera = useCallback(() => {
    if (recorderRef.current?.state === 'recording') {
      recorderRef.current.stop()
    }
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    if (videoRef.current) videoRef.current.srcObject = null
    setCameraOpen(false)
    setIsRecording(false)
  }, [])

  const startCamera = useCallback(async () => {
    setCameraError('')
    try {
      if (streamRef.current) {
        setCameraOpen(true)
        return streamRef.current
      }

      const stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: { ideal: 'environment' },
          width: { ideal: 1280 },
          height: { ideal: 720 },
        },
        audio: false,
      })
      streamRef.current = stream
      setCameraOpen(true)
      return stream
    } catch {
      setCameraError('Camera unavailable')
      stopCamera()
      return null
    }
  }, [stopCamera])

  useEffect(() => {
    if (cameraOpen && videoRef.current && streamRef.current) {
      videoRef.current.srcObject = streamRef.current
      videoRef.current.play().catch(() => setCameraError('Camera paused'))
    }
  }, [cameraOpen])

  useEffect(() => () => stopCamera(), [stopCamera])

  const capturePhoto = useCallback(() => {
    const video = videoRef.current
    if (!video || video.readyState < 2) return

    const canvas = document.createElement('canvas')
    canvas.width = video.videoWidth || 1280
    canvas.height = video.videoHeight || 720
    const context = canvas.getContext('2d')
    context.drawImage(video, 0, 0, canvas.width, canvas.height)

    canvas.toBlob(
      (blob) => {
        if (!blob) return
        const file = new File([blob], `camera-capture-${Date.now()}.jpg`, { type: 'image/jpeg' })
        const src = URL.createObjectURL(blob)

        setPhotos((current) =>
          [
            {
              id: makeId(),
              name: file.name,
              src,
              file,
              origin: 'camera',
            },
            ...current,
          ].slice(0, MAX_PHOTOS),
        )
      },
      'image/jpeg',
      0.88,
    )
  }, [])

  const startScanRecording = useCallback(async () => {
    setCameraError('')
    setErrorMessage('')

    const stream = streamRef.current ?? await startCamera()
    if (!stream) return
    if (!window.MediaRecorder) {
      setCameraError('Video recording is not supported in this browser')
      return
    }

    const preferredTypes = [
      'video/webm;codecs=vp9',
      'video/webm;codecs=vp8',
      'video/webm',
      'video/mp4',
    ]
    const mimeType = preferredTypes.find((type) => MediaRecorder.isTypeSupported(type)) || ''

    recordedChunksRef.current = []
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
    recorderRef.current = recorder

    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) recordedChunksRef.current.push(event.data)
    }

    recorder.onstop = async () => {
      setIsRecording(false)
      setRecordingSeconds(0)

      const type = recorder.mimeType || 'video/webm'
      const extension = type.includes('mp4') ? 'mp4' : 'webm'
      const blob = new Blob(recordedChunksRef.current, { type })
      recordedChunksRef.current = []

      if (!blob.size) return

      try {
        const file = new File([blob], `space-scan-${Date.now()}.${extension}`, { type })
        const frames = await extractVideoFrames(file, SCAN_RECORDING_FRAME_COUNT)
        setMode('space')
        setPhotos((current) => [...frames, ...current].slice(0, MAX_PHOTOS))
      } catch (err) {
        console.error('Recorded scan extraction failed:', err)
        setErrorMessage('Could not turn that recording into scan frames. Try a slower walkthrough.')
      }
    }

    recorder.start(1000)
    setIsRecording(true)
    setRecordingSeconds(0)
  }, [startCamera])

  const stopScanRecording = useCallback(() => {
    if (recorderRef.current?.state === 'recording') {
      recorderRef.current.stop()
    }
  }, [])

  useEffect(() => {
    if (!isRecording) return undefined

    const timer = window.setInterval(() => {
      setRecordingSeconds((value) => value + 1)
    }, 1000)

    return () => window.clearInterval(timer)
  }, [isRecording])

  const removePhoto = useCallback((id) => {
    setPhotos((current) => current.filter((photo) => photo.id !== id))
  }, [])


  // ── Polling for processing status ──
  const startPolling = useCallback((scanId) => {
    if (pollTimerRef.current) clearInterval(pollTimerRef.current)

    pollTimerRef.current = setInterval(async () => {
      try {
        const statusData = await getStatus(scanId)

        setStage(statusData.stage ?? 0)
        setStageName(statusData.stage_name ?? '')
        setProgress(statusData.progress ?? 0)

        if (statusData.status === 'ready') {
          clearInterval(pollTimerRef.current)
          pollTimerRef.current = null
          setIsProcessing(false)
          setProgress(100)

          // Reload scans to get updated data
          const data = await fetchScans()
          if (data.scans?.length) {
            setScans(data.scans)
            setSelectedScanId(scanId)
          }
        } else if (statusData.status === 'failed') {
          clearInterval(pollTimerRef.current)
          pollTimerRef.current = null
          setIsProcessing(false)
          setErrorMessage(statusData.error || 'Processing failed')
        }
      } catch (err) {
        console.error('Polling error:', err)
      }
    }, 2000)
  }, [])

  useEffect(() => {
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current)
    }
  }, [])


  // ── Real reconstruction pipeline ──
  const reconstructScan = useCallback(async () => {
    if (!photos.length || isProcessing || isUploading) return

    setErrorMessage('')
    setIsUploading(true)

    try {
      // Step 1: Create scan in Supabase
      const scanName = `Capture ${scans.length + 1}`
      const createResult = await createScan(scanName, mode)
      const newScanId = createResult.scan.id

      // Step 2: Upload photos to backend → Supabase storage
      const files = photos.map((p) => p.file).filter(Boolean)
      if (files.length < 1) {
        setErrorMessage('Need at least 1 photo with file data. Try re-uploading.')
        setIsUploading(false)
        return
      }

      await uploadPhotos(newScanId, files)
      setIsUploading(false)

      // Step 3: Start processing
      setIsProcessing(true)
      setProgress(0)
      setStage(0)
      setStageName('Starting...')

      await startProcessing(newScanId, quality, detail)

      // Step 4: Poll for status
      startPolling(newScanId)

      // Clear photos from the upload panel
      setPhotos([])

    } catch (err) {
      console.error('Reconstruction error:', err)
      setErrorMessage(err.message || 'Failed to start reconstruction')
      setIsUploading(false)
      setIsProcessing(false)
    }
  }, [detail, isProcessing, isUploading, mode, photos, quality, scans.length, startPolling])


  const cancelReconstruction = useCallback(() => {
    if (pollTimerRef.current) clearInterval(pollTimerRef.current)
    pollTimerRef.current = null
    setIsProcessing(false)
    setProgress(0)
    setStage(0)
  }, [])


  const removeScan = useCallback(
    async (event, id) => {
      event.stopPropagation()

      try {
        await apiDeleteScan(id)
        setScans((current) => current.filter((scan) => scan.id !== id))
        if (selectedScanId === id) {
          setSelectedScanId(scans[0]?.id ?? null)
        }
      } catch (err) {
        console.error('Delete failed:', err)
      }
    },
    [selectedScanId, scans],
  )


  const exportScan = useCallback(
    (format) => {
      if (!selectedScan?.model_url) return

      // Construct the download URL based on format
      const modelUrl = selectedScan.model_url
      const link = document.createElement('a')
      link.href = modelUrl
      link.download = `${slugify(selectedScan.name)}.${format}`
      link.target = '_blank'
      link.click()
    },
    [selectedScan],
  )

  const startLiveScan = useCallback(async () => {
    try {
      setErrorMessage('')
      const scanName = `Live Space Capture ${scans.length + 1}`
      const createResult = await createScan(scanName, 'space')
      const newScanId = createResult.scan.id

      setActiveLiveScanId(newScanId)
      setIsLiveScanning(true)
      setSelectedScanId(newScanId)
    } catch (err) {
      console.error('Failed to create live scan:', err)
      setErrorMessage(err.message || 'Failed to start live scan')
    }
  }, [scans.length])

  const handleLiveScanFinalized = useCallback(async () => {
    // Reset live scanning state
    setIsLiveScanning(false)

    // Start processing the captured scan
    if (activeLiveScanId) {
      try {
        setIsProcessing(true)
        setProgress(0)
        setStage(0)
        setStageName('Starting...')

        await startProcessing(activeLiveScanId, quality, detail)
        startPolling(activeLiveScanId)
      } catch (err) {
        console.error('Failed to start processing:', err)
        setErrorMessage(err.message || 'Failed to start processing')
        setIsProcessing(false)
      }
    }
  }, [activeLiveScanId, detail, quality, startPolling])

  return (
    <main className="app-shell">
      <aside className="panel capture-panel">
        <header className="brand-row">
          <div className="brand-mark">
            <ScanLine size={22} />
          </div>
          <div>
            <h1>Visionaire</h1>
            <p>3D capture studio</p>
          </div>
          <div className={`api-status ${apiStatus.state}`} title={apiStatus.label}>
            {apiStatus.state === 'checking' && <LoaderCircle size={14} />}
            {apiStatus.state === 'online' && <CheckCircle2 size={14} />}
            {apiStatus.state === 'offline' && <AlertTriangle size={14} />}
            <span>{apiStatus.state === 'online' ? 'Backend online' : apiStatus.message}</span>
          </div>
        </header>

        <section className="panel-section">
          <div className="section-heading">
            <h2>Capture</h2>
            <span>{photos.length}/{MAX_PHOTOS}</span>
          </div>

          <div className="segmented-control" aria-label="Capture mode">
            <button className={mode === 'object' ? 'active' : ''} type="button" onClick={() => setMode('object')}>
              <Box size={17} />
              Object
            </button>
            <button className={mode === 'space' ? 'active' : ''} type="button" onClick={() => setMode('space')}>
              <Layers size={17} />
              Space
            </button>
          </div>

          {mode === 'space' && !isLiveScanning && (
            <button className="button primary" style={{ marginBottom: '16px' }} type="button" onClick={startLiveScan}>
              🎥 Start Live Scan
            </button>
          )}

          <div className="capture-actions">
            <input
              ref={fileInputRef}
              id="photo-upload"
              type="file"
              accept="image/*,video/*"
              multiple
              onChange={(event) => {
                addFiles(event.target.files)
                event.target.value = ''
              }}
            />
            <label className="button primary" htmlFor="photo-upload">
              <Upload size={17} />
              Upload
            </label>
            <button className="button" type="button" onClick={cameraOpen ? stopCamera : startCamera}>
              {cameraOpen ? <Square size={17} /> : <Camera size={17} />}
              {cameraOpen ? 'Stop' : 'Camera'}
            </button>
            <button className={`button ${isRecording ? '' : 'primary'}`} type="button" onClick={isRecording ? stopScanRecording : startScanRecording}>
              {isRecording ? <Square size={17} /> : <Video size={17} />}
              {isRecording ? 'End scan' : 'Video scan'}
            </button>
          </div>

          <button className="drop-zone" type="button" onClick={() => fileInputRef.current?.click()} onDrop={handleDrop} onDragOver={(event) => event.preventDefault()}>
            <ImagePlus size={22} />
            <span>Drop video or photos</span>
            <small>MP4, MOV, JPG, PNG</small>
          </button>

          {cameraOpen && (
            <div className="camera-box">
              <video ref={videoRef} playsInline muted />
              {isRecording && (
                <div className="recording-strip">
                  <span />
                  <strong>Scanning space</strong>
                  <small>{recordingSeconds}s</small>
                </div>
              )}
              <div className="camera-controls">
                <button className="button primary" type="button" onClick={capturePhoto} disabled={isRecording}>
                  <Aperture size={17} />
                  Capture
                </button>
                <button className="button" type="button" onClick={isRecording ? stopScanRecording : startScanRecording}>
                  {isRecording ? <Square size={17} /> : <Video size={17} />}
                  {isRecording ? 'Stop recording' : 'Record walkthrough'}
                </button>
              </div>
            </div>
          )}
          {cameraError && <p className="status-text warning">{cameraError}</p>}
        </section>

        <section className="panel-section">
          <div className="section-heading">
            <h2>Sources</h2>
            <span>{sourceStats.coverage}% coverage</span>
          </div>

          <div className="photo-grid">
            {photos.map((photo) => (
              <figure className="photo-tile" key={photo.id}>
                <img src={photo.src} alt={photo.name} />
                <button type="button" aria-label={`Remove ${photo.name}`} onClick={() => removePhoto(photo.id)}>
                  <Trash2 size={14} />
                </button>
              </figure>
            ))}
            {!photos.length && (
              <div className="empty-state">
                <Sparkles size={18} />
                <span>Ready for video</span>
              </div>
            )}
          </div>

          {errorMessage && (
            <div className="error-banner">
              <AlertTriangle size={16} />
              <span>{errorMessage}</span>
              <button type="button" onClick={() => setErrorMessage('')}>×</button>
            </div>
          )}

          <div className="process-row">
            <button
              className="button primary process-button"
              type="button"
              disabled={!photos.length || isProcessing || isUploading}
              onClick={reconstructScan}
            >
              {isUploading ? (
                <>
                  <LoaderCircle size={17} className="spin" />
                  Uploading…
                </>
              ) : isProcessing ? (
                <>
                  <LoaderCircle size={17} className="spin" />
                  Processing…
                </>
              ) : (
                <>
                  <Play size={17} />
                  Process
                </>
              )}
            </button>
            {isProcessing && (
              <button className="icon-button" type="button" aria-label="Cancel reconstruction" onClick={cancelReconstruction}>
                <Square size={17} />
              </button>
            )}
          </div>
        </section>
      </aside>

      <section className="viewer-panel">
        <header className="viewer-toolbar">
          <div>
            <span className="eyebrow">{selectedScan?.status ?? 'No scans'}</span>
            <h2>{selectedScan?.name ?? 'Visionaire'}</h2>
          </div>
          <div className="toolbar-actions">
            <button className={`icon-button ${autoRotate ? 'active' : ''}`} type="button" aria-label="Toggle rotation" onClick={() => setAutoRotate((value) => !value)}>
              <RotateCcw size={18} />
            </button>
            <button className="button" type="button" onClick={() => exportScan('obj')} disabled={!selectedScan?.model_url}>
              <Download size={17} />
              OBJ
            </button>
            <button className="button primary" type="button" onClick={() => exportScan('glb')} disabled={!selectedScan?.model_url}>
              <Download size={17} />
              GLB
            </button>
          </div>
        </header>

        <div className="viewer-surface">
          {isLiveScanning && activeLiveScanId ? (
            <LiveCameraScanner
              scanId={activeLiveScanId}
              quality={quality}
              onScanFinalized={handleLiveScanFinalized}
            />
          ) : selectedScan ? (
            <ScanViewer scan={selectedScan} autoRotate={autoRotate} renderMode={renderMode} onDimensions={handleModelDimensions} />
          ) : (
            <div className="empty-viewer">
              <ScanLine size={48} />
              <p>Upload a video and process to see your 3D model</p>
            </div>
          )}

          {isProcessing && (
            <div className="process-overlay">
              <div className="process-meter">
                <LoaderCircle className="spin" size={22} />
                <div>
                  <strong>{stageName || PIPELINE[stage] || 'Processing'}</strong>
                  <span>{progress}%</span>
                </div>
                <progress value={progress} max="100" />
              </div>
            </div>
          )}
        </div>

        <footer className="viewer-footer">
          <div className="segmented-control compact" aria-label="Render mode">
            <button className={renderMode === 'surface' ? 'active' : ''} type="button" onClick={() => setRenderMode('surface')}>
              <CircleDot size={16} />
              Surface
            </button>
            <button className={renderMode === 'points' ? 'active' : ''} type="button" onClick={() => setRenderMode('points')}>
              <Grid3X3 size={16} />
              Points
            </button>
            <button className={renderMode === 'mesh' ? 'active' : ''} type="button" onClick={() => setRenderMode('mesh')}>
              <Box size={16} />
              Mesh
            </button>
          </div>
          <div className="pipeline-list" aria-label="Reconstruction pipeline">
            {PIPELINE.map((item, index) => (
              <span className={index <= stage && (isProcessing || progress === 100) ? 'complete' : ''} key={item}>
                {index <= stage && (isProcessing || progress === 100) ? <CheckCircle2 size={15} /> : <CircleDot size={15} />}
                {item}
              </span>
            ))}
          </div>
        </footer>
      </section>

      <aside className="panel inspector-panel">
        <section className="panel-section">
          <div className="section-heading">
            <h2>Scan</h2>
            <span>{selectedScan?.mode ?? '—'}</span>
          </div>

          <div className="metric-grid">
            <div className="metric">
              <Gauge size={18} />
              <span>Quality</span>
              <strong>{selectedScan?.quality ?? 0}%</strong>
            </div>
            <div className="metric">
              <Grid3X3 size={18} />
              <span>Coverage</span>
              <strong>{selectedScan?.coverage ?? 0}%</strong>
            </div>
            <div className="metric">
              <CircleDot size={18} />
              <span>Points</span>
              <strong>{(selectedScan?.points ?? 0).toLocaleString()}</strong>
            </div>
            <div className="metric">
              <Video size={18} />
              <span>Photos</span>
              <strong>{selectedScan?.photo_count ?? 0}</strong>
            </div>
          </div>
        </section>

        <section className="panel-section">
          <div className="section-heading">
            <h2>Dimensions</h2>
            <span>{calibratedDimensions?.calibrated ? 'calibrated' : 'estimate'}</span>
          </div>

          <div className="dimension-grid">
            <div className="dimension">
              <span>Width</span>
              <strong>{formatMeasurement(calibratedDimensions?.width, calibratedDimensions?.unit)}</strong>
            </div>
            <div className="dimension">
              <span>Height</span>
              <strong>{formatMeasurement(calibratedDimensions?.height, calibratedDimensions?.unit)}</strong>
            </div>
            <div className="dimension">
              <span>Depth</span>
              <strong>{formatMeasurement(calibratedDimensions?.depth, calibratedDimensions?.unit)}</strong>
            </div>
          </div>

          <label className="calibration-row">
            <span>
              <Ruler size={17} />
              Known longest side
            </span>
            <input
              type="number"
              min="0"
              step="0.01"
              placeholder="meters"
              value={calibrationLength}
              onChange={(event) => updateCalibrationLength(event.target.value)}
            />
          </label>
          <p className="helper-text">Video gives relative scale. Enter one real measurement for meter-based dimensions.</p>
        </section>

        <section className="panel-section">
          <div className="section-heading">
            <h2>Settings</h2>
            <span>{sourceStats.points.toLocaleString()} pts</span>
          </div>

          <label className="slider-row">
            <span>
              <Gauge size={17} />
              Quality
            </span>
            <strong>{quality}%</strong>
            <input type="range" min="40" max="100" value={quality} onChange={(event) => setQuality(Number(event.target.value))} />
          </label>

          <label className="slider-row">
            <span>
              <SlidersHorizontal size={17} />
              Detail
            </span>
            <strong>{detail}%</strong>
            <input type="range" min="35" max="100" value={detail} onChange={(event) => setDetail(Number(event.target.value))} />
          </label>
        </section>

        <section className="panel-section gallery-section">
          <div className="section-heading">
            <h2>Gallery</h2>
            <span>{scans.length}</span>
          </div>

          <div className="scan-list">
            {scans.map((scan) => (
              <button className={`scan-card ${selectedScanId === scan.id ? 'active' : ''}`} type="button" key={scan.id} onClick={() => setSelectedScanId(scan.id)}>
                <span className="scan-swatch" style={{ background: scan.mode === 'object' ? '#4fd1c5' : '#ffb35c' }} />
                <span>
                  <strong>{scan.name}</strong>
                  <small>{formatDate(scan.created_at)} / {scan.photo_count} photos / {scan.status}</small>
                </span>
                <span className="scan-delete" role="button" tabIndex={0} onClick={(event) => removeScan(event, scan.id)} onKeyDown={(event) => event.key === 'Enter' && removeScan(event, scan.id)}>
                  <Trash2 size={15} />
                </span>
              </button>
            ))}
            {!scans.length && (
              <div className="empty-state">
                <Sparkles size={18} />
                <span>No scans yet</span>
              </div>
            )}
          </div>
        </section>
      </aside>
    </main>
  )
}

export default App
