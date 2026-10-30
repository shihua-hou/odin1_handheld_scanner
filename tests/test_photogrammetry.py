import tempfile
import unittest
from pathlib import Path
import numpy as np
from handheld.photogrammetry import rectify_maps,interpolate_poses,select_views,ply_to_splat
from handheld.core import write_ply


class ReconstructionTests(unittest.TestCase):
    def test_fishpoly_rectification_center_and_scale(self):
        calib={'cam_0':{'A11':700.,'A22':710.,'A12':.1,'u0':800.,'v0':650.,'k2':.01}}
        mx,my,k=rectify_maps(calib,100,80)
        self.assertAlmostEqual(float(mx[40,50]),800.)
        self.assertAlmostEqual(float(my[40,50]),650.)
        self.assertEqual(k[0,0],65)
        self.assertGreater(mx[40,60],800)
        self.assertLess(my[20,50],650)

    def test_pose_interpolation_and_gap_rejection(self):
        a=np.eye(4);b=np.eye(4);b[0,3]=1
        result=interpolate_poses([(0,a),(1,b)],[.5,2.],max_gap=2)
        self.assertAlmostEqual(result[0][0,3],.5)
        self.assertIsNone(result[1])
        self.assertIsNone(interpolate_poses([(0,a),(1,b)],[.5])[0])

    def test_static_views_are_not_fabricated(self):
        poses=[np.eye(4) for _ in range(20)]
        self.assertEqual(select_views(np.arange(20),poses),[0])
        for i,p in enumerate(poses):p[0,3]=i*.1
        self.assertEqual(len(select_views(np.arange(20),poses,maximum=8)),8)

    def test_trained_gaussian_conversion_and_reject_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            write_ply(path/'points.ply',np.array([[1,2,3]],np.float32),np.array([[255,0,0]],np.uint8))
            with self.assertRaises(ValueError):ply_to_splat(path/'points.ply',path/'fake.splat')
            fields=['x','y','z','scale_0','scale_1','scale_2','rot_0','rot_1','rot_2','rot_3','opacity','f_dc_0','f_dc_1','f_dc_2']
            header='ply\nformat binary_little_endian 1.0\nelement vertex 1\n'+''.join('property float '+n+'\n' for n in fields)+'end_header\n'
            values=np.zeros(1,dtype=[(name,'<f4') for name in fields]);values['x']=1;values['rot_0']=1
            (path/'trained.ply').write_bytes(header.encode()+values.tobytes())
            self.assertEqual(ply_to_splat(path/'trained.ply',path/'model.splat'),1)
            self.assertEqual((path/'model.splat').stat().st_size,32)
            self.assertEqual(np.frombuffer((path/'model.splat').read_bytes()[:12],'<f4')[0],1)


if __name__=='__main__':unittest.main()
