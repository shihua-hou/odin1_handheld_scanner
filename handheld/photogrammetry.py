"""Calibrated Odin1 ROS bag -> real multiview 3DGS training dataset.

FishPoly is evaluated explicitly; it is not OpenCV's four-coefficient fisheye.
Camera poses are interpolated from the driver's odom -> camera_0 TF.
"""
from pathlib import Path
import json
import math
import shutil
import time
import numpy as np
from .core import decode_cloud, write_ply, atomic_json


def stamp(header):
    return header.stamp.sec + header.stamp.nanosec * 1e-9


def pose_matrix(position, quaternion):
    from scipy.spatial.transform import Rotation
    result=np.eye(4)
    result[:3,:3]=Rotation.from_quat(quaternion).as_matrix()
    result[:3,3]=position
    return result


def rectify_maps(calibration, width=960, height=768):
    """Inverse mapping from an undistorted PINHOLE view into actual FishPoly pixels."""
    cam=calibration['cam_0']
    focal=width*.65
    intrinsics=np.array([[focal,0,width/2],[0,focal,height/2],[0,0,1]],float)
    u,v=np.meshgrid(np.arange(width),np.arange(height))
    x=(u-intrinsics[0,2])/focal
    y=(v-intrinsics[1,2])/focal
    radius=np.sqrt(x*x+y*y)
    theta=np.arctan(radius)
    distorted=theta.copy()
    for power in range(2,8):
        distorted += float(cam.get('k'+str(power),0))*theta**power
    factor=np.divide(distorted,radius,out=np.ones_like(radius),where=radius>1e-12)
    dx,dy=x*factor,y*factor
    map_x=cam['A11']*dx+cam.get('A12',0)*dy+cam['u0']
    map_y=cam['A22']*dy+cam['v0']
    return map_x.astype(np.float32),map_y.astype(np.float32),intrinsics


def interpolate_poses(samples, timestamps, max_gap=.3):
    from scipy.spatial.transform import Rotation, Slerp
    ordered={float(t):np.asarray(matrix,float) for t,matrix in samples}
    times=np.array(sorted(ordered))
    if len(times)<2:
        raise ValueError('至少需要两条有效相机位姿；请检查录制中的 TF 或里程计')
    matrices=np.array([ordered[t] for t in times])
    rotations=Slerp(times,Rotation.from_matrix(matrices[:,:3,:3]))
    result=[]
    for t in timestamps:
        j=np.searchsorted(times,t)
        if j==0 or j==len(times) or times[j]-times[j-1]>max_gap:
            result.append(None)
            continue
        alpha=(t-times[j-1])/(times[j]-times[j-1])
        matrix=np.eye(4)
        matrix[:3,:3]=rotations([t]).as_matrix()[0]
        matrix[:3,3]=(1-alpha)*matrices[j-1,:3,3]+alpha*matrices[j,:3,3]
        result.append(matrix)
    return result


def motion_distance(a,b):
    relative=a[:3,:3].T@b[:3,:3]
    angle=math.acos(float(np.clip((np.trace(relative)-1)/2,-1,1)))
    return float(np.linalg.norm(a[:3,3]-b[:3,3])),angle


def select_views(timestamps, poses, maximum=240):
    chosen=[]
    for i,(t,matrix) in enumerate(zip(timestamps,poses)):
        if matrix is None:
            continue
        if not chosen:
            chosen.append(i)
            continue
        last=chosen[-1]
        distance,angle=motion_distance(poses[last],matrix)
        if t-timestamps[last]>=.18 and (distance>=.055 or angle>=math.radians(5)):
            chosen.append(i)
    if len(chosen)>maximum:
        chosen=[chosen[i] for i in np.linspace(0,len(chosen)-1,maximum).astype(int)]
    return chosen


def read_bag(path, topics):
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message
    reader=rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(path),storage_id='sqlite3'),
                rosbag2_py.ConverterOptions('',''))
    types={item.name:get_message(item.type) for item in reader.get_all_topics_and_types()
           if item.name in topics}
    reader.set_filter(rosbag2_py.StorageFilter(topics=list(topics)))
    while reader.has_next():
        topic,serialized,receive_time=reader.read_next()
        if topic in types:
            yield topic,deserialize_message(serialized,types[topic])


def export_dataset(bag, destination, calibration_path, width=960, max_frames=240,
                   seed_points=30000, allow_small_baseline=False, progress=None):
    import cv2
    import yaml
    bag,destination=Path(bag),Path(destination)
    destination.mkdir(parents=True,exist_ok=True)
    (destination/'images').mkdir(exist_ok=True)
    calibration=yaml.safe_load(Path(calibration_path).read_text())
    shutil.copyfile(calibration_path,destination/'sensor_calibration.yaml')
    image_times=[]; camera_samples=[]; odometry=[]; xyz_chunks=[]; rgb_chunks=[]
    cloud_index=0
    topics={'/odin1/image/compressed','/odin1/odometry','/tf','/odin1/cloud_slam'}
    for topic,message in read_bag(bag,topics):
        if topic=='/odin1/image/compressed':
            if 'jpeg' in message.format.lower() or 'jpg' in message.format.lower():
                image_times.append(stamp(message.header))
        elif topic=='/tf':
            for transform in message.transforms:
                if transform.header.frame_id=='odom' and transform.child_frame_id=='camera_0':
                    p,q=transform.transform.translation,transform.transform.rotation
                    camera_samples.append((stamp(transform.header),pose_matrix([p.x,p.y,p.z],[q.x,q.y,q.z,q.w])))
        elif topic=='/odin1/odometry':
            p,q=message.pose.pose.position,message.pose.pose.orientation
            if message.header.frame_id=='odom':
                odometry.append((stamp(message.header),pose_matrix([p.x,p.y,p.z],[q.x,q.y,q.z,q.w])))
        elif topic=='/odin1/cloud_slam' and message.header.frame_id=='odom':
            cloud_index+=1
            if cloud_index%5==0:
                xyz,rgb=decode_cloud(message)
                stride=max(1,int(np.ceil(len(xyz)/500)))
                xyz_chunks.append(xyz[::stride]);rgb_chunks.append(rgb[::stride])
    if progress:progress('同步标定图像和相机位姿',.1)
    if not image_times or not xyz_chunks:
        raise ValueError('录制缺少 JPEG 图像或 SLAM 点云；需要重新采集照片重建数据')
    if len(camera_samples)<2:
        # Manufacturer's fixed body_T_lidar; calibration Tcl maps lidar -> camera.
        body_lidar=np.eye(4);body_lidar[:3,3]=[-.02663,.03447,.02174]
        camera_lidar=np.array(calibration['Tcl_0'],float).reshape(4,4)
        camera_samples=[(t,m@body_lidar@np.linalg.inv(camera_lidar)) for t,m in odometry]
        pose_source='odometry + calibrated extrinsics'
    else:
        pose_source='driver odom->camera_0 TF'
    image_times=sorted(set(image_times))
    poses=interpolate_poses(camera_samples,image_times)
    selected=select_views(image_times,poses,max_frames)
    valid=[m for m in poses if m is not None]
    if not valid:raise ValueError('图像与位姿没有时间重叠，请检查设备时间戳')
    positions=np.array([m[:3,3] for m in valid])
    extent=float(np.linalg.norm(np.ptp(positions,axis=0)))
    quality=dict(images_available=len(image_times),selected_views=len(selected),
                 camera_translation_extent_m=extent,pose_source=pose_source,
                 rejected_unsynchronized=sum(m is None for m in poses),source_bag=str(bag),
                 sensor_camera_model='FishPoly',export_camera_model='PINHOLE',
                 original_data_modified=False)
    atomic_json(destination/'quality.json',quality)
    if len(selected)<8 or extent<.15:
        if not allow_small_baseline:
            raise ValueError(f'多视角覆盖不足：{len(selected)} 个关键帧，移动范围 {extent:.3f} m；需要移动设备重新采集，不能用原地静止画面假装三维重建')
        selected=[i for i,m in enumerate(poses) if m is not None][::max(1,len(valid)//12)][:24]
        quality['limited_baseline_diagnostic_only']=True
    height=int(round(width*.8))
    map_x,map_y,intrinsics=rectify_maps(calibration,width,height)
    selection={round(image_times[i],8):(i,poses[i]) for i in selected}
    records=[];sharpness=[];last_saved=set()
    for topic,message in read_bag(bag,{'/odin1/image/compressed'}):
        key=round(stamp(message.header),8)
        if key not in selection or key in last_saved:continue
        image=cv2.imdecode(np.frombuffer(message.data,np.uint8),cv2.IMREAD_COLOR)
        if image is None:continue
        if image.shape[:2]!=(int(calibration['cam_0']['image_height']),int(calibration['cam_0']['image_width'])):
            raise ValueError('图像尺寸与设备标定不一致，拒绝使用错误相机内参')
        rectified=cv2.remap(image,map_x,map_y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
        index,matrix=selection[key]
        # Nerfstudio/OpenSplat use OpenGL c2w: right, up, backwards.
        matrix_gl=matrix@np.diag([1,-1,-1,1])
        filename=f'images/frame_{len(records):05d}.jpg'
        cv2.imwrite(str(destination/filename),rectified,[cv2.IMWRITE_JPEG_QUALITY,96])
        sharpness.append(float(cv2.Laplacian(cv2.cvtColor(rectified,cv2.COLOR_BGR2GRAY),cv2.CV_64F).var()))
        records.append(dict(file_path=filename,transform_matrix=matrix_gl.tolist(),timestamp=key))
        last_saved.add(key)
        if progress:progress('导出去畸变多视角图像',.1+.6*len(records)/len(selection))
    if len(records)<4:raise ValueError('可解码的同步图像数量不足')
    xyz,rgb=np.concatenate(xyz_chunks),np.concatenate(rgb_chunks)
    _,indices=np.unique(np.floor(xyz/.015).astype(np.int64),axis=0,return_index=True)
    xyz,rgb=xyz[indices],rgb[indices]
    # Keep seeds visible in at least one selected camera. Never invent geometry.
    visible=np.zeros(len(xyz),bool)
    for frame in records:
        c2w=np.asarray(frame['transform_matrix'])@np.diag([1,-1,-1,1])
        camera=(xyz-c2w[:3,3])@c2w[:3,:3]
        z=camera[:,2];u=camera[:,0]/np.maximum(z,1e-8)*intrinsics[0,0]+width/2
        v=camera[:,1]/np.maximum(z,1e-8)*intrinsics[1,1]+height/2
        visible|=(z>.15)&(z<40)&(u>=0)&(u<width)&(v>=0)&(v<height)
    xyz,rgb=xyz[visible],rgb[visible]
    if len(xyz)<100:raise ValueError('相机视野与点云不重叠，拒绝训练；请检查标定与位姿')
    if len(xyz)>seed_points:
        indices=np.random.default_rng(42).choice(len(xyz),seed_points,replace=False)
        xyz,rgb=xyz[indices],rgb[indices]
    write_ply(destination/'points.ply',xyz,rgb)
    transforms=dict(camera_model='PINHOLE',w=width,h=height,fl_x=float(intrinsics[0,0]),
                    fl_y=float(intrinsics[1,1]),cx=width/2,cy=height/2,k1=0,k2=0,p1=0,p2=0,
                    ply_file_path='points.ply',frames=records)
    atomic_json(destination/'transforms.json',transforms)
    quality.update(selected_views=len(records),seed_points=len(xyz),
                   median_sharpness=float(np.median(sharpness)),export_width=width,export_height=height)
    atomic_json(destination/'quality.json',quality)
    if progress:progress('训练数据准备完成',1.)
    return quality


def ply_to_splat(source,destination):
    """Convert genuine learned Gaussian parameters, not XYZ points, to 32-byte splats."""
    with open(source,'rb') as stream:
        fields=[];count=None;in_vertices=False
        while True:
            line=stream.readline().decode('ascii').strip()
            if not line:raise ValueError('Invalid PLY header')
            if line.startswith('format ') and line!='format binary_little_endian 1.0':
                raise ValueError('Expected binary little endian Gaussian PLY')
            if line.startswith('element '):
                words=line.split();in_vertices=words[1]=='vertex'
                if in_vertices:count=int(words[2])
            if line.startswith('property ') and in_vertices:
                _,kind,name=line.split()
                if kind not in ('float','float32'):raise ValueError('Expected float Gaussian properties')
                fields.append((name,'<f4'))
            if line=='end_header':break
        required=['x','y','z','scale_0','scale_1','scale_2','rot_0','rot_1','rot_2','rot_3','opacity','f_dc_0','f_dc_1','f_dc_2']
        if not set(required)<=set(n for n,_ in fields):raise ValueError('This is a point cloud, not a trained Gaussian scene')
        data=np.fromfile(stream,dtype=np.dtype(fields),count=count)
    if len(data)!=count or count==0:raise ValueError('Truncated or empty Gaussian PLY')
    output=np.empty(count,dtype=[('position','<f4',(3,)),('scale','<f4',(3,)),('color','u1',(4,)),('rotation','u1',(4,))])
    output['position']=np.column_stack([data[k] for k in ['x','y','z']])
    output['scale']=np.exp(np.clip(np.column_stack([data['scale_'+str(i)] for i in range(3)]),-20,10))
    rgb=.5+.28209479177387814*np.column_stack([data['f_dc_'+str(i)] for i in range(3)])
    output['color'][:,:3]=np.clip(rgb*255,0,255).astype(np.uint8)
    output['color'][:,3]=np.clip(255/(1+np.exp(-np.clip(data['opacity'],-30,30))),0,255).astype(np.uint8)
    q=np.column_stack([data['rot_'+str(i)] for i in range(4)])
    q/=np.maximum(np.linalg.norm(q,axis=1,keepdims=True),1e-8)
    output['rotation']=np.clip(q*128+128,0,255).astype(np.uint8)
    if not np.isfinite(output['position']).all():raise ValueError('Invalid Gaussian positions')
    output.tofile(destination)
    return count
