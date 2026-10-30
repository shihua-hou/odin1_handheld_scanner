"""Bounded voxel mapping and lossless scene revisions; coordinates are metres."""
from __future__ import annotations
import json
import os
import threading
import tempfile
import time
import uuid
from pathlib import Path
import numpy as np

MAX_POINTS = None  # No application point-count cap.
MIN_VOXEL = 0.001
MAX_VOXEL = 1.0
DEFAULT_VOXEL = 0.005


def decode_cloud(msg):
    """Respect PointCloud2 byte order, offsets, field types and row padding."""
    endian = '>' if msg.is_bigendian else '<'
    kinds = {1: 'i1', 2: 'u1', 3: 'i2', 4: 'u2', 5: 'i4', 6: 'u4', 7: 'f4', 8: 'f8'}
    fields = {f.name: f for f in msg.fields}
    if not all(k in fields for k in ('x', 'y', 'z')):
        raise ValueError('PointCloud2 missing XYZ fields')
    def read(name, override=None):
        f = fields[name]
        dtype = np.dtype(endian + (override or kinds[f.datatype]))
        return np.ndarray((msg.height, msg.width), dtype=dtype, buffer=msg.data,
                          offset=f.offset, strides=(msg.row_step, msg.point_step)).reshape(-1)
    xyz = np.column_stack([read(k) for k in ('x', 'y', 'z')]).astype(np.float32)
    color = np.full((len(xyz), 3), 180, np.uint8)
    rgb = 'rgb' if 'rgb' in fields else 'rgba' if 'rgba' in fields else None
    if rgb:
        packed = read(rgb, 'u4').astype(np.uint32)
        color = np.column_stack([(packed >> 16) & 255, (packed >> 8) & 255, packed & 255]).astype(np.uint8)
    good = np.isfinite(xyz).all(axis=1) & (np.abs(xyz) < 10000).all(axis=1)
    return xyz[good], color[good]


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf8', dir=path.parent,
                                         prefix=f'.{path.name}.', suffix='.tmp',
                                         delete=False) as stream:
            temp = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        if temp is not None:
            try: temp.unlink()
            except FileNotFoundError: pass


class Scene:
    def __init__(self, directory, voxel=DEFAULT_VOXEL, limit=MAX_POINTS):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.voxel, self.limit = voxel, limit
        self.points = np.empty((0, 3), np.float32)
        self.colors = np.empty((0, 3), np.uint8)
        self.keys = set()
        self.history = []
        self.session = uuid.uuid4().hex[:12]
        self.revision = 0
        self.topology = 0
        self.capturing = False
        self.frames = 0
        self.received_frames = 0
        self.last_cloud = 0.
        self.last_stamp = None
        self.frame = 'odom'
        self.error = ''
        self.full = False
        self.loaded = False
        self.trajectory = []
        self.last_pose = 0.
        self.pose = None

    def ingest(self, xyz, colors, frame, stamp=None):
        with self.lock:
            self.received_frames += 1
            self.last_cloud = time.time()
            if not self.capturing:
                return
            if frame != 'odom':
                self.error = f'Rejected frame {frame!r}: expected official cloud_slam in odom'
                return
            if stamp is not None and self.last_stamp is not None and stamp <= self.last_stamp:
                self.capturing = False
                self.loaded = True
                self.error = 'Device timestamp reset/out of order. Save scene and start a new session.'
                return
            self.last_stamp = stamp
            self.frame = frame
            good = np.isfinite(xyz).all(axis=1) & (np.abs(xyz) < 10000).all(axis=1)
            xyz, colors = xyz[good], colors[good]
            cells = np.floor(xyz / self.voxel).astype(np.int64)
            _, idx = np.unique(cells, axis=0, return_index=True)
            selected = []
            remaining = self.limit - len(self.points) if self.limit is not None else len(idx)
            for i in idx:
                key = tuple(cells[i])
                if key not in self.keys and len(selected) < remaining:
                    self.keys.add(key)
                    selected.append(i)
            if selected:
                self.points = np.concatenate((self.points, xyz[selected]))
                self.colors = np.concatenate((self.colors, colors[selected]))
                self.revision += 1
            self.frames += 1
            if self.limit is not None and len(self.points) >= self.limit:
                self.full = True
                self.capturing = False
                self.error = 'Point limit reached; save this scene and start a new session.'

    def status(self):
        with self.lock:
            age = time.time() - self.last_cloud if self.last_cloud else None
            return dict(session=self.session, revision=self.revision, points=len(self.points),
                        frames=self.frames, received_frames=self.received_frames, capturing=self.capturing,
                        connected=age is not None and age < 3, cloud_age=age, frame=self.frame,
                        voxel=self.voxel, voxel_min=MIN_VOXEL, voxel_max=MAX_VOXEL,
                        limit=self.limit, full=self.full, error=self.error,
                        pose=self.pose, trajectory=self.trajectory[-3000:], undo=len(self.history))

    def snapshot(self):
        with self.lock:
            return self.points.copy(), self.colors.copy()

    def preview(self, maximum=150000):
        with self.lock:
            step = max(1, int(np.ceil(len(self.points) / maximum)))
            xyz, rgb = self.points[::step], self.colors[::step]
            # Header uint32 magic/revision/count then interleaved float32 XYZRGB (0..1).
            data = np.empty((len(xyz), 6), '<f4')
            data[:, :3], data[:, 3:] = xyz, rgb / 255.
            return np.array([0x4F44494E, self.revision, len(xyz)], '<u4').tobytes() + data.tobytes()

    def point_packet(self,offset,maximum=50000):
        with self.lock:
            count=min(maximum,max(0,len(self.points)-offset))
            data=np.empty(count,dtype=[('xyz','<f4',(3,)),('rgb','u1',(4,))])
            data['xyz']=self.points[offset:offset+count]
            data['rgb'][:,:3]=self.colors[offset:offset+count];data['rgb'][:,3]=255
            header=np.array([0x4F444E32,self.topology,self.revision,offset,count,len(self.points)],'<u4')
            return header.tobytes()+data.tobytes()

    def reindex(self):
        self.keys = {tuple(k) for k in np.floor(self.points / self.voxel).astype(np.int64)}

    def set_voxel(self, value):
        value = float(value)
        if not np.isfinite(value) or not MIN_VOXEL <= value <= MAX_VOXEL:
            raise ValueError('建图体素范围为 1 mm 到 1 m')
        with self.lock:
            if self.capturing:
                raise ValueError('请先暂停采集，再修改建图体素')
            if value == self.voxel:
                return
            if len(self.points):
                self.save('autosave-before-voxel-change')
                if value > self.voxel:
                    _, indices = np.unique(np.floor(self.points / value).astype(np.int64),
                                           axis=0, return_index=True)
                    self.points, self.colors = self.points[indices].copy(), self.colors[indices].copy()
            self.voxel = value
            self.reindex()
            self.history.clear()
            self.full = self.limit is not None and len(self.points) >= self.limit
            if not self.full and self.error.startswith('Point limit reached'):
                self.error = ''
            self.revision += 1
            self.topology += 1

    def edit(self, action, data):
        with self.lock:
            if self.capturing:
                raise ValueError('Pause capture before editing')
            if not len(self.points):
                raise ValueError('Scene is empty')
            if action == 'undo':
                if not self.history:
                    raise ValueError('No undo history')
                self.points, self.colors = self.history.pop()
            else:
                if action in ('crop', 'delete_box'):
                    low, high = np.asarray(data['min'], float), np.asarray(data['max'], float)
                    if low.shape != (3,) or high.shape != (3,) or not np.isfinite([low, high]).all() or (low > high).any():
                        raise ValueError('Invalid XYZ bounds')
                    inside = ((self.points >= low) & (self.points <= high)).all(axis=1)
                    keep = inside if action == 'crop' else ~inside
                elif action == 'delete_sphere':
                    center, radius = np.asarray(data['center'], float), float(data['radius'])
                    if center.shape != (3,) or not np.isfinite(center).all() or not 0 < radius <= 100:
                        raise ValueError('Invalid sphere')
                    keep = np.linalg.norm(self.points - center, axis=1) > radius
                elif action == 'downsample':
                    size = float(data['voxel'])
                    if not 0.001 <= size <= 2:
                        raise ValueError('Voxel size must be 0.001..2 metres')
                    _, keep = np.unique(np.floor(self.points / size).astype(np.int64), axis=0, return_index=True)
                else:
                    raise ValueError('Unknown edit operation')
                self.history.append((self.points, self.colors))
                self.history = self.history[-3:]
                self.points, self.colors = self.points[keep].copy(), self.colors[keep].copy()
            self.reindex()
            self.revision += 1
            self.topology += 1

    def save(self, name='scan'):
        with self.lock:
            if not len(self.points):
                raise ValueError('Cannot save empty scene')
            safe = ''.join(c for c in str(name) if c.isalnum() or c in '-_')[:48] or 'scan'
            scene_id = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6] + '-' + safe
            dest = self.directory / scene_id
            dest.mkdir()
            xyz, rgb = self.points.copy(), self.colors.copy()
            meta = self.status()
            meta.update(id=scene_id, name=str(name)[:100], saved_at=time.time(), trajectory=self.trajectory.copy())
        # Write to temporary filenames; metadata marks a complete archive.
        np.savez_compressed(dest / 'scene.npz', points=xyz, colors=rgb)
        write_ply(dest / 'scene.ply', xyz, rgb)
        write_pcd(dest / 'scene.pcd', xyz, rgb)
        atomic_json(dest / 'metadata.json', meta)
        return meta

    def archives(self):
        result = []
        for path in sorted(self.directory.glob('*/metadata.json'), reverse=True):
            try:
                result.append(json.loads(path.read_text(encoding='utf8')))
            except (OSError, ValueError):
                continue
        return result

    def archive_path(self, scene_id):
        if not scene_id or Path(scene_id).name != scene_id or '/' in scene_id or '\\' in scene_id:
            raise ValueError('Invalid scene ID')
        path = (self.directory / scene_id).resolve()
        if path.parent != self.directory.resolve() or not (path / 'metadata.json').is_file():
            raise ValueError('Scene not found')
        return path

    def load(self, scene_id):
        with self.lock:
            if self.capturing:
                raise ValueError('Pause capture before loading')
            path = self.archive_path(scene_id)
            if len(self.points):
                self.save('autosave-before-load')
            with np.load(path / 'scene.npz', allow_pickle=False) as saved:
                self.points, self.colors = saved['points'].copy(), saved['colors'].copy()
            meta = json.loads((path / 'metadata.json').read_text(encoding='utf8'))
            voxel = float(meta.get('voxel', 0.04))
            self.voxel = voxel if MIN_VOXEL <= voxel <= MAX_VOXEL else DEFAULT_VOXEL
            self.trajectory = meta.get('trajectory', [])
            self.history = []
            self.session = uuid.uuid4().hex[:12]
            self.revision += 1
            self.topology += 1
            self.reindex()
            self.error = ''
            self.loaded = True
            self.last_stamp = None
            self.frames = 0
            self.full = False

    def new(self):
        with self.lock:
            if self.capturing:
                raise ValueError('Pause capture before creating a scene')
            if len(self.points):
                self.save('autosave-before-new')
            self.points = np.empty((0, 3), np.float32)
            self.colors = np.empty((0, 3), np.uint8)
            self.keys.clear()
            self.history.clear()
            self.trajectory.clear()
            self.session = uuid.uuid4().hex[:12]
            self.revision += 1
            self.topology += 1
            self.frames, self.full, self.error = 0, False, ''
            self.last_stamp = None
            self.loaded = False


def write_ply(path, xyz, rgb):
    header = ('ply\nformat binary_little_endian 1.0\n' + f'element vertex {len(xyz)}\n' +
              'property float x\nproperty float y\nproperty float z\n' +
              'property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n')
    records = np.empty(len(xyz), dtype=[('xyz', '<f4', (3,)), ('rgb', 'u1', (3,))])
    records['xyz'], records['rgb'] = xyz, rgb
    with open(path, 'wb') as out:
        out.write(header.encode())
        out.write(records.tobytes())


def write_pcd(path, xyz, rgb):
    header = ('# .PCD v0.7\nVERSION 0.7\nFIELDS x y z rgb\nSIZE 4 4 4 4\nTYPE F F F U\n'
              f'COUNT 1 1 1 1\nWIDTH {len(xyz)}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS {len(xyz)}\nDATA binary\n')
    records = np.empty(len(xyz), dtype=[('xyz', '<f4', (3,)), ('rgb', '<u4')])
    records['xyz'] = xyz
    c = rgb.astype(np.uint32)
    records['rgb'] = (c[:, 0] << 16) | (c[:, 1] << 8) | c[:, 2]
    with open(path, 'wb') as out:
        out.write(header.encode())
        out.write(records.tobytes())
