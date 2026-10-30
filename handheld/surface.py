"""CPU sparse coloured TSDF fusion and incremental real triangle surfaces.

Unknown voxels never generate triangles. A surface is reconstructed only from
observed depth measurements; no offline Gaussian training is involved.
"""
import itertools
import struct
import threading
import time
import numpy as np

SURFACE_MAGIC=0x53555246
TETS=np.array([[0,5,1,6],[0,1,2,6],[0,2,3,6],[0,3,7,6],[0,7,4,6],[0,4,5,6]])
CORNERS=np.array([[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]])


def tetrahedra_surface(values,weights,colors,base,voxel):
    """Extract measured zero crossings, including neighbouring block halo."""
    size=values.shape[0]-1
    slices=tuple(tuple(slice(c,c+size) for c in corner) for corner in CORNERS)
    signed=np.stack([values[s] for s in slices],axis=-1)
    observed=np.stack([weights[s]>0 for s in slices],axis=-1)
    valid=observed.all(axis=-1)&(signed.min(axis=-1)<0)&(signed.max(axis=-1)>=0)
    cells=np.argwhere(valid)
    if not len(cells):return np.empty((0,3),np.float32),np.empty((0,3),np.uint8)
    nodes=cells[:,None,:]+CORNERS[None,:,:]
    coords=(nodes+np.asarray(base))*voxel
    s=values[nodes[:,:,0],nodes[:,:,1],nodes[:,:,2]]
    c=colors[nodes[:,:,0],nodes[:,:,1],nodes[:,:,2]]
    triangles=[];triangle_colors=[]
    # Batch all six tetrahedra so case classification runs fourteen times,
    # rather than repeating the same Python dispatch for every tetrahedron.
    for tet in [TETS]:
        sv=s[:,tet].reshape(-1,4);pv=coords[:,tet].reshape(-1,4,3);cv=c[:,tet].reshape(-1,4,3)
        codes=(sv<0).astype(np.uint8)@np.array([1,2,4,8],np.uint8)
        for code in range(1,15):
            selection=codes==code
            if not selection.any():continue
            inside=[i for i in range(4) if code&(1<<i)]
            outside=[i for i in range(4) if not code&(1<<i)]
            if len(inside)==1:edges=[(inside[0],o) for o in outside];faces=[(0,1,2)]
            elif len(outside)==1:edges=[(outside[0],i) for i in inside];faces=[(0,2,1)]
            else:
                edges=[(inside[0],outside[0]),(inside[0],outside[1]),(inside[1],outside[0]),(inside[1],outside[1])]
                faces=[(0,1,2),(1,3,2)]
            points=[];rgb=[]
            for a,b in edges:
                alpha=np.clip(sv[selection,a]/(sv[selection,a]-sv[selection,b]),0,1)[:,None]
                points.append(pv[selection,a]*(1-alpha)+pv[selection,b]*alpha)
                rgb.append(cv[selection,a]*(1-alpha)+cv[selection,b]*alpha)
            for face in faces:
                triangles.append(np.stack([points[i] for i in face],axis=1).reshape(-1,3))
                triangle_colors.append(np.stack([rgb[i] for i in face],axis=1).reshape(-1,3))
    if not triangles:return np.empty((0,3),np.float32),np.empty((0,3),np.uint8)
    return np.concatenate(triangles).astype(np.float32),np.clip(np.concatenate(triangle_colors)*255,0,255).astype(np.uint8)


class SparseSurface:
    def __init__(self,voxel=.02,block_size=16,max_blocks=2500):
        self.voxel=voxel;self.size=block_size;self.max_blocks=max_blocks
        self.truncation=voxel*3
        self.blocks={};self.meshes={};self.versions={};self.revision=0;self.generation=0
        self.lock=threading.RLock();self.frames=0;self.last_update=0.;self.duration=0.;self.error=''

    def clear(self):
        with self.lock:
            self.blocks.clear();self.meshes.clear();self.versions.clear();self.revision+=1;self.generation+=1
            self.frames=0;self.last_update=0.;self.error=''

    def integrate(self,xyz,rgb,origin):
        start=time.monotonic()
        xyz=np.asarray(xyz,np.float32);rgb=np.asarray(rgb,np.float32)/255.
        rays=xyz-np.asarray(origin,np.float32)
        ranges=np.linalg.norm(rays,axis=1)
        good=np.isfinite(xyz).all(axis=1)&(ranges>.2)&(ranges<12)
        rays,ranges,rgb=rays[good],ranges[good],rgb[good]
        if not len(ranges):return
        directions=rays/ranges[:,None]
        offsets=np.arange(-3,4,dtype=np.float32)*self.voxel
        centers=np.asarray(origin)+directions[:,None,:]*(ranges[:,None]+offsets[None,:])[:,:,None]
        lower=np.floor(centers/self.voxel).astype(np.int64)
        # Trilinear neighbourhood coverage prevents cracks between adjacent depth rays.
        neighbors=np.array(list(itertools.product([0,1],repeat=3)),np.int64)
        nodes=(lower[:,:,None,:]+neighbors[None,None,:,:]).reshape(-1,3)
        ray_index=np.repeat(np.arange(len(ranges)),len(offsets)*8)
        signed=(ranges[ray_index]-np.sum((nodes*self.voxel-origin)*directions[ray_index],axis=1))/self.truncation
        signed=np.clip(signed,-1,1)
        offset=1<<20
        bounded=(np.abs(nodes)<offset).all(axis=1)
        nodes,signed,ray_index=nodes[bounded],signed[bounded],ray_index[bounded]
        packed=((nodes[:,0]+offset)<<42)|((nodes[:,1]+offset)<<21)|(nodes[:,2]+offset)
        unique,inverse,count=np.unique(packed,return_inverse=True,return_counts=True)
        averages=np.bincount(inverse,weights=signed)/count
        averaged_rgb=np.column_stack([np.bincount(inverse,weights=rgb[ray_index,k])/count for k in range(3)])
        nodes=np.column_stack([(unique>>42)-offset,((unique>>21)&((1<<21)-1))-offset,(unique&((1<<21)-1))-offset])
        keys=nodes//self.size;local=nodes%self.size
        unique_blocks,groups=np.unique(keys,axis=0,return_inverse=True)
        order=np.argsort(groups,kind='stable')
        boundaries=np.searchsorted(groups[order],np.arange(len(unique_blocks)+1))
        dirty=set()
        with self.lock:
            for group,key_array in enumerate(unique_blocks):
                key=tuple(key_array)
                if key not in self.blocks:
                    if len(self.blocks)>=self.max_blocks:
                        self.error='表面体积已达到内存上限，请保存后新建场景'
                        continue
                    shape=(self.size,)*3
                    self.blocks[key]=(np.ones(shape,np.float32),np.zeros(shape,np.float32),np.zeros(shape+(3,),np.float32))
                values,weights,colors=self.blocks[key]
                selected=order[boundaries[group]:boundaries[group+1]];coords=local[selected];at=tuple(coords.T)
                old=weights[at];new=np.minimum(count[selected],4)
                total=old+new
                values[at]=(values[at]*old+averages[selected]*new)/total
                colors[at]=(colors[at]*old[:,None]+averaged_rgb[selected]*new[:,None])/total[:,None]
                weights[at]=np.minimum(total,32)
                dirty.update(tuple(key_array-np.asarray(neighbor)) for neighbor in itertools.product([0,1],repeat=3))
            generation=self.generation
        for key in dirty:
            with self.lock:
                if generation!=self.generation:return
                if key not in self.blocks:continue
                values,weights,colors=self.halo(key)
            vertices,rgb_vertices=tetrahedra_surface(values,weights,colors,np.array(key)*self.size,self.voxel)
            with self.lock:
                if generation!=self.generation:return
                self.revision+=1
                self.meshes[key]=(vertices,rgb_vertices)
                self.versions[key]=self.revision
        with self.lock:
            self.frames+=1;self.last_update=time.time();self.duration=time.monotonic()-start

    def halo(self,key):
        shape=(self.size+1,)*3
        values=np.ones(shape,np.float32);weights=np.zeros(shape,np.float32);colors=np.zeros(shape+(3,),np.float32)
        for neighbor in itertools.product([0,1],repeat=3):
            other=tuple(np.array(key)+neighbor)
            if other not in self.blocks:continue
            source=self.blocks[other]
            dest=tuple(slice(0,self.size) if n==0 else slice(self.size,self.size+1) for n in neighbor)
            src=tuple(slice(None) if n==0 else slice(0,1) for n in neighbor)
            values[dest],weights[dest],colors[dest]=source[0][src],source[1][src],source[2][src]
        return values,weights,colors

    def status(self):
        with self.lock:
            return dict(generation=self.generation,revision=self.revision,frames=self.frames,blocks=len(self.blocks),
                        triangles=sum(len(v[0])//3 for v in self.meshes.values()),voxel=self.voxel,
                        last_update=self.last_update,integration_seconds=self.duration,error=self.error)

    def packets(self,versions):
        with self.lock:
            result=[]
            for key,version in self.versions.items():
                if versions.get(key)==version:continue
                vertices,rgb=self.meshes[key]
                data=np.empty(len(vertices),dtype=[('xyz','<f4',(3,)),('rgb','u1',(4,))])
                data['xyz']=vertices;data['rgb'][:,:3]=rgb;data['rgb'][:,3]=255
                name=','.join(map(str,key)).encode();name+=b' '*((-len(name))%4)
                header=struct.pack('<5I',SURFACE_MAGIC,self.generation,version,len(name),len(vertices))
                result.append((key,version,header+name+data.tobytes()))
            return result

    def save(self,path):
        from .core import write_ply
        with self.lock:
            nonempty=[mesh for mesh in self.meshes.values() if len(mesh[0])]
            if not nonempty:return 0
            points=np.concatenate([mesh[0] for mesh in nonempty])
            colors=np.concatenate([mesh[1] for mesh in nonempty])
        # Actual connected triangle topology, not a point-only PLY.
        vertex_data=np.empty(len(points),dtype=[('xyz','<f4',(3,)),('rgb','u1',(3,))])
        vertex_data['xyz']=points;vertex_data['rgb']=colors
        count=len(points)//3
        faces=np.empty(count,dtype=[('n','u1'),('indices','<i4',(3,))]);faces['n']=3
        faces['indices']=np.arange(len(points)).reshape(-1,3)
        header=('ply\nformat binary_little_endian 1.0\n'+f'element vertex {len(points)}\n'+
                'property float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\n'+
                f'element face {count}\nproperty list uchar int vertex_indices\nend_header\n')
        with open(path,'wb') as stream:stream.write(header.encode());stream.write(vertex_data.tobytes());stream.write(faces.tobytes())
        return count


def organized_xyz(message):
    """Keep the sensor's pixel topology and invalid depth mask."""
    fields={f.name:f for f in message.fields};endian='>' if message.is_bigendian else '<'
    xyz=np.stack([np.ndarray((message.height,message.width),dtype=endian+'f4',buffer=message.data,
        offset=fields[k].offset,strides=(message.row_step,message.point_step)) for k in ['x','y','z']],axis=-1).copy()
    valid=np.isfinite(xyz).all(axis=2)&(np.linalg.norm(xyz,axis=2)>.2)
    if 'confidence' in fields:
        confidence=np.ndarray((message.height,message.width),dtype=endian+'u2',buffer=message.data,
            offset=fields['confidence'].offset,strides=(message.row_step,message.point_step))
        valid &= confidence>=35
    # PointCloud2Modifier::resize in the official driver flattens the header
    # while preserving the documented 256-column ToF sensor pixel order.
    if message.height==1 and 'offset_time' in fields and message.width%256==0 and 512<=message.width<=256*192:
        xyz=xyz.reshape(-1,256,3);valid=valid.reshape(-1,256)
    return xyz,valid


def sample_rgb(xyz,image,calibration):
    """Calibrated FishPoly colour projection in the ROS LiDAR coordinate system."""
    import cv2
    matrix=np.array(calibration['Tcl_0'],float).reshape(4,4)
    points=xyz@matrix[:3,:3].T+matrix[:3,3]
    x,y,z=points[:,0],points[:,1],points[:,2]
    radius=np.sqrt(x*x+y*y);theta=np.arctan2(radius,z)
    distorted=theta.copy();cam=calibration['cam_0']
    for power in range(2,8):distorted+=cam.get('k'+str(power),0)*theta**power
    factor=np.divide(distorted,radius,out=np.zeros_like(radius),where=radius>1e-10)
    u=cam['A11']*x*factor+cam.get('A12',0)*y*factor+cam['u0']
    v=cam['A22']*y*factor+cam['v0']
    valid=(z>0)&(u>=0)&(v>=0)&(u<image.shape[1]-1)&(v<image.shape[0]-1)
    colors=cv2.remap(image,u.astype(np.float32).reshape(-1,1),v.astype(np.float32).reshape(-1,1),cv2.INTER_LINEAR).reshape(-1,3)[:,::-1]
    return colors,valid


class LiveSurface:
    def __init__(self,root):
        self.root=root;self.volume=SparseSurface();self.active=False
        self.condition=threading.Condition();self.latest=None;self.images=[];self.poses=[];self.extrinsic=np.eye(4)
        self.extrinsic[:3,3]=[-.02663,.03447,.02174]
        self.stopping=False;self.error='';self.dropped=0;self.calibration=None
        self.worker=threading.Thread(target=self.run,daemon=True,name='surface-fusion');self.worker.start()

    def add_image(self,message):
        from .photogrammetry import stamp
        with self.condition:
            self.images.append((stamp(message.header),bytes(message.data)));self.images=self.images[-8:]

    def add_pose(self,message):
        from .photogrammetry import stamp,pose_matrix
        p,q=message.pose.pose.position,message.pose.pose.orientation
        with self.condition:
            self.poses.append((stamp(message.header),pose_matrix([p.x,p.y,p.z],[q.x,q.y,q.z,q.w])))
            self.poses=self.poses[-100:]

    def add_raw(self,message):
        if not self.active:return
        with self.condition:
            if self.latest is not None:self.dropped+=1
            self.latest=message;self.condition.notify()

    def run(self):
        from .photogrammetry import stamp,interpolate_poses
        import cv2,yaml
        while not self.stopping:
            with self.condition:
                if self.latest is None:self.condition.wait(timeout=.2)
                if self.latest is None:continue
                message=self.latest;self.latest=None
                images=self.images.copy();poses=self.poses.copy()
            if not self.active or not images or len(poses)<2:continue
            started=time.monotonic()
            try:
                if self.calibration is None:
                    self.calibration=yaml.safe_load((self.root/'runtime/calibration/calib.yaml').read_text())
                timestamp=stamp(message.header)
                if poses[-1][0]<timestamp:
                    time.sleep(.1)
                    with self.condition:poses=self.poses.copy();images=self.images.copy()
                image_time,jpeg=min(images,key=lambda pair:abs(pair[0]-timestamp))
                if abs(image_time-timestamp)>.16:continue
                pose=interpolate_poses(poses,[timestamp],max_gap=.35)[0]
                if pose is None:continue
                xyz,valid=organized_xyz(message)
                if xyz.shape[0]<2:raise ValueError('原始深度缺少二维网格，无法实时重建表面')
                # Subsample the actual sensor grid for bounded CPU integration.
                xyz,valid=xyz[::2,::2],valid[::2,::2]
                points=xyz[valid]
                image=cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
                if image is None:continue
                colors,in_image=sample_rgb(points,image,self.calibration)
                points,colors=points[in_image],colors[in_image]
                transform=pose@self.extrinsic
                world=points@transform[:3,:3].T+transform[:3,3]
                self.volume.integrate(world,colors,transform[:3,3]);self.error=''
            except Exception as error:self.error=str(error)
            # Process the newest frame, not a growing queue of stale frames.
            delay=.5-(time.monotonic()-started)
            if delay>0:time.sleep(delay)

    def stop(self):
        self.stopping=True
        with self.condition:self.condition.notify()
        self.worker.join(timeout=5)
