# ScanForge Web

A Polycam-style React web app for local 3D capture workflows.

## Features

- Photo upload and drag/drop sources
- Camera capture through the browser camera API
- Object and space scan modes
- Client-side reconstruction progress flow
- Interactive Three.js scan viewer
- Surface, point cloud, and mesh viewing modes
- Scan gallery with rebuild quality controls
- JSON metadata export
- OBJ mesh export

This is a complete local web app and prototype scanner experience. Real photogrammetry-grade reconstruction like Polycam uses native/cloud computer-vision pipelines; this version generates a working in-browser 3D preview and exportable procedural mesh from the capture session.

## Run

```bash
npm install
npm run dev
```

Open the local URL Vite prints, usually:

```text
http://127.0.0.1:5173/
```

## Build

```bash
npm run build
```
