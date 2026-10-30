"""Run on the sensor host: verify the complete pipeline using actual ODIN1 data."""
import hashlib
import json
from pathlib import Path
import time
import urllib.request
from urllib.parse import quote
import numpy as np

root=Path(__file__).resolve().parents[1]
def api(path,data=None):
    request=urllib.request.Request('http://127.0.0.1:8080/api/'+path,
        data=None if data is None else json.dumps(data).encode(),
        headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=45) as response:
        return json.loads(response.read())

def digest(scene_id):
    with np.load(root/'maps'/scene_id/'scene.npz',allow_pickle=False) as a:
        return hashlib.sha256(a['points'].tobytes()+a['colors'].tobytes()).hexdigest()

print('Starting actual device capture',flush=True)
api('action/pause',{})
api('action/new',{})
api('action/start',{})
deadline=time.time()+45
while time.time()<deadline:
    state=api('status')
    if state['connected'] and state['points']>100:
        break
    time.sleep(1)
else:
    raise RuntimeError('No real SLAM points: '+json.dumps(state))
api('action/record-start',{})
print('Real SLAM data online; recording eight seconds',flush=True)
time.sleep(8)
api('action/record-stop',{})
api('action/pause',{})
state=api('status')
assert not state['error'],state['error']
saved=api('action/save',{'name':'首次实机扫描'})
with np.load(root/'maps'/saved['id']/'scene.npz',allow_pickle=False) as data:
    assert len(data['points'])>100 and np.isfinite(data['points']).all()
    first=data['points'][0].astype(float).tolist()
original=digest(saved['id'])
edited=api('action/edit',{'action':'delete_sphere','center':first,'radius':.15})
assert edited['points']<state['points'],(edited['points'],state['points'])
api('action/edit',{'action':'undo'})
restored=api('action/save',{'name':'实机编辑撤销验证'})
assert original==digest(restored['id'])
for kind in ['ply','pcd']:
    req=urllib.request.Request('http://127.0.0.1:8080/api/scenes/'+quote(saved['id'])+'/'+kind)
    with urllib.request.urlopen(req,timeout=15) as response:
        assert len(response.read())>1000
bags=list((root/'recordings').glob('*/metadata.yaml'))
assert bags,'No finalised ROS bag metadata'
device=api('action/device-save',{})
deadline=time.time()+40
while time.time()<deadline:
    device=api('status')['device_save']
    if device and device['state']=='completed':
        assert Path(device['path']).is_file() and Path(device['path']).stat().st_size>1000
        break
    time.sleep(1)
else:
    raise RuntimeError('Device map transfer did not complete: '+json.dumps(device))
result=dict(status=state,saved=saved['id'],restored=restored['id'],sha256=original,
    deleted_points=state['points']-edited['points'],rosbag=str(max(bags,key=lambda p:p.stat().st_mtime)),
    device_save=device,verified_at=time.strftime('%Y-%m-%d %H:%M:%S'))
(root/'logs/live-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(result,ensure_ascii=False),flush=True)
