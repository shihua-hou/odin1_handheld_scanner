"""Bounded, thread-safe latest compressed camera frame for the web preview."""
import threading
import time
from aiohttp import web


class CameraPreview:
    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._updated = 0.0

    def ingest(self, message):
        now = time.monotonic()
        with self._lock:
            if now - self._updated < 0.1:
                return
        if not message.data or len(message.data) > 8 * 1024 * 1024:
            return
        payload = bytes(message.data)
        if payload.startswith(b'\xff\xd8'):
            mime = 'image/jpeg'
        elif payload.startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        else:
            return
        with self._lock:
            self._frame = (payload, mime)
            self._updated = now

    def snapshot(self):
        with self._lock:
            age = time.monotonic() - self._updated if self._frame else None
            return self._frame, age


def register_camera_preview(app, preview):
    async def frame(request):
        latest, age = preview.snapshot()
        headers = {'Cache-Control': 'no-store'}
        if latest is None or age > 3:
            return web.Response(status=204, headers=headers)
        payload, mime = latest
        headers['X-Camera-Age'] = f'{age:.3f}'
        return web.Response(body=payload, content_type=mime, headers=headers)

    app.router.add_get('/api/camera/frame', frame)
