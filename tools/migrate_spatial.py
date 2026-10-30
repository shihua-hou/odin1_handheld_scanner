"""Run on the remote host while the legacy service is paused, before restarting."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import aiohttp
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from handheld.disk_scene import DiskScene,RECORD


async def main():
    root=Path(__file__).resolve().parents[1]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None,sock_read=120)) as client:
        async with client.post('http://127.0.0.1:8080/api/action/pause',json={}) as response:
            response.raise_for_status();status=await response.json()
        expected=status['points'];source_session=status['session']
        stage=root/'runtime/spatial-migration';stage.mkdir(parents=True,exist_ok=True)
        marker=stage/'source.json'
        identity=dict(session=source_session,points=expected,voxel=status['voxel'])
        if marker.exists() and json.loads(marker.read_text())!=identity:raise RuntimeError('Migration source changed')
        marker.write_text(json.dumps(identity))
        scene=DiskScene(stage/'maps',voxel=status['voxel'])
        offset=0;began=time.monotonic();last_log=0
        async with client.ws_connect('http://127.0.0.1:8080/ws',max_msg_size=2**22,heartbeat=20) as socket:
            async for message in socket:
                if message.type==aiohttp.WSMsgType.TEXT:
                    update=json.loads(message.data)
                    if update['session']!=source_session or update['points']!=expected or update['capturing']:
                        raise RuntimeError('Source changed during migration')
                    if not expected:break
                    continue
                if message.type!=aiohttp.WSMsgType.BINARY:raise RuntimeError('Legacy stream ended early')
                data=message.data;head=np.frombuffer(data,'<u4',count=6)
                if head[0]!=0x4f444e32 or head[3]!=offset or head[5]!=expected:raise RuntimeError('Non-contiguous point stream')
                n=int(head[4])
                if offset>=scene.count:
                    records=np.frombuffer(data,RECORD,offset=24,count=n)
                    await asyncio.to_thread(scene.import_records,records['xyz'],records['rgb'][:,:3])
                elif offset+n>scene.count:raise RuntimeError('Unaligned resume boundary')
                offset+=n
                now=time.monotonic()
                if now-last_log>5:
                    print(json.dumps(dict(points=scene.count,total=expected,seconds=round(now-began,1))),flush=True);last_log=now
                if offset==expected:break
        if scene.count!=expected:raise RuntimeError(f'Incomplete migration: {scene.count}/{expected}')
        scene.trajectory=status.get('trajectory',[]);scene.checkpoint()
        scene.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');scene.db.close()
        target=root/'runtime/point-pages.sqlite'
        if target.exists():raise RuntimeError('Destination database already exists; keep staged data for review')
        os.replace(scene.path,target)
        report=dict(state='complete',points=expected,seconds=round(time.monotonic()-began,2),database=str(target))
        (root/'runtime/spatial-migration-result.json').write_text(json.dumps(report))
        print(json.dumps(report),flush=True)

asyncio.run(main())
