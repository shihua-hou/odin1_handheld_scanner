import {createApi} from './modules/api.js';
import {createStatusSocket} from './modules/websocket.js';
import {createPointCloudViewer} from './modules/point-cloud-viewer.js';
import {createSceneManager} from './modules/scene-manager.js';
import {installDeviceControls} from './modules/device-control.js';
import * as THREE from 'three';
import {OrbitControls} from './vendor/OrbitControls.js';
import {SpatialCloud} from './spatial-cloud.js';
import {createWorldAxes} from './modules/world-axes.js?v=highlight-2';

const $ = id => document.getElementById(id);
let state = {}, socket, sceneRevision = -1, tool = 'orbit', measure = [], cloud, rgbColors;
let busy = false, toastTimer, reconnectTimer, colorMode = 'rgb', firstCloud = true;
const view = $('viewport');
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(55, 1, .03, 2000);
camera.up.set(0,0,1); camera.position.set(10,-14,10);
let renderer;
try { renderer = new THREE.WebGLRenderer({antialias:true,alpha:true}); }
catch (error) { $('statusMessage').textContent = '浏览器不支持 WebGL，请启用硬件加速'; throw error; }
renderer.setPixelRatio(Math.min(devicePixelRatio, 2)); view.appendChild(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true; controls.dampingFactor = .08;
const grid = new THREE.GridHelper(60,60,0x818b99,0x414b58); grid.material.transparent=true;grid.material.opacity=.58;grid.material.depthWrite=false;grid.rotation.x = Math.PI/2; scene.add(grid);
const worldAxes = createWorldAxes({scene, camera, viewport:view});
const trajectory = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({color:0xc3ea94})); scene.add(trajectory);
const measureLine = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({color:0xffd37b})); scene.add(measureLine);
const markers = new THREE.Group(); scene.add(markers);
const box = new THREE.Box3Helper(new THREE.Box3(new THREE.Vector3(-10,-10,-2),new THREE.Vector3(10,10,5)),0xb8e68b); box.visible=false; scene.add(box);
const raycaster = new THREE.Raycaster(); raycaster.params.Points.threshold=.08;
let renderDirty=true;
function resize(){renderDirty=true;const w=view.clientWidth,h=view.clientHeight;renderer.setSize(w,h);camera.aspect=w/h;camera.updateProjectionMatrix();}
new ResizeObserver(resize).observe(view); resize();
const spatial=new SpatialCloud({scene,camera,renderer,controls,onChange:metrics=>{
 $('viewCount').textContent=metrics.visiblePoints.toLocaleString()+' / '+metrics.totalPoints.toLocaleString()+' points · 视角细节';
 $('empty').hidden=metrics.totalPoints>0;renderDirty=true;
}});
cloud=spatial.group;window.odinSpatial=spatial;
renderer.setAnimationLoop(now=>{
 controls.update();spatial.tick(now);
 if(renderDirty||spatial.dirty){worldAxes.update();renderer.render(scene,camera);spatial.renderedFrames++;renderDirty=false;spatial.dirty=false;}
});

function toast(text,error=false){clearTimeout(toastTimer);$('toast').textContent=text;$('toast').className=error?'error':'';$('toast').style.display='block';toastTimer=setTimeout(()=>$('toast').style.display='none',5500);}
const apiClient=createApi();
async function api(path,body){return apiClient.request(path,body);}
async function action(op,body={}){if(busy)return;busy=true;try{const data=await api('action/'+op,body);if(data.points!==undefined&&data.session)updateStatus(data);return data;}finally{busy=false;}}
function bind(id,callback){$(id).addEventListener('click',async()=>{try{await callback();renderDirty=true;}catch(error){toast(error.message,true);}});}
function updateStatus(data){state=data;updateVoxelStatus(data);$('pointCount').textContent=data.points.toLocaleString();$('frameCount').textContent=data.received_frames.toLocaleString();const deviceReady=!!data.usb_connected;$('deviceDot').classList.toggle('online',deviceReady);$('deviceText').textContent=data.connected?'SLAM 数据在线':data.driver_running?'驱动运行中，等待数据':deviceReady?'USB 已连接 · 驱动待机':'未检测到 Odin1';$('modeBadge').textContent=data.capturing?'CAPTURING':'STANDBY';$('captureState').textContent=data.capturing?(data.connected?'采集中':'等待设备'):'已暂停';$('cloudAge').textContent=data.cloud_age===null?'等待第一帧数据':`${data.cloud_age.toFixed(1)} 秒前更新`;$('liveLabel').textContent=data.capturing?'● 采集中':'○ 已暂停';$('sessionId').textContent=data.session;$('pose').textContent=data.pose?data.pose.slice(0,3).map(n=>n.toFixed(3)).join(' / '):'—';$('record').checked=data.recording;$('recordState').textContent=data.recording?'REC':'OFF';$('undo').disabled=!data.undo||data.capturing;for(const id of ['crop','deleteBox','downsample'])$(id).disabled=data.capturing;for(const button of document.querySelectorAll('[data-preset]'))button.disabled=data.capturing;const pts=data.trajectory?.flat()||[];trajectory.geometry.dispose();trajectory.geometry=new THREE.BufferGeometry();trajectory.geometry.setAttribute('position',new THREE.Float32BufferAttribute(pts,3));const diagnostic=data.error||data.ros_error||(data.connected?`设备在线 · ${data.frame} 坐标系 · SLAM 彩色点云正在累计`:(deviceReady?'USB 已连接但尚未收到 SLAM 数据 · 请检查驱动和 USB 3.0 连接':'等待 ODIN1 · 请确认供电、USB 连接和 Zadig 驱动'));$('statusMessage').textContent=diagnostic;}
const viewer=createPointCloudViewer({spatial,camera,controls,renderer,setDirty:()=>{renderDirty=true;}});
function fit(){viewer.fit();}
function applyColors(){viewer.setColorMode(colorMode);}
let displayMode='points',surfaceGeneration=-1,surfaceSocket;
const surfaceObjects=new Map();const surfaceGroup=new THREE.Group();scene.add(surfaceGroup);
function setDisplayMode(mode){displayMode=mode;if(cloud)cloud.visible=mode==='points';surfaceGroup.visible=mode==='surface';$('surfaceMode').classList.toggle('active',mode==='surface');$('pointsMode').classList.toggle('active',mode==='points');}
bind('surfaceMode',()=>setDisplayMode('surface'));bind('pointsMode',()=>setDisplayMode('points'));
function clearSurfaces(){for(const object of surfaceObjects.values()){surfaceGroup.remove(object);object.geometry.dispose();object.material.dispose();}surfaceObjects.clear();}
function surfacePacket(buffer){const head=new Uint32Array(buffer,0,5);if(head[0]!==0x53555246)return;if(surfaceGeneration!==head[1]){clearSurfaces();surfaceGeneration=head[1];}const name=new TextDecoder().decode(new Uint8Array(buffer,20,head[3])).trim();const n=head[4],base=20+head[3];if(buffer.byteLength!==base+n*16)return;const old=surfaceObjects.get(name);if(old){surfaceGroup.remove(old);old.geometry.dispose();old.material.dispose();surfaceObjects.delete(name);}if(!n)return;
 const position=new Float32Array(n*3),color=new Float32Array(n*3),data=new DataView(buffer);for(let i=0;i<n;i++){let b=base+i*16,a=i*3;position[a]=data.getFloat32(b,true);position[a+1]=data.getFloat32(b+4,true);position[a+2]=data.getFloat32(b+8,true);color[a]=data.getUint8(b+12)/255;color[a+1]=data.getUint8(b+13)/255;color[a+2]=data.getUint8(b+14)/255;}
 const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(position,3));geometry.setAttribute('color',new THREE.BufferAttribute(color,3));geometry.computeBoundingSphere();const mesh=new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({vertexColors:true,side:THREE.DoubleSide}));surfaceObjects.set(name,mesh);surfaceGroup.add(mesh);$('empty').hidden=true;
}
function connectSurface(){if(surfaceSocket){surfaceSocket.onclose=null;surfaceSocket.close();}surfaceSocket=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/surface');surfaceSocket.binaryType='arraybuffer';surfaceSocket.onmessage=event=>{if(event.data instanceof ArrayBuffer)surfacePacket(event.data);else{const data=JSON.parse(event.data);if(surfaceGeneration!==data.generation){clearSurfaces();surfaceGeneration=data.generation;}$('surfaceStats').textContent=data.triangles?data.triangles.toLocaleString()+' 个三角面 · '+data.integration_seconds.toFixed(2)+' s/帧':'连续表面等待深度数据';if(data.error)$('statusMessage').textContent=data.error;}};surfaceSocket.onclose=()=>setTimeout(connectSurface,2000);}
// Surface fusion disabled by user preference.
const statusSocket=createStatusSocket({
  url:()=>((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/ws?mode=spatial'),
  onStatus:updateStatus,
  onConnection:(status)=>{$('transport').textContent=status==='connected'?'● 实时连接':status==='closed'?'○ 连接中断':'连接异常';},
  onError:()=>toast('状态数据解析失败',true)
});
function connect(){statusSocket.connect();}

let voxelDirty=false, voxelSession;
function voxelLabel(value){const mm=Number((value*1000).toFixed(2));return mm<10?mm+' mm':Number((mm/10).toFixed(2))+' cm';}
function updateVoxelStatus(data){
  const label=voxelLabel(data.voxel);$('resolutionText').textContent=label;
  if(voxelSession!==data.session){voxelDirty=false;voxelSession=data.session;}
  if(!voxelDirty){const mm=Number((data.voxel*1000).toFixed(2));$('mappingVoxel').value=mm;$('mappingVoxelRange').value=mm;$('mappingVoxelLabel').textContent=label;}
  $('applyVoxel').disabled=data.capturing;
}
function voxelDraft(mm){voxelDirty=true;$('mappingVoxel').value=mm;$('mappingVoxelRange').value=mm;$('mappingVoxelLabel').textContent=voxelLabel(mm/1000);}
$('mappingVoxelRange').addEventListener('input',()=>voxelDraft(Number($('mappingVoxelRange').value)));
$('mappingVoxel').addEventListener('input',()=>{if(Number.isFinite($('mappingVoxel').valueAsNumber))voxelDraft($('mappingVoxel').valueAsNumber);});
bind('applyVoxel',async()=>{const mm=$('mappingVoxel').valueAsNumber;if(!Number.isFinite(mm)||mm<1||mm>1000)throw Error('请输入 1 到 1000 mm');const result=await action('set-voxel',{voxel:mm/1000});if(result){voxelDirty=false;updateVoxelStatus(result);await refreshArchives();toast('体素已设置为 '+voxelLabel(result.voxel)+'；调细后继续扫描以补充细节');}});
const sceneManager=createSceneManager({api:apiClient,action, $,toast,setDisplayMode,clearMeasurement});
const refreshArchives=()=>sceneManager.refresh();
installDeviceControls({$,action,toast,refreshArchives,clearMeasurement});

function bounds(){const min=['X','Y','Z'].map(a=>Number($('min'+a).value)),max=['X','Y','Z'].map(a=>Number($('max'+a).value));if(![...min,...max].every(Number.isFinite)||min.some((v,i)=>v>max[i]))throw Error('请填写有效的范围');return{min,max};}
function updateBox(){try{const b=bounds();box.box.min.fromArray(b.min);box.box.max.fromArray(b.max);box.visible=$('showBox').checked;}catch{box.visible=false;}}
for(const id of ['minX','maxX','minY','maxY','minZ','maxZ','showBox'])$(id).addEventListener('input',updateBox);
bind('crop',async()=>{await action('edit',{action:'crop',...bounds()});toast('已保留框内点，可撤销');});
bind('deleteBox',async()=>{await action('edit',{action:'delete_box',...bounds()});toast('已删除框内点，可撤销');});
bind('downsample',async()=>{await action('edit',{action:'downsample',voxel:Number($('voxel').value),...bounds()});toast('降采样完成');});
bind('undo',async()=>{await action('edit',{action:'undo'});toast('已撤销');});
bind('fit',fit);document.addEventListener('keydown',e=>{if(['INPUT','TEXTAREA'].includes(document.activeElement.tagName))return;if(e.key.toLowerCase()==='f'||e.key.toLowerCase()==='r')fit();});
bind('perspective',()=>{$('top').classList.remove('active');$('perspective').classList.add('active');fit();});
bind('top',()=>{$('perspective').classList.remove('active');$('top').classList.add('active');const dist=camera.position.distanceTo(controls.target);camera.position.copy(controls.target).add(new THREE.Vector3(0,-.001,Math.max(dist,3)));controls.update();});
bind('grid',()=>{grid.visible=!grid.visible;$('grid').classList.toggle('active',grid.visible);});
bind('colorMode',()=>{colorMode=colorMode==='rgb'?'height':'rgb';$('colorMode').textContent=colorMode==='rgb'?'RGB':'高度';applyColors();});
$('pointSize').addEventListener('input',()=>{spatial.material.size=Number($('pointSize').value);spatial.dirty=true;});
const presets={detail:{voxel:3,pointSize:2.5,hint:'细节浏览：3 mm 体素，点大小 2.5 px。适合近距离观察。'},indoor:{voxel:5,pointSize:2,hint:'室内浏览：5 mm 体素，点大小 2 px。适合日常建图。'},large:{voxel:30,pointSize:1.5,hint:'大场景：30 mm 体素，点大小 1.5 px。优先稳定帧率。'}};
for(const button of document.querySelectorAll('[data-preset]'))button.addEventListener('click',async()=>{const preset=presets[button.dataset.preset];if(!preset)return;try{const result=await action('set-voxel',{voxel:preset.voxel/1000});if(result)updateVoxelStatus(result);$('pointSize').value=preset.pointSize;spatial.material.size=preset.pointSize;spatial.dirty=true;document.querySelectorAll('[data-preset]').forEach(item=>item.classList.toggle('active',item===button));$('presetHint').textContent=preset.hint;toast('已应用 '+button.textContent+' 预设');}catch(error){toast(error.message,true);}});
for(const name of ['Edit','Info'])bind('tab'+name,()=>{$('editPanel').hidden=name!=='Edit';$('infoPanel').hidden=name!=='Info';$('tabEdit').classList.toggle('active',name==='Edit');$('tabInfo').classList.toggle('active',name==='Info');});
function clearMeasurement(){measure=[];measureLine.geometry.dispose();measureLine.geometry=new THREE.BufferGeometry();for(const marker of [...markers.children]){markers.remove(marker);marker.geometry.dispose();marker.material.dispose();}$('measurement').textContent='测距：依次点击两个点';}
document.querySelectorAll('[data-tool]').forEach(b=>b.onclick=()=>{tool=b.dataset.tool;document.querySelectorAll('[data-tool]').forEach(x=>x.classList.toggle('active',x===b));view.style.cursor=tool==='orbit'?'default':'crosshair';clearMeasurement();});
let pointerDown;
renderer.domElement.addEventListener('pointerdown',e=>{pointerDown=[e.clientX,e.clientY];});
renderer.domElement.addEventListener('pointerup',async e=>{if(e.button!==0||tool==='orbit'||!cloud||!pointerDown||Math.hypot(e.clientX-pointerDown[0],e.clientY-pointerDown[1])>5)return;const rect=renderer.domElement.getBoundingClientRect();raycaster.setFromCamera(new THREE.Vector2((e.clientX-rect.left)/rect.width*2-1,-(e.clientY-rect.top)/rect.height*2+1),camera);raycaster.params.Points.threshold=Math.max(.025,camera.position.distanceTo(controls.target)*.003);const hit=spatial.pick(raycaster);if(!hit){toast('请点击一个可见的点');return;}renderDirty=true;const point=new THREE.Vector3().fromBufferAttribute(hit.object.geometry.attributes.position,hit.index);$('selection').textContent=point.toArray().map(n=>n.toFixed(3)).join(', ')+' m';if(tool==='erase'){try{await action('edit',{action:'delete_sphere',center:point.toArray(),radius:Number($('radius').value)});toast('已删除选中半径内的点');}catch(error){toast(error.message,true);}return;}if(measure.length===2)clearMeasurement();measure.push(point);const marker=new THREE.Mesh(new THREE.SphereGeometry(.035,12,8),new THREE.MeshBasicMaterial({color:0xffd37b}));marker.position.copy(point);markers.add(marker);if(measure.length===2){measureLine.geometry.dispose();measureLine.geometry=new THREE.BufferGeometry().setFromPoints(measure);$('measurement').textContent='距离：'+measure[0].distanceTo(measure[1]).toFixed(3)+' m';}});
async function initialize(){updateStatus(await api('status'));connect();await refreshArchives();}
initialize().catch(error=>{toast(error.message,true);reconnectTimer=setTimeout(()=>location.reload(),3000);});
