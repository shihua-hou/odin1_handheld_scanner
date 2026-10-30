import tempfile
import unittest
import zipfile
from pathlib import Path
import numpy as np
from handheld.core import Scene
from handheld.disk_scene import DiskScene,RECORD,PAGE_POINTS


class DiskSceneTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.scene=DiskScene(Path(self.tmp.name)/'maps',voxel=.001)

    def tearDown(self):
        self.scene.db.close();self.tmp.cleanup()

    def add(self,points,stamp=1):
        points=np.asarray(points,np.float32);self.scene.capturing=True
        self.scene.ingest(points,np.full((len(points),3),129,np.uint8),'odom',stamp)
        self.scene.capturing=False

    def test_disk_dedup_recovery_and_lossless_tiles(self):
        points=[[.01,0,0],[.02,0,0],[3,0,0]]
        self.add(points);self.add(points,2)
        self.assertEqual(len(self.scene.points),3)
        manifest=self.scene.manifest();self.assertEqual(len(manifest['tiles']),2)
        full=[]
        for block in manifest['tiles']:
            raw_size=0
            for tile in block['pages']:
                raw=self.scene.tile(tile['id'],0,manifest['generation'],tile['version']);raw_size+=len(raw)
                full.extend(np.frombuffer(raw,RECORD)['xyz'].tolist())
            coarse=self.scene.block_tile(block['id'][2:],5,manifest['generation'],block['version'])
            self.assertLessEqual(len(coarse),raw_size)
        np.testing.assert_allclose(sorted(full),sorted(points))
        self.assertIsNone(self.scene.status()['limit'])
        recovered=DiskScene(Path(self.tmp.name)/'maps')
        try:
            self.assertTrue(recovered.loaded);self.assertFalse(recovered.capturing)
            self.assertEqual(len(recovered.points),3);self.assertEqual(recovered.voxel,.001)
        finally:recovered.db.close()

    def test_spatial_edit_undo_and_stale_tile_rejection(self):
        self.add([[0,0,0],[.05,0,0],[3,0,0]])
        manifest=self.scene.manifest();tile=manifest['tiles'][0]['pages'][0]
        unaffected=self.scene.db.execute('SELECT data FROM chunks WHERE bx=1').fetchone()[0]
        self.scene.edit('delete_sphere',dict(center=[0,0,0],radius=.01))
        self.assertEqual(len(self.scene.points),2)
        self.assertEqual(self.scene.db.execute('SELECT data FROM chunks WHERE bx=1').fetchone()[0],unaffected)
        with self.assertRaises(ValueError):self.scene.tile(tile['id'],0,manifest['generation'],tile['version'])
        self.scene.edit('undo',{});self.assertEqual(len(self.scene.points),3)
        self.scene.edit('crop',dict(min=[-1,-1,-1],max=[1,1,1]));self.assertEqual(len(self.scene.points),2)
        self.scene.edit('undo',{});self.assertEqual(len(self.scene.points),3)

    def test_streaming_export_archive_and_legacy_import(self):
        self.add([[0,0,0],[3,0,0]])
        saved=self.scene.save('roundtrip');path=self.scene.archive_path(saved['id'])
        with np.load(path/'scene.npz') as arrays:
            self.assertEqual(arrays['points'].shape,(2,3));self.assertEqual(arrays['colors'].shape,(2,3))
        self.assertEqual(len((path/'scene.pcd').read_bytes().split(b'DATA binary\n')[1]),32)
        self.scene.new();self.scene.load(saved['id']);self.assertEqual(len(self.scene.points),2)
        # Earlier disk archives did not yet contain aggregated block caches.
        import sqlite3
        old_db=sqlite3.connect(path/'pages.sqlite');old_db.execute('DROP TABLE block_lod');old_db.commit();old_db.close()
        self.scene.load(saved['id']);self.assertEqual(len(self.scene.points),2)
        legacy=Scene(Path(self.tmp.name)/'legacy',voxel=.001);legacy.capturing=True
        legacy.ingest(np.array([[1,2,3]],np.float32),np.array([[5,6,7]],np.uint8),'odom',1)
        legacy.capturing=False;old=legacy.save('legacy')
        import shutil
        shutil.copytree(legacy.archive_path(old['id']),self.scene.directory/old['id'])
        self.scene.load(old['id']);xyz,rgb=self.scene.snapshot()
        np.testing.assert_array_equal(xyz,[[1,2,3]]);np.testing.assert_array_equal(rgb,[[5,6,7]])

    def test_downsample_only_selected_area_and_canonical_reindex(self):
        self.add([[0,0,0],[.01,0,0],[3,0,0],[3.01,0,0]])
        self.scene.edit('downsample',dict(voxel=.1,min=[-1,-1,-1],max=[1,1,1]))
        self.assertEqual(len(self.scene.points),3)
        self.scene.edit('undo',{});self.assertEqual(len(self.scene.points),4)
        self.scene.set_voxel(.1);self.assertEqual(len(self.scene.points),2)
        self.add([[0,0,0],[3,0,0]],2);self.assertEqual(len(self.scene.points),2)

    def test_large_page_splits_without_in_memory_map(self):
        n=PAGE_POINTS+100
        points=np.column_stack([np.arange(n)%300*.002,np.arange(n)//300*.002,np.zeros(n)]).astype(np.float32)
        self.add(points)
        self.assertEqual(len(self.scene.points),n)
        self.assertGreater(sum(len(b['pages']) for b in self.scene.manifest()['tiles']),1)
        self.assertEqual(len(self.scene.keys),0)
        self.assertTrue(all(t['count']<=PAGE_POINTS for b in self.scene.manifest()['tiles'] for t in b['pages']))
        manifest=self.scene.manifest();block=manifest['tiles'][0]
        raw=self.scene.block_tile(block['id'][2:],3,manifest['generation'],block['version'])
        points=np.frombuffer(raw,RECORD)['xyz']
        self.assertLess(len(points),n//10)
        self.assertEqual(len(np.unique(np.floor(points.astype(np.float64)/.032),axis=0)),len(points))

    def test_live_preview_accepts_newer_versions_without_losing_raw_records(self):
        self.add([[0,0,0]])
        manifest=self.scene.manifest();block=manifest['tiles'][0];page=block['pages'][0]
        self.add([[.01,0,0]],2)
        raw,version=self.scene.tile(page['id'],0,manifest['generation'],page['version'],True)
        self.assertEqual(len(raw),32);self.assertGreater(version,page['version'])
        raw,version=self.scene.block_tile(block['id'][2:],1,manifest['generation'],block['version'],True)
        self.assertEqual(len(raw),32);self.assertGreater(version,block['version'])
