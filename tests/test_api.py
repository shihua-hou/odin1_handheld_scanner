import asyncio
import tempfile
import unittest
from pathlib import Path
import numpy as np
from aiohttp.test_utils import TestClient, TestServer
from handheld.server import Runtime, make_app


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.runtime=Runtime(self.tmp.name,ros=False)
        self.client=TestClient(TestServer(make_app(self.runtime)))
        await self.client.start_server()
        self.headers={}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_auth_and_empty_state(self):
        self.assertEqual((await self.client.get('/api/status')).status,200)
        response=await self.client.get('/api/status',headers=self.headers)
        self.assertEqual(response.status,200)
        self.assertFalse((await response.json())['connected'])
        response=await self.client.post('/api/action/save',json={},headers=self.headers)
        self.assertEqual(response.status,400)
        self.assertEqual((await self.client.get('/')).status,200)

    async def test_save_download_edit_and_ws(self):
        scene=self.runtime.scene
        scene.capturing=True
        scene.ingest(np.array([[1,2,3],[2,3,4]],np.float32),np.array([[255,0,0],[0,255,0]],np.uint8),'odom',1)
        scene.capturing=False
        response=await self.client.post('/api/action/save',json={'name':'test'},headers=self.headers)
        self.assertEqual(response.status,200)
        saved=await response.json()
        response=await self.client.get('/api/scenes/'+saved['id']+'/ply',headers=self.headers)
        self.assertTrue((await response.read()).startswith(b'ply\n'))
        ws=await self.client.ws_connect('/ws')
        status=await ws.receive_json(timeout=3)
        self.assertEqual(status['points'],2)
        data=await ws.receive_bytes(timeout=3)
        self.assertEqual(len(data),56)
        await ws.close()
        response=await self.client.post('/api/action/edit',json={'action':'crop','min':[0,0,0],'max':[1.5,3,4]},headers=self.headers)
        self.assertEqual((await response.json())['points'],1)

    async def test_ws_without_token_and_cross_origin(self):
        ws=await self.client.ws_connect('/ws')
        self.assertEqual((await ws.receive_json(timeout=3))['type'],'status')
        await ws.close()
        headers={**self.headers,'Origin':'https://example.com'}
        response=await self.client.post('/api/action/pause',json={},headers=headers)
        self.assertEqual(response.status,403)

    async def test_driver_exit_pauses_and_requires_new(self):
        class Exited:
            def poll(self): return 1
        self.runtime.driver=Exited()
        scene=self.runtime.scene
        scene.capturing=True
        scene.ingest(np.array([[1,2,3]],np.float32),np.array([[1,2,3]],np.uint8),'odom',1)
        status=self.runtime.status()
        self.assertFalse(status['capturing'])
        self.assertTrue(scene.loaded)
        response=await self.client.post('/api/action/start',json={},headers=self.headers)
        self.assertEqual(response.status,400)

    async def test_restart_restores_saved_scene_as_edit_only(self):
        scene=self.runtime.scene
        scene.capturing=True
        scene.ingest(np.array([[1,2,3]],np.float32),np.array([[1,2,3]],np.uint8),'odom',1)
        scene.capturing=False
        scene.save('restore-test')
        restored=Runtime(self.tmp.name,ros=False)
        self.assertEqual(len(restored.scene.points),1)
        self.assertTrue(restored.scene.loaded)
        self.assertFalse(restored.scene.capturing)

    async def test_mapping_voxel_setting_persists(self):
        for value in [.001,1.,.005]:
            response=await self.client.post('/api/action/set-voxel',json={'voxel':value})
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['voxel'],value)
        restored=Runtime(self.tmp.name,ros=False)
        self.assertEqual(restored.default_voxel,.005)
        self.runtime.scene.capturing=True
        response=await self.client.post('/api/action/set-voxel',json={'voxel':.001})
        self.assertEqual(response.status,400)
        self.runtime.scene.capturing=False
        response=await self.client.post('/api/action/set-voxel',json={'voxel':0})
        self.assertEqual(response.status,400)

    async def test_spatial_manifest_detail_and_status_only_socket(self):
        scene=self.runtime.scene;scene.capturing=True
        scene.ingest(np.array([[0,0,0],[.01,0,0],[4,0,0]],np.float32),np.full((3,3),123,np.uint8),'odom',1)
        scene.capturing=False
        manifest=await (await self.client.get('/api/tiles')).json()
        self.assertEqual(manifest['total'],3)
        tile=manifest['tiles'][0]['pages'][0]
        url=f"/api/tiles/{tile['id']}?generation={manifest['generation']}&version={tile['version']}&level=0"
        response=await self.client.get(url)
        self.assertEqual(response.status,200);self.assertEqual(len(await response.read()),tile['count']*16)
        ws=await self.client.ws_connect('/ws?mode=spatial')
        self.assertEqual((await ws.receive_json(timeout=3))['points'],3)
        # Next message remains lightweight status; full-map binary is not sent.
        self.assertEqual((await ws.receive_json(timeout=3))['type'],'status');await ws.close()
        scene.edit('delete_box',dict(min=[-1,-1,-1],max=[1,1,1]))
        self.assertEqual((await self.client.get(url)).status,400)


if __name__=='__main__':
    unittest.main()
