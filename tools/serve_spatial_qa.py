import json
from pathlib import Path
import shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from handheld.server import Runtime,make_app

root=Path(__file__).resolve().parents[1]
qa=root/'runtime/spatial-qa';(qa/'runtime').mkdir(parents=True,exist_ok=True)
source=max((p for p in (root/'maps').glob('*/pages.sqlite')),
           key=lambda p:json.loads((p.parent/'metadata.json').read_text())['points'])
target=qa/'runtime/point-pages.sqlite'
if not target.exists():shutil.copyfile(source,target)
runtime=Runtime(qa,ros=False)
print('QA_POINTS',runtime.scene.count,flush=True)
web.run_app(make_app(runtime),host='0.0.0.0',port=8081,access_log=None)
