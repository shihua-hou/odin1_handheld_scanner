"""Persistent CPU/GPU Gaussian training jobs and the LAN scene viewer routes."""
import asyncio
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid
from aiohttp import web
from .core import atomic_json


class GaussianJobs:
    def __init__(self,root):
        self.root=Path(root)
        self.directory=self.root/'gaussian_jobs'
        self.directory.mkdir(exist_ok=True)
        (self.root/'datasets').mkdir(exist_ok=True)

    def path(self,identifier):
        if not re.fullmatch('[a-f0-9]{12}',identifier):raise ValueError('Invalid reconstruction ID')
        result=self.directory/identifier
        if not (result/'job.json').is_file():raise ValueError('Reconstruction not found')
        return result

    def list(self):
        items=[]
        for path in self.directory.glob('*/job.json'):
            try:items.append(json.loads(path.read_text()))
            except (ValueError,OSError):continue
        return sorted(items,key=lambda item:item['created_at'],reverse=True)

    def recordings(self):
        return [dict(id=p.parent.name,modified_at=p.stat().st_mtime)
                for p in sorted((self.root/'recordings').glob('*/metadata.yaml'),key=lambda p:p.stat().st_mtime,reverse=True)]

    def start(self,parameters):
        if any(job['state'] in ('queued','exporting','training') for job in self.list()):
            raise ValueError('已有重建任务正在运行，请等待完成或取消')
        recordings={item['id'] for item in self.recordings()}
        bag=parameters.get('recording')
        if bag not in recordings:raise ValueError('请选择已结束录制的数据包')
        iterations=int(parameters.get('iterations',3000))
        width=int(parameters.get('width',960))
        downscale=float(parameters.get('downscale',3))
        seeds=int(parameters.get('seed_points',20000))
        if not 100<=iterations<=30000 or width not in (480,640,960,1280) or not 1<=downscale<=4 or not 1000<=seeds<=100000:
            raise ValueError('Invalid training parameters')
        identifier=uuid.uuid4().hex[:12]
        path=self.directory/identifier;path.mkdir()
        job=dict(id=identifier,state='queued',created_at=time.time(),recording=bag,
                 iterations=iterations,width=width,downscale=downscale,seed_points=seeds,
                 step=0,progress=0,message='等待训练器',engine='OpenSplat 3D Gaussian Splatting',
                 device='CPU',model_ready=False)
        dataset=parameters.get('dataset')
        if dataset:
            if not re.fullmatch('[A-Za-z0-9_-]+',dataset):raise ValueError('Invalid dataset ID')
            if not (self.root/'datasets'/dataset/'transforms.json').is_file():raise ValueError('Dataset not found')
            job['dataset']=dataset
        atomic_json(path/'job.json',job)
        script=(f'source /opt/ros/humble/setup.bash; export PYTHONNOUSERSITE=1; cd {shlex.quote(str(self.root))}; '
                f'exec .venv/bin/python -m handheld.gaussian_worker {shlex.quote(str(path))}')
        result=subprocess.run(['systemd-run','--user','--collect','--unit=odin-gaussian-'+identifier,
                '--property=MemoryMax=10G','--property=CPUQuota=250%',
                '/bin/bash','-c',script],capture_output=True,text=True,timeout=15)
        if result.returncode:
            job.update(state='failed',message=result.stderr[-1000:]);atomic_json(path/'job.json',job)
            raise ValueError('无法启动重建任务：'+result.stderr[-500:])
        return job

    def cancel(self,identifier):
        path=self.path(identifier)
        subprocess.run(['systemctl','--user','stop','odin-gaussian-'+identifier],capture_output=True,timeout=20)
        job=json.loads((path/'job.json').read_text())
        if job['state'] not in ('completed','failed'):
            job.update(state='cancelled',message='用户取消');atomic_json(path/'job.json',job)
        return job


def register_gaussian(app,runtime):
    manager=GaussianJobs(runtime.root)
    async def status(request):
        trainer=runtime.root/'third_party/OpenSplat/build/opensplat'
        return web.json_response(dict(jobs=manager.list(),recordings=manager.recordings(),
                                      trainer_ready=trainer.is_file(),device='CPU',
                                      capture_recording=runtime.status()['recording']))
    async def start(request):
        return web.json_response(await asyncio.to_thread(manager.start,await request.json()))
    async def cancel(request):
        return web.json_response(await asyncio.to_thread(manager.cancel,request.match_info['id']))
    async def asset(request):
        directory=manager.path(request.match_info['id']);name=request.match_info['file']
        allowed={'model.ply','model.splat','training.log','comparison.jpg','cameras.json','view.json'}
        if name not in allowed:raise ValueError('Unsupported reconstruction asset')
        job=json.loads((directory/'job.json').read_text())
        if name in ('model.ply','model.splat') and not job.get('model_ready'):
            raise ValueError('模型尚未生成，请等待训练检查点')
        path=directory/name
        if name=='model.ply' and job['state']!='completed':path=directory/'preview.ply'
        if not path.is_file():raise ValueError('文件尚未生成')
        return web.FileResponse(path,headers={'Cache-Control':'no-cache'})
    async def image(request):
        directory=manager.path(request.match_info['id'])
        job=json.loads((directory/'job.json').read_text())
        dataset=job.get('dataset',request.match_info['id'])
        path=runtime.root/'datasets'/dataset/'images/frame_00000.jpg'
        if not path.is_file():raise ValueError('数据集尚未导出')
        return web.FileResponse(path)
    async def viewer(request):
        return web.FileResponse(Path(__file__).parent/'web/gaussian.html')
    app.router.add_get('/gaussian',viewer)
    app.router.add_get('/api/gaussian/status',status)
    app.router.add_post('/api/gaussian/start',start)
    app.router.add_post('/api/gaussian/{id}/cancel',cancel)
    app.router.add_get('/api/gaussian/{id}/reference.jpg',image)
    app.router.add_get('/api/gaussian/{id}/{file}',asset)
    return manager
