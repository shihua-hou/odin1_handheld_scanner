import struct
import unittest
import numpy as np
from handheld.surface import SparseSurface,tetrahedra_surface,SURFACE_MAGIC,organized_xyz
from types import SimpleNamespace as NS


class SurfaceTests(unittest.TestCase):
    def test_official_driver_flattened_sensor_layout(self):
        records=np.zeros(512,dtype=np.dtype({'names':['x','y','z','confidence','offset_time'],
            'formats':['<f4','<f4','<f4','<u2','<f4'],'offsets':[0,4,8,13,15],'itemsize':19}))
        records['x']=2;records['confidence']=100;records['confidence'][0]=0
        fields=[NS(name=name,offset=records.dtype.fields[name][1]) for name in records.dtype.names]
        msg=NS(height=1,width=512,point_step=19,row_step=512*19,is_bigendian=False,fields=fields,data=records.tobytes())
        xyz,valid=organized_xyz(msg)
        self.assertEqual(xyz.shape,(2,256,3));self.assertFalse(valid[0,0]);self.assertTrue(valid[1,0])

    def test_observed_plane_produces_real_triangles(self):
        x,y,z=np.meshgrid(np.arange(5),np.arange(5),np.arange(5),indexing='ij')
        values=(2.3-z).astype(np.float32)
        colors=np.ones((5,5,5,3),np.float32)*.5
        vertices,rgb=tetrahedra_surface(values,np.ones_like(values),colors,[0,0,0],.1)
        self.assertGreater(len(vertices),0)
        self.assertEqual(len(vertices)%3,0)
        np.testing.assert_allclose(vertices[:,2],.23,atol=1e-6)
        self.assertEqual(rgb.dtype,np.uint8)
        empty,_=tetrahedra_surface(values,np.zeros_like(values),colors,[0,0,0],.1)
        self.assertEqual(len(empty),0)

    def test_fusion_is_bounded_and_streams_changed_meshes(self):
        volume=SparseSurface(voxel=.04,block_size=8,max_blocks=100)
        x,y=np.meshgrid(np.linspace(-.2,.2,25),np.linspace(-.2,.2,25))
        points=np.column_stack([x.ravel(),y.ravel(),np.ones(x.size)])
        colors=np.full((len(points),3),150,np.uint8)
        volume.integrate(points,colors,np.array([0.,0.,0.]))
        state=volume.status();self.assertGreater(state['triangles'],0)
        self.assertLessEqual(state['blocks'],100)
        packets=volume.packets({});self.assertTrue(packets)
        versions={key:version for key,version,packet in packets}
        self.assertEqual(volume.packets(versions),[])
        for _,_,packet in packets:
            magic,generation,version,name,count=struct.unpack('<5I',packet[:20])
            self.assertEqual(magic,SURFACE_MAGIC)
            self.assertEqual(len(packet),20+name+count*16)
        volume.clear();self.assertEqual(volume.status()['triangles'],0)
        self.assertEqual(volume.generation,1)
