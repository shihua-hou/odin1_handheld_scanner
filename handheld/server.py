"""Self-hosted ROS 2 handheld mapping API and point-cloud workbench."""
import asyncio
import json
import logging
import os
import re
from pathlib import Path
import secrets
import signal
import subprocess
import threading
import time
from aiohttp import web, WSMsgType
from .core import Scene, DEFAULT_VOXEL, MIN_VOXEL, MAX_VOXEL, atomic_json
from .disk_scene import DiskScene
from .camera_preview import CameraPreview, register_camera_preview

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger('odin.workbench')


class Runtime:
    def __init__(self, root=ROOT, ros=True):
        self.root = Path(root)
        self.default_voxel = DEFAULT_VOXEL
        try:
            settings = json.loads((self.root / 'config/mapping.json').read_text())
            value = float(settings['voxel'])
            if MIN_VOXEL <= value <= MAX_VOXEL:
                self.default_voxel = value
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.scene = DiskScene(self.root / 'maps', voxel=self.default_voxel)
        self.driver = None
        self.bag = None
        self.driver_log = None
        self.bag_log = None
        self.olx_enabled = False
        self.auto_device_map_save = True
        try:
            settings = json.loads((self.root / 'config/ui-settings.json').read_text())
            self.auto_device_map_save = bool(settings.get('auto_device_map_save', True))
        except (OSError, ValueError, TypeError):
            pass
        self.olx_before = None
        self.olx_started_at = 0.0
        self.olx_session_dir = None
        try:
            saved = json.loads((self.root / "config/olx-latest.json").read_text())
            candidate = (self.root / saved["folder"]).resolve()
            base = (self.root / "src/odin_ros_driver/recorddata").resolve()
            if base in candidate.parents and candidate.is_dir(): self.olx_session_dir = candidate
        except (OSError, ValueError, KeyError, TypeError): pass
        self.ros_error = 'ROS bridge starting' if ros else 'ROS disabled for test'
        self.node = None
        self.executor = None
        self.surface = None
        self.stopping = threading.Event()
        self.ros_enabled = ros
        self.frames = {}
        self.camera_preview = CameraPreview()
        self.pending_save = None
        self.last_checkpoint = 0
        self.device_save_log_offset = 0
        archives = self.scene.archives()
        if archives and not self.scene.recovered:
            self.scene.load(max(archives, key=lambda item: item['saved_at'])['id'])

    def launch(self, script, log):
        stream = open(self.root / 'logs' / log, 'a', buffering=1)
        env = os.environ.copy()
        if script == 'driver.sh':
            config = self.root / 'config/handheld.yaml'
            if self.olx_enabled:
                content = config.read_text()
                content, count = re.subn(r'(?m)^(\s*recorddata\s*:\s*)\d+(\s*(?:#.*)?$)', r'\g<1>1\g<2>', content)
                if count != 1: raise ValueError('recorddata 配置项不唯一，未启动 OLX 录制')
                config = self.root / 'config/handheld-olx-runtime.yaml'
                config.write_text(content)
            env['ODIN_CONFIG_FILE'] = str(config)
            if self.olx_enabled:
                base = self.root / 'src/odin_ros_driver/recorddata'
                self.olx_before = {p.name for p in base.iterdir() if p.is_dir()} if base.exists() else set()
                self.olx_started_at = time.time()
                self.olx_session_dir = None
        try:
            child = subprocess.Popen(['bash', str(self.root / 'scripts' / script)], cwd=self.root,
                                     stdout=stream, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        except Exception:
            stream.close()
            raise
        return child, stream

    def ensure_driver(self, for_photos=False):
        if not self.ros_enabled:
            raise ValueError('ROS is disabled')
        if not (self.root / 'install/odin_ros_driver/lib/odin_ros_driver/host_sdk_sample').is_file():
            raise ValueError('Driver has not been built successfully')
        if self.driver is None or self.driver.poll() is not None:
            if not for_photos and self.driver is not None and len(self.scene.points):
                with self.scene.lock:
                    self.scene.capturing = False
                    self.scene.loaded = True
                raise ValueError('Driver exited. Save the old scene and create a new scene before restarting.')
            if self.driver_log:
                self.driver_log.close()
            self.driver, self.driver_log = self.launch('driver.sh', 'driver.log')

    @staticmethod
    def terminate(child):
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGINT)
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()

    def usb_connected(self):
        base = Path('/sys/bus/usb/devices')
        try:
            for vendor in base.glob('*/idVendor'):
                if vendor.read_text().strip() != '2207':
                    continue
                product = vendor.parent / 'idProduct'
                if product.exists() and product.read_text().strip() == '0019':
                    return True
        except OSError:
            pass
        return False

    def finish_olx(self):
        if self.olx_before is None: return self.olx_session_dir
        base = self.root / 'src/odin_ros_driver/recorddata'
        found = [p for p in base.iterdir() if p.is_dir() and p.name not in self.olx_before and p.stat().st_mtime >= self.olx_started_at - 5] if base.exists() else []
        self.olx_session_dir = max(found, key=lambda p: p.stat().st_mtime) if found else None
        self.olx_before = None
        if self.olx_session_dir:
            atomic_json(self.root / 'config/olx-latest.json', {'folder': self.olx_session_dir.resolve().relative_to(self.root.resolve()).as_posix()})
        return self.olx_session_dir

    def attach_olx(self, saved):
        if self.olx_session_dir and self.olx_session_dir.is_dir():
            saved['olx'] = {'available': True, 'folder': self.olx_session_dir.resolve().relative_to(self.root.resolve()).as_posix()}
            atomic_json(self.scene.archive_path(saved['id']) / 'metadata.json', saved)
        return saved

    def device_maps(self):
        directory = self.root / 'device_maps'
        try:
            files = sorted((p for p in directory.glob('*.bin') if p.is_file()),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            return [{'name': p.name, 'size': p.stat().st_size,
                     'modified_at': p.stat().st_mtime} for p in files[:100]]
        except OSError:
            return []

    def status(self):
        if self.driver is not None and self.driver.poll() is not None and self.olx_before is not None:
            self.finish_olx()
        if self.driver is not None and self.driver.poll() is not None and self.scene.capturing:
            with self.scene.lock:
                self.scene.capturing = False
                self.scene.loaded = bool(len(self.scene.points))
                self.scene.error = 'Driver exited. Save the old scene and create a new scene.'
        data = self.scene.status()
        if self.pending_save and self.pending_save.get('state') == 'accepted':
            import re
            path = self.root / 'logs/driver.log'
            try:
                with path.open('rb') as stream:
                    stream.seek(self.device_save_log_offset)
                    raw = stream.read(4 * 1024 * 1024)
                    self.device_save_log_offset = stream.tell()
                    text = raw.decode('utf8', errors='replace')
                text = re.sub(r'\x1b\[[0-9;]*[mK]', '', text)
                success = re.search(r'Map transfer completed: ([^\r\n]+)', text)
                if success:
                    self.pending_save.update(state='completed', path=success[1], completed_at=time.time())
                elif 'Map transfer failed' in text:
                    self.pending_save.update(state='failed')
            except OSError:
                pass
        data.update(ros_error=self.ros_error, frames=dict(self.frames), usb_connected=self.usb_connected(), loaded=bool(getattr(self.scene, "loaded", False)), driver_running=self.driver is not None and self.driver.poll() is None,
                    recording=self.bag is not None and self.bag.poll() is None,
                    olx_enabled=self.olx_enabled,
                    olx_recording=bool(self.olx_enabled and self.driver is not None and self.driver.poll() is None),
                    olx_ready=bool(self.olx_session_dir and self.olx_session_dir.is_dir()),
                    device_save=self.pending_save,
                    device_maps=self.device_maps(),
                    auto_device_map_save=self.auto_device_map_save)
        if self.surface:
            data['surface']={**self.surface.volume.status(),'active':self.surface.active,'error':self.surface.error or self.surface.volume.error}
            if self.surface.active:
                data['capturing']=True
                if self.scene.full:data['error']='点云存储已到上限；实时表面继续更新，可暂停保存或新建场景'
        try:
            data['ethernet_carrier'] = Path('/sys/class/net/eno1/carrier').read_text().strip() == '1'
        except OSError:
            data['ethernet_carrier'] = False
        return data

    def ros_main(self):
        try:
            import rclpy
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.qos import qos_profile_sensor_data
            from sensor_msgs.msg import PointCloud2, Imu, CompressedImage
            from nav_msgs.msg import Odometry
            from tf2_msgs.msg import TFMessage
            from .core import decode_cloud
            rclpy.init()
            self.node = rclpy.create_node('odin_handheld_workbench')
            self.executor = SingleThreadedExecutor()
            self.executor.add_node(self.node)
            if os.environ.get('ODIN_ENABLE_SURFACE') == '1':
                from .surface import LiveSurface
                self.surface=LiveSurface(self.root)
            self.last_imu = 0.

            def cloud(msg):
                try:
                    xyz, rgb = decode_cloud(msg)
                    stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
                    self.scene.ingest(xyz, rgb, msg.header.frame_id, stamp)
                except Exception as exc:
                    with self.scene.lock:
                        self.scene.error = str(exc)
                    LOG.exception('Point cloud callback failed')

            def odometry(msg):
                if self.surface:self.surface.add_pose(msg)
                p, q = msg.pose.pose.position, msg.pose.pose.orientation
                now = time.time()
                with self.scene.lock:
                    self.scene.pose = [p.x, p.y, p.z, q.x, q.y, q.z, q.w]
                    if self.scene.capturing and now - self.scene.last_pose >= .2:
                        self.scene.trajectory.append([p.x, p.y, p.z])
                        self.scene.trajectory = self.scene.trajectory[-30000:]
                        self.scene.last_pose = now

            def imu(msg):
                self.last_imu = time.time()

            def tf_message(msg):
                for transform in msg.transforms:
                    parent = transform.header.frame_id.strip('/')
                    child = transform.child_frame_id.strip('/')
                    if not parent or not child:
                        continue
                    t = transform.transform.translation
                    q = transform.transform.rotation
                    self.frames[child] = {'parent': parent, 'translation': [t.x, t.y, t.z], 'rotation': [q.x, q.y, q.z, q.w], 'stamp': time.time()}

            self.node.create_subscription(PointCloud2, '/odin1/cloud_slam', cloud, qos_profile_sensor_data)
            self.node.create_subscription(Odometry, '/odin1/odometry', odometry, qos_profile_sensor_data)
            self.node.create_subscription(Imu, '/odin1/imu', imu, qos_profile_sensor_data)
            self.node.create_subscription(TFMessage, '/tf', tf_message, qos_profile_sensor_data)
            self.node.create_subscription(TFMessage, '/tf_static', tf_message, qos_profile_sensor_data)
            self.node.create_subscription(CompressedImage, '/odin1/image/compressed', self.camera_preview.ingest, qos_profile_sensor_data)
            if self.surface:
                self.node.create_subscription(PointCloud2,'/odin1/cloud_raw',self.surface.add_raw,qos_profile_sensor_data)
                self.node.create_subscription(CompressedImage,'/odin1/image/compressed',self.surface.add_image,qos_profile_sensor_data)
            self.ros_error = ''
            while not self.stopping.is_set() and rclpy.ok():
                self.executor.spin_once(timeout_sec=.1)
        except Exception as exc:
            self.ros_error = str(exc)
            LOG.exception('ROS bridge failed')
        finally:
            if self.surface:self.surface.stop()
            if self.executor:
                self.executor.shutdown()
            if self.node:
                self.node.destroy_node()

    def save_device(self):
        """Acceptance is distinct from device transfer completion, which stays in driver logs."""
        import rclpy
        from odin_ros_driver.srv import SaveMap
        temporary = rclpy.create_node('odin_map_save_' + secrets.token_hex(3))
        try:
            log = self.root / 'logs/driver.log'
            self.device_save_log_offset = log.stat().st_size if log.exists() else 0
            client = temporary.create_client(SaveMap, '/odin1/save_map')
            if not client.wait_for_service(timeout_sec=3):
                raise ValueError('ODIN save service unavailable')
            request = SaveMap.Request()
            request.value = 1
            future = client.call_async(request)
            rclpy.spin_until_future_complete(temporary, future, timeout_sec=8)
            if not future.done() or future.result() is None:
                raise ValueError('Device save request timed out')
            response = future.result()
            if not response.success:
                raise ValueError(f'Device save rejected: rc={response.rc}')
            self.pending_save = dict(state='accepted', at=time.time(),
                                     note='Transfer runs in the driver; check device_maps and driver.log for completion')
            return self.pending_save
        finally:
            temporary.destroy_node()


def make_app(runtime):
    jobs = {}
    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except (ValueError, KeyError, TypeError) as exc:
            return web.json_response({'error': str(exc)}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            LOG.exception('Request failed')
            return web.json_response({'error': 'Operation failed; check logs/web.log'}, status=500)

    @web.middleware
    async def auth(request, handler):
        if request.method != 'GET' and request.headers.get('Origin'):
            from urllib.parse import urlsplit
            if urlsplit(request.headers['Origin']).netloc != request.host:
                raise web.HTTPForbidden(text='Cross-origin request rejected')
        result = await handler(request)
        result.headers['X-Content-Type-Options'] = 'nosniff'
        return result

    app = web.Application(middlewares=[errors, auth], client_max_size=1024 * 1024)
    scene = runtime.scene

    async def index(request):
        return web.FileResponse(ROOT / 'handheld/web/workbench.html', headers={'Cache-Control': 'no-cache'})

    async def console_page(request):
        return web.FileResponse(ROOT / 'handheld/web/console.html', headers={'Cache-Control': 'no-cache'})

    async def status(request):
        return web.json_response(await asyncio.to_thread(runtime.status))

    async def tiles(request):
        return web.json_response(await asyncio.to_thread(scene.manifest),headers={'Cache-Control':'no-store'})

    async def tile(request):
        raw,version=await asyncio.to_thread(scene.tile,int(request.match_info['id']),
            int(request.query.get('level','0')),int(request.query['generation']),int(request.query['version']),True)
        return web.Response(body=raw,content_type='application/octet-stream',headers={'Cache-Control':'no-store','X-Point-Version':str(version)})

    async def block_tile(request):
        raw,version=await asyncio.to_thread(scene.block_tile,request.match_info['key'],
            int(request.query['level']),int(request.query['generation']),int(request.query['version']),True)
        return web.Response(body=raw,content_type='application/octet-stream',headers={'Cache-Control':'no-store','X-Point-Version':str(version)})

    async def export_job(request):
        data = await request.json()
        name = data.get('name') or time.strftime('scene_%Y-%m-%d_%H-%M-%S')
        formats = [x for x in data.get('formats', []) if x in ('ply','pcd','npz','surface')]
        job_id = 'export-' + secrets.token_hex(6)
        jobs[job_id] = {'id':job_id,'state':'queued','progress':0,'message':'导出任务排队中','name':name,'formats':formats}
        async def run():
            try:
                jobs[job_id].update(state='running',progress=8,message='正在保存场景')
                saved = await asyncio.to_thread(scene.save,name)
                runtime.attach_olx(saved)
                archive = scene.archive_path(saved['id'])
                outputs = {}
                for fmt in formats:
                    filename = 'scene.ply' if fmt=='ply' else 'scene.pcd' if fmt=='pcd' else 'scene.npz' if fmt=='npz' else 'surface.ply'
                    if (archive / filename).exists(): outputs[fmt] = '/api/scenes/'+saved['id']+'/'+fmt
                jobs[job_id].update(state='completed',progress=100,message='保存与导出完成',scene=saved,outputs=outputs)
            except Exception as exc:
                LOG.exception('Export job failed')
                jobs[job_id].update(state='failed',progress=100,message='保存或导出失败',error=str(exc))
        asyncio.create_task(run())
        return web.json_response(jobs[job_id],status=202)

    async def job_status(request):
        job = jobs.get(request.match_info['job_id'])
        if not job: raise web.HTTPNotFound(text='Unknown job')
        return web.json_response(job)

    async def frames(request):
        return web.json_response(runtime.status().get('frames', {}))

    async def action(request):
        data = await request.json()
        operation = request.match_info['op']
        if operation == 'olx-enable':
            if runtime.driver is not None and runtime.driver.poll() is None:
                raise ValueError('请先停止驱动，再更改 OLX 录制选项')
            runtime.olx_enabled = bool(data.get('enabled'))
            return web.json_response(runtime.status())
        elif operation == 'device-save-auto':
            runtime.auto_device_map_save = bool(data.get('enabled', True))
            settings_path = runtime.root / 'config/ui-settings.json'
            settings_path.parent.mkdir(exist_ok=True)
            existing = {}
            try: existing = json.loads(settings_path.read_text())
            except (OSError, ValueError, TypeError): pass
            existing['auto_device_map_save'] = runtime.auto_device_map_save
            await asyncio.to_thread(atomic_json, settings_path, existing)
            return web.json_response(runtime.status())
        elif operation == 'start':
            with scene.lock:
                if getattr(scene, 'loaded', False):
                    raise ValueError('Create a new scene before capturing; loaded scenes are edit-only')
                if scene.full:
                    raise ValueError('Map is full; save and create a new scene')
            runtime.ensure_driver()
            with scene.lock:
                scene.capturing = True
                scene.error = ''
            if runtime.surface:runtime.surface.active=True
        elif operation == 'pause':
            with scene.lock:
                scene.capturing = False
            if runtime.surface:runtime.surface.active=False
        elif operation == 'new':
            if runtime.surface:
                runtime.surface.active=False
                runtime.surface.volume.clear()
            await asyncio.to_thread(scene.new)
            await asyncio.to_thread(scene.set_voxel, runtime.default_voxel)
        elif operation == 'set-voxel':
            await asyncio.to_thread(scene.set_voxel, data['voxel'])
            runtime.default_voxel = scene.voxel
            (runtime.root / 'config').mkdir(exist_ok=True)
            await asyncio.to_thread(atomic_json, runtime.root / 'config/mapping.json',
                                    {'voxel': scene.voxel})
        elif operation == 'save':
            saved=await asyncio.to_thread(scene.save, data.get('name', 'scan'))
            runtime.attach_olx(saved)
            if runtime.surface:
                saved['surface_triangles']=await asyncio.to_thread(runtime.surface.volume.save,
                                              scene.archive_path(saved['id'])/'surface.ply')
                await asyncio.to_thread(atomic_json,scene.archive_path(saved['id'])/'metadata.json',saved)
            return web.json_response(saved)
        elif operation == 'load':
            if runtime.surface:
                runtime.surface.active=False
                runtime.surface.volume.clear()
            await asyncio.to_thread(scene.load, data['id'])
        elif operation == 'edit':
            if runtime.surface and runtime.surface.active:raise ValueError('请先暂停实时表面采集再编辑')
            await asyncio.to_thread(scene.edit, data['action'], data)
        elif operation == 'device-save':
            if not scene.status()['connected']:
                raise ValueError('No live SLAM data; device save unavailable')
            return web.json_response(await asyncio.to_thread(runtime.save_device))
        elif operation == 'record-start':
            if not scene.status()['connected']:
                raise ValueError('No live SLAM data to record')
            if runtime.bag is None or runtime.bag.poll() is not None:
                runtime.bag, runtime.bag_log = runtime.launch('record.sh', 'record.log')
        elif operation == 'photo-start':
            with scene.lock:
                scene.capturing = False
                scene.loaded = True
            runtime.ensure_driver(for_photos=True)
            deadline=time.monotonic()+35
            while not scene.status()['connected'] and time.monotonic()<deadline:
                await asyncio.sleep(.3)
            if not scene.status()['connected']:
                raise ValueError('未收到设备数据，请确认 ODIN1 供电和 USB 连接')
            if runtime.bag is None or runtime.bag.poll() is not None:
                if runtime.bag_log:runtime.bag_log.close()
                runtime.bag, runtime.bag_log = runtime.launch('record.sh', 'record.log')
        elif operation == 'record-stop':
            await asyncio.to_thread(runtime.terminate, runtime.bag)
        elif operation == 'driver-stop':
            if runtime.surface:runtime.surface.active=False
            with scene.lock:
                scene.capturing = False
            driver_active = runtime.driver is not None and runtime.driver.poll() is None
            if runtime.auto_device_map_save and driver_active and scene.status()['points']:
                if not scene.status()['connected']:
                    raise ValueError('Odin1 当前没有实时 SLAM 数据，驱动保持运行。请检查设备连接，或在 Studio 关闭“停止时自动保存 .bin”后重试停止。')
                await asyncio.to_thread(runtime.save_device)
                deadline = time.monotonic() + 135
                while True:
                    save_state = (await asyncio.to_thread(runtime.status)).get('device_save') or {}
                    if save_state.get('state') == 'completed':
                        break
                    if save_state.get('state') == 'failed':
                        raise ValueError('设备生成 .bin 失败，驱动保持运行。请查看 driver.log，或关闭自动保存后重试停止。')
                    if time.monotonic() >= deadline:
                        raise ValueError('等待设备 .bin 传输超时，驱动保持运行以避免中断保存。请查看 driver.log，确认后重试。')
                    await asyncio.sleep(.5)
            await asyncio.to_thread(runtime.terminate, runtime.bag)
            await asyncio.to_thread(runtime.terminate, runtime.driver)
            # A restart may reset the device odom frame. Never append it to the old scene.
            scene.loaded = True
        else:
            raise ValueError('Unknown operation')
        return web.json_response(runtime.status())

    async def archives(request):
        return web.json_response(await asyncio.to_thread(scene.archives))

    async def device_map_download(request):
        name = request.match_info['name']
        if Path(name).name != name or not name.endswith('.bin'):
            raise web.HTTPNotFound(text='设备地图文件不存在')
        directory = (runtime.root / 'device_maps').resolve()
        path = (directory / name).resolve()
        if path.parent != directory or not path.is_file():
            raise web.HTTPNotFound(text='设备地图文件不存在')
        return web.FileResponse(path, headers={
            'Content-Disposition': f'attachment; filename="{name}"'})

    async def download_olx(request):
        try:
            meta = json.loads((runtime.scene.archive_path(request.match_info['id']) / 'metadata.json').read_text())
            path = (runtime.root / meta.get('olx', {}).get('folder', '')).resolve()
            base = (runtime.root / 'src/odin_ros_driver/recorddata').resolve()
            if base not in path.parents or not path.is_dir(): raise web.HTTPNotFound(text='此场景没有 OLX 数据包')
        except (OSError, ValueError, TypeError): raise web.HTTPNotFound(text='此场景没有 OLX 数据包')
        res = web.StreamResponse(headers={'Content-Type':'application/gzip','Content-Disposition':'attachment; filename="'+request.match_info['id']+'.olx.tar.gz"'})
        await res.prepare(request)
        proc = await asyncio.create_subprocess_exec('tar','-czf','-','-C',str(path),'.',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            while True:
                chunk=await proc.stdout.read(1024*1024)
                if not chunk: break
                await res.write(chunk)
            code=await proc.wait()
            if code: LOG.error('OLX archive failed: %s',(await proc.stderr.read()).decode(errors='replace'))
        except (ConnectionError,asyncio.CancelledError):
            proc.kill(); await proc.wait(); raise
        await res.write_eof(); return res

    async def download(request):
        from urllib.parse import quote
        directory = scene.archive_path(request.match_info['id'])
        kind = request.match_info['format']
        if kind not in ('ply', 'pcd', 'npz', 'json','mesh'):
            raise ValueError('Unsupported format')
        filename = 'metadata.json' if kind == 'json' else 'surface.ply' if kind=='mesh' else 'scene.' + kind
        disposition = f"attachment; filename=odin-scene.{kind}; filename*=UTF-8''{quote(directory.name + '.' + kind)}"
        return web.FileResponse(directory / filename, headers={'Content-Disposition': disposition})

    async def ws(request):
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=1024)
        await socket.prepare(request)
        writer = None
        try:
            async def publish():
                last_revision = -1
                last_count=0;last_topology=None
                try:
                    while not socket.closed:
                        state = await asyncio.to_thread(runtime.status)
                        await asyncio.wait_for(socket.send_json({'type': 'status', **state}), timeout=5)
                        if request.query.get('mode')!='spatial' and (state['revision'] != last_revision or state['points']!=last_count):
                            with scene.lock:topology=scene.topology;total=len(scene.points)
                            if last_topology!=topology or last_count>total:last_count=0
                            if total==0:
                                await socket.send_bytes(scene.point_packet(0))
                            while last_count<total:
                                packet=await asyncio.to_thread(scene.point_packet,last_count)
                                if int.from_bytes(packet[4:8],'little')!=topology:break
                                await asyncio.wait_for(socket.send_bytes(packet),timeout=10)
                                last_count+=int.from_bytes(packet[16:20],'little')
                            last_revision=state['revision'];last_topology=topology
                        await asyncio.sleep(.5)
                finally:
                    await socket.close()
            writer = asyncio.create_task(publish())
            # Keep reading so pong and close frames are processed by aiohttp.
            async for incoming in socket:
                if incoming.type == WSMsgType.ERROR:
                    break
        except (asyncio.TimeoutError, ConnectionError, ValueError, TypeError):
            pass
        finally:
            if writer:
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
            await socket.close()
        return socket

    async def lifecycle(app):
        worker = None
        if runtime.ros_enabled:
            worker = threading.Thread(target=runtime.ros_main, name='ros-bridge', daemon=True)
            worker.start()
        async def checkpoint():
            while True:
                await asyncio.sleep(120)
                state = scene.status()
                if state['capturing'] and state['points'] and state['revision'] != runtime.last_checkpoint:
                    try:
                        await asyncio.to_thread(scene.checkpoint)
                        runtime.last_checkpoint = state['revision']
                    except Exception:
                        LOG.exception('Checkpoint failed')
        job = asyncio.create_task(checkpoint())
        yield
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        runtime.stopping.set()
        await asyncio.to_thread(runtime.terminate, runtime.bag)
        await asyncio.to_thread(runtime.terminate, runtime.driver)
        if scene.status()['points']:
            await asyncio.to_thread(scene.checkpoint)
        if worker:
            await asyncio.to_thread(worker.join, 4)

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get('/', index)
    app.router.add_get('/workbench', index)
    app.router.add_get('/console', console_page)
    app.router.add_static('/static/', ROOT / 'handheld/web', show_index=False)
    app.router.add_get('/api/status', status)
    app.router.add_get('/api/frames', frames)
    app.router.add_post('/api/jobs/export', export_job)
    app.router.add_get('/api/jobs/{job_id}', job_status)
    app.router.add_get('/api/tiles', tiles)
    app.router.add_get('/api/tiles/{id}', tile)
    app.router.add_get('/api/blocks/{key}', block_tile)
    app.router.add_get('/api/scenes', archives)
    app.router.add_get('/api/device-maps/{name}', device_map_download)
    app.router.add_get('/api/scenes/{id}/olx', download_olx)
    app.router.add_get('/api/scenes/{id}/{format}', download)
    app.router.add_post('/api/action/{op}', action)
    app.router.add_get('/ws', ws)
    async def surface_ws(request):
        socket=web.WebSocketResponse(heartbeat=20,compress=True)
        await socket.prepare(request)
        async def publish():
            versions={};generation=None
            try:
                while not socket.closed:
                    if runtime.surface:
                        state=runtime.surface.volume.status()
                        if generation!=state['generation']:versions={};generation=state['generation']
                        await socket.send_json({'type':'surface',**state,'error':runtime.surface.error or state['error']})
                        packets=await asyncio.to_thread(runtime.surface.volume.packets,versions)
                        for key,version,packet in packets:
                            await asyncio.wait_for(socket.send_bytes(packet),timeout=10);versions[key]=version
                    await asyncio.sleep(.2)
            finally:await socket.close()
        writer=asyncio.create_task(publish())
        try:
            async for message in socket:
                if message.type==WSMsgType.ERROR:break
        finally:
            writer.cancel();await asyncio.gather(writer,return_exceptions=True)
        return socket
    app.router.add_get('/surface',surface_ws)
    from .gaussian import register_gaussian
    register_gaussian(app,runtime)
    register_camera_preview(app, runtime.camera_preview)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    (ROOT / 'logs').mkdir(exist_ok=True)
    runtime = Runtime(ros=os.environ.get('ODIN_NO_ROS') != '1')
    web.run_app(make_app(runtime), host=os.environ.get('ODIN_BIND', '0.0.0.0'),
                port=int(os.environ.get('ODIN_PORT', '8080')), access_log=None)


if __name__ == '__main__':
    main()
