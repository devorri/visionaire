/**
 * ScanForge API client.
 * Communicates with the FastAPI backend for scan operations.
 */

const configuredApiUrl = import.meta.env.VITE_API_URL?.trim()
const isLocalPage = ['localhost', '127.0.0.1', ''].includes(window.location.hostname)
const pointsToLocalhost = configuredApiUrl && /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?/i.test(configuredApiUrl)
const legacyApiUrl = 'https://visionaire-api.onrender.com'
const productionApiUrl = 'https://visionaire.onrender.com'

const API_URL = configuredApiUrl === legacyApiUrl
  ? productionApiUrl
  : pointsToLocalhost && !isLocalPage
  ? ''
  : configuredApiUrl || (isLocalPage ? 'http://localhost:8000' : '')

function apiTargetLabel() {
  return API_URL || window.location.origin
}

async function request(path, options = {}) {
  const url = `${API_URL}${path}`
  let response

  try {
    response = await fetch(url, {
      ...options,
      headers: {
        ...options.headers,
      },
    })
  } catch {
    const target = API_URL || 'this site'
    throw new Error(`Could not reach the API at ${target}. Check VITE_API_URL for the deployed backend URL.`)
  }

  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `API error: ${response.status}`)
  }

  return response.json()
}

export async function checkApiHealth() {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 4500)

  try {
    const data = await request('/api/health', { signal: controller.signal })
    return {
      ok: true,
      label: apiTargetLabel(),
      message: data.service || 'Backend online',
    }
  } finally {
    window.clearTimeout(timeout)
  }
}


/**
 * Create a new scan session.
 */
export async function createScan(name = 'Untitled Scan', mode = 'object') {
  const params = new URLSearchParams({ name, mode })
  return request(`/api/scans?${params}`, { method: 'POST' })
}


/**
 * Upload photo files to a scan.
 */
export async function uploadPhotos(scanId, files) {
  const formData = new FormData()
  for (const file of files) {
    formData.append('files', file)
  }
  return request(`/api/scans/${scanId}/photos`, {
    method: 'POST',
    body: formData,
  })
}


/**
 * Start photogrammetry processing.
 */
export async function startProcessing(scanId, quality = 78, detail = 64) {
  const params = new URLSearchParams({
    quality: quality.toString(),
    detail: detail.toString(),
  })
  return request(`/api/scans/${scanId}/process?${params}`, { method: 'POST' })
}


/**
 * Poll processing status.
 */
export async function getStatus(scanId) {
  return request(`/api/scans/${scanId}/status`)
}


/**
 * Fetch all scans.
 */
export async function fetchScans() {
  return request('/api/scans')
}


/**
 * Fetch a single scan.
 */
export async function fetchScan(scanId) {
  return request(`/api/scans/${scanId}`)
}


/**
 * Delete a scan.
 */
export async function deleteScan(scanId) {
  return request(`/api/scans/${scanId}`, { method: 'DELETE' })
}


// ── Measurement Calibration ──────────────────────────────────────────

/**
 * Calibrate measurement scale using ArUco marker detection.
 */
export async function calibrateWithAruco(scanId, markerSize = '10cm') {
  const params = new URLSearchParams({ marker_size: markerSize })
  return request(`/api/scans/${scanId}/calibration/aruco?${params}`, { method: 'POST' })
}


/**
 * Calibrate using manually selected reference distance.
 */
export async function calibrateManual(scanId, referenceDistanceMeters) {
  const params = new URLSearchParams({ reference_distance_meters: referenceDistanceMeters })
  return request(`/api/scans/${scanId}/calibration/manual?${params}`, { method: 'POST' })
}


/**
 * Reset calibration for a scan.
 */
export async function resetCalibration(scanId) {
  return request(`/api/scans/${scanId}/calibration/reset`, { method: 'POST' })
}


/**
 * Get bounding box dimensions in meters.
 */
export async function getDimensions(scanId) {
  return request(`/api/scans/${scanId}/dimensions`)
}


// ── Progressive Refinement ──────────────────────────────────────────

/**
 * Initialize progressive refinement for a scan.
 */
export async function initializeRefiner(scanId) {
  return request(`/api/scans/${scanId}/refiner/initialize`, { method: 'POST' })
}


/**
 * Add a frame to progressive refinement.
 */
export async function addRefinementFrame(scanId, frameId, file) {
  const formData = new FormData()
  formData.append('file', file)

  const params = new URLSearchParams({ frame_id: frameId })
  return request(`/api/scans/${scanId}/refiner/add-frame?${params}`, {
    method: 'POST',
    body: formData,
  })
}


/**
 * Get refinement statistics.
 */
export async function getRefinementStats(scanId) {
  return request(`/api/scans/${scanId}/refiner/stats`)
}


// ── Live Camera Streaming ──────────────────────────────────────────

/**
 * Create a new live streaming session.
 */
export async function createLiveSession(quality = 60) {
  const params = new URLSearchParams({ quality: quality.toString() })
  return request(`/api/live-sessions?${params}`, { method: 'POST' })
}


/**
 * Get live session status.
 */
export async function getLiveSession(sessionId) {
  return request(`/api/live-sessions/${sessionId}`)
}


/**
 * Process a frame from live camera.
 */
export async function processLiveFrame(sessionId, file) {
  const formData = new FormData()
  formData.append('file', file)

  return request(`/api/live-sessions/${sessionId}/frame`, {
    method: 'POST',
    body: formData,
  })
}


/**
 * Finalize live session and save frames to scan.
 */
export async function finalizeLiveSession(sessionId, scanId) {
  const params = new URLSearchParams({ scan_id: scanId })
  return request(`/api/live-sessions/${sessionId}/finalize?${params}`, { method: 'POST' })
}


/**
 * List all active live sessions.
 */
export async function listLiveSessions() {
  return request('/api/live-sessions')
}

