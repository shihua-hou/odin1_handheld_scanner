import time
import unittest
from types import SimpleNamespace
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from camera_preview import CameraPreview, register_camera_preview


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.preview = CameraPreview()
        app = web.Application()
        register_camera_preview(app, self.preview)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_empty_and_stale_do_not_serve_old_frame(self):
        response = await self.client.get('/api/camera/frame')
        self.assertEqual(response.status, 204)
        self.preview.ingest(SimpleNamespace(data=b'\xff\xd8example'))
        self.preview._updated = time.monotonic() - 4
        response = await self.client.get('/api/camera/frame')
        self.assertEqual(response.status, 204)

    async def test_latest_frame_and_mime(self):
        data = b'\xff\xd8example'
        self.preview.ingest(SimpleNamespace(data=data))
        response = await self.client.get('/api/camera/frame')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, 'image/jpeg')
        self.assertEqual(await response.read(), data)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_unknown_encoding_does_not_replace_frame(self):
        self.preview.ingest(SimpleNamespace(data=b'unknown image data'))
        self.assertIsNone(self.preview.snapshot()[0])


if __name__ == '__main__':
    unittest.main()
