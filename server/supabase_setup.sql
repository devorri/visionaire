-- ScanForge Web — Supabase Database Setup
-- Run this SQL in your Supabase SQL Editor:
-- https://supabase.com/dashboard/project/qmlfwqjwxxohxbhpdvrl/sql/new

-- ════════════════════════════════════════════════════════════════════
-- Table: scans
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS scans (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL DEFAULT 'Untitled Scan',
  mode TEXT NOT NULL DEFAULT 'object',
  quality INTEGER NOT NULL DEFAULT 0,
  detail INTEGER NOT NULL DEFAULT 0,
  coverage INTEGER NOT NULL DEFAULT 0,
  texture_score INTEGER NOT NULL DEFAULT 0,
  points INTEGER NOT NULL DEFAULT 0,
  photo_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'uploading',
  model_url TEXT,
  thumbnail_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ════════════════════════════════════════════════════════════════════
-- Table: scan_photos
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS scan_photos (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_id UUID NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  file_name TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scan_photos_scan_id ON scan_photos(scan_id);

-- ════════════════════════════════════════════════════════════════════
-- Row Level Security — Allow public access (no auth)
-- ════════════════════════════════════════════════════════════════════

ALTER TABLE scans ENABLE ROW LEVEL SECURITY;
ALTER TABLE scan_photos ENABLE ROW LEVEL SECURITY;

-- Allow all operations for anonymous users (no auth mode)
CREATE POLICY "Allow all on scans" ON scans
  FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "Allow all on scan_photos" ON scan_photos
  FOR ALL USING (true) WITH CHECK (true);

-- ════════════════════════════════════════════════════════════════════
-- Storage Buckets (create this in the Supabase dashboard)
-- Go to: Storage → New Bucket
--   Name: "gallery"  — Set to Public
-- ════════════════════════════════════════════════════════════════════

-- If you want to create the bucket via SQL (optional):
INSERT INTO storage.buckets (id, name, public)
VALUES ('gallery', 'gallery', true)
ON CONFLICT (id) DO NOTHING;

-- Storage policies — allow public uploads/reads/deletes on the gallery bucket
CREATE POLICY "Allow public upload to gallery"
ON storage.objects FOR INSERT
WITH CHECK (bucket_id = 'gallery');

CREATE POLICY "Allow public read from gallery"
ON storage.objects FOR SELECT
USING (bucket_id = 'gallery');

CREATE POLICY "Allow public delete from gallery"
ON storage.objects FOR DELETE
USING (bucket_id = 'gallery');

