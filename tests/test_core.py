import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
import numpy as np
from handheld.core import Scene, decode_cloud


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scene = Scene(self.tmp.name, voxel=.1, limit=100)

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self):
        self.scene.capturing = True
        self.scene.ingest(np.array([[0,0,0],[.01,0,0],[1,2,3],[np.nan,0,0]],np.float32),
                          np.array([[255,0,0],[0,255,0],[0,0,255],[0,0,0]],np.uint8), 'odom', 1.)

    def test_voxel_dedup_and_invalid_points(self):
        self.ingest()
        self.assertEqual(len(self.scene.points), 2)
        self.assertEqual(self.scene.frames, 1)
        self.assertEqual(len(self.scene.preview()), 12+2*24)

    def test_timestamp_reset_pauses(self):
        self.ingest()
        self.scene.ingest(np.zeros((1,3),np.float32),np.zeros((1,3),np.uint8),'odom',.5)
        self.assertFalse(self.scene.capturing)
        self.assertTrue(self.scene.loaded)
        self.assertIn('timestamp',self.scene.error)

    def test_wrong_frame_never_accumulates(self):
        self.scene.capturing = True
        self.scene.ingest(np.zeros((1,3),np.float32),np.zeros((1,3),np.uint8),'lidar')
        self.assertEqual(len(self.scene.points),0)

    def test_pause_required_and_undo(self):
        self.ingest()
        with self.assertRaises(ValueError):
            self.scene.edit('delete_sphere',dict(center=[0,0,0],radius=.2))
        self.scene.capturing=False
        self.scene.edit('delete_sphere',dict(center=[0,0,0],radius=.2))
        self.assertEqual(len(self.scene.points),1)
        self.scene.edit('undo',{})
        self.assertEqual(len(self.scene.points),2)
        with self.assertRaises(ValueError):
            self.scene.edit('crop',dict(min=[5,0,0],max=[1,1,1]))

    def test_save_load_exports_and_new_autosave(self):
        self.ingest()
        self.scene.capturing=False
        result=self.scene.save('../测试')
        directory=self.scene.archive_path(result['id'])
        original=self.scene.points.copy()
        self.assertTrue((directory/'scene.ply').read_bytes().startswith(b'ply\nformat binary_little_endian'))
        pcd=(directory/'scene.pcd').read_bytes()
        self.assertEqual(len(pcd.split(b'DATA binary\n')[1]),32)
        self.scene.new()
        self.assertEqual(len(self.scene.points),0)
        self.scene.load(result['id'])
        np.testing.assert_array_equal(original,self.scene.points)
        self.assertEqual(len(self.scene.archives()),2)
        for bad in ['../x','..','x/y','x\\y']:
            with self.assertRaises(ValueError):
                self.scene.archive_path(bad)

    def test_limit_stops_instead_of_growing(self):
        self.scene.limit=1
        self.ingest()
        self.assertEqual(len(self.scene.points),1)
        self.assertTrue(self.scene.full)
        self.assertFalse(self.scene.capturing)

    def test_default_has_no_point_count_cap(self):
        scene=Scene(self.tmp.name,voxel=.001)
        scene.capturing=True
        for frame in range(3):
            points=np.column_stack([np.arange(150)*.002,np.full(150,frame),np.ones(150)]).astype(np.float32)
            scene.ingest(points,np.full((150,3),100,np.uint8),'odom',frame+1)
        self.assertEqual(len(scene.points),450)
        self.assertIsNone(scene.status()['limit'])
        self.assertFalse(scene.full)
        self.assertTrue(scene.capturing)

    def test_row_padding_and_endianness(self):
        for endian in ['<','>']:
            data=b''.join(struct.pack(endian+'fffI',i,2.,3.,0xff2040)+b'padding!' for i in [1.,4.])
            msg=NS(width=1,height=2,row_step=24,point_step=16,is_bigendian=endian=='>',data=data,
                   fields=[NS(name=n,offset=i*4,datatype=7) for i,n in enumerate(['x','y','z','rgb'])])
            xyz,rgb=decode_cloud(msg)
            np.testing.assert_allclose(xyz[:,0],[1,4])
            np.testing.assert_array_equal(rgb,[[255,32,64],[255,32,64]])

    def test_voxel_limits_and_finer_capture(self):
        for bad in [0, .0009, 1.01, float('nan'), float('inf')]:
            with self.assertRaises(ValueError): self.scene.set_voxel(bad)
        self.ingest()
        with self.assertRaises(ValueError): self.scene.set_voxel(.001)
        self.scene.capturing=False
        self.scene.set_voxel(.001)
        self.assertEqual(len(self.scene.points),2)
        self.scene.capturing=True
        self.scene.ingest(np.array([[.01,0,0]],np.float32),np.array([[0,255,0]],np.uint8),'odom',2.)
        self.assertEqual(len(self.scene.points),3)

    def test_coarser_voxel_and_archive_restore(self):
        self.scene.set_voxel(.001)
        self.scene.capturing=True
        self.scene.ingest(np.array([[0,0,0],[.02,0,0]],np.float32),np.zeros((2,3),np.uint8),'odom',1.)
        self.scene.capturing=False
        saved=self.scene.save('fine')
        self.scene.set_voxel(1.)
        self.assertEqual(len(self.scene.points),1)
        self.assertTrue(self.scene.archives())
        self.scene.load(saved['id'])
        self.assertEqual(self.scene.voxel,.001)
        self.assertEqual(len(self.scene.points),2)


if __name__=='__main__':
    unittest.main()
