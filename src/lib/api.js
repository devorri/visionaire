/**
 * ScanForge API client.
 * Communicates with the FastAPI backend for scan operations.
 */

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

async function request(path, options = {}) {
  const url = `${API_URL}${path}`
  const response = await fetch(url, {
    ...options,
    headers: {
      ...options.headers,
    },
  })

  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `API error: ${response.status}`)
  }

  return response.json()
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
