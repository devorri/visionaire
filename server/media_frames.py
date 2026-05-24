"""Helpers for turning uploaded media into reconstruction-ready image bytes."""

import os
import tempfile

import cv2


def extract_video_frames(video_bytes: bytes, suffix: str = ".mp4", frame_count: int = 12) -> list[bytes]:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    try:
        capture = cv2.VideoCapture(tmp_path)
        if not capture.isOpened():
            return []

        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total_frames <= 0:
            return []

        frames = []
        for index in range(frame_count):
            frame_index = int((index + 0.5) * total_frames / frame_count)
            capture.set(cv2.CAP_PROP_POS_FRAMES, min(frame_index, total_frames - 1))
            ok, frame = capture.read()
            if not ok or frame is None:
                continue

            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            if ok:
                frames.append(encoded.tobytes())

        return frames
    finally:
        try:
            capture.release()
        except Exception:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
