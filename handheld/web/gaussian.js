import * as GaussianSplats3D from './vendor/gaussian-splats-3d.module.js';
const $=id=>document.getElementById(id);
let viewer=null,currentJob=null,currentVersion=null,loading=false,lastJobs=[];
const initial={position:[0,-4,2],target:[0,0,0]};
async function api(path,data){const response=await fetch('/api/'+path,{method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});if(!response.ok){let d;try{d=await response.json();}catch{d={error:'请求失败 '+response.status};}throw Error(d.error);}return response.json();}
function message(text,error=false){$('gsMessage').textContent=text;$('gsMessage').style.color=error?'#e8a095':'var(--accent)';}
function button(text,callback){const b=document.createElement('button');b.textContent=text;b.onclick=async()=>{try{await callback();}catch(error){message(error.message,true);}};return b;}
function timeLabel(seconds){if(!Number.isFinite(seconds))return'计算中';return seconds<60?Math.round(seconds)+' 秒':seconds<3600?Math.round(seconds/60)+' 分钟':(seconds/3600).toFixed(1)+' 小时';}
async function loadModel(job){if(loading)return;loading=true;message('加载真正的高斯模型…');try{
 if(viewer)await viewer.dispose();
 $('gaussianView').replaceChildren();
 const base='/api/gaussian/'+job.id+'/';
 let position=[0,-4,2],target=[0,0,0],up=[0,0,1];
 try{const response=await fetch(base+'view.json');if(response.ok){const c=await response.json();position=c.position;target=c.target;up=c.up;}}catch{}
 initial.position=position;initial.target=target;
 viewer=new GaussianSplats3D.Viewer({rootElement:$('gaussianView'),cameraUp:up,initialCameraPosition:position,initialCameraLookAt:target,
  sharedMemoryForWorkers:false,gpuAcceleratedSort:false,enableSIMDInSort:false,integerBasedSort:false,
  sphericalHarmonicsDegree:2,selfDrivenMode:true,ignoreDevicePixelRatio:true});
 // PLY preserves learned view-dependent spherical harmonics. Checkpoints use compact SPLAT.
 const file=job.state==='completed'?'model.ply':'model.splat';
 await viewer.addSplatScene(base+file+'?v='+(job.checkpoint_step||job.step),{splatAlphaRemovalThreshold:1,showLoadingUI:true,progressiveLoad:false});
 viewer.start();$('gsEmpty').hidden=true;currentJob=job.id;currentVersion=job.checkpoint_step||job.step;
 $('modelTitle').textContent='已训练的三维高斯场景';$('modelInfo').textContent=`${(job.gaussians||0).toLocaleString()} GAUSSIANS · ${job.state==='completed'?'训练完成':'训练检查点'}`;
 $('gsBottom').textContent='真实 3D Gaussian Splatting · 左键旋转 / 右键平移 / 滚轮缩放';message('高斯场景已加载，可自由浏览。');
}finally{loading=false;}}
function renderJobs(jobs){lastJobs=jobs;$('jobs').replaceChildren();for(const job of jobs){const row=document.createElement('div');row.className='job';const title=document.createElement('b');title.textContent={queued:'等待中',exporting:'准备多视角数据',training:'优化三维高斯',completed:'重建完成',failed:'重建失败',cancelled:'已停止'}[job.state]||job.state;
 const detail=document.createElement('small');detail.textContent=`${new Date(job.created_at*1000).toLocaleString()} · ${job.id}`;
 const info=document.createElement('small');info.textContent=job.message||'';const progress=document.createElement('progress');progress.max=1;progress.value=job.progress||0;
 const metrics=document.createElement('small');metrics.textContent=`${job.step||0} / ${job.iterations} 步${job.loss!==undefined?' · loss '+job.loss.toFixed(4):''}${job.eta_seconds!==undefined?' · 剩余约 '+timeLabel(job.eta_seconds):''}`;
 const actions=document.createElement('div');actions.className='button-row';if(job.model_ready)actions.append(button('浏览高斯场景',()=>loadModel(job)));
 if(job.state==='completed'){for(const file of ['model.ply','model.splat']){const link=document.createElement('a');link.href='/api/gaussian/'+job.id+'/'+file;link.download=file;link.textContent=file.endsWith('.ply')?'下载 PLY':'下载 SPLAT';actions.append(link);}actions.append(button('照片对照',()=>{$('comparisonImage').src='/api/gaussian/'+job.id+'/comparison.jpg';$('comparison').hidden=false;}));}
 if(['queued','exporting','training'].includes(job.state))actions.append(button('停止',()=>api('gaussian/'+job.id+'/cancel',{})));
 row.append(title,detail,info,progress,metrics);if(job.quality){const q=document.createElement('small');q.textContent=`${job.quality.selected_views} 个标定视角 · 移动范围 ${job.quality.camera_translation_extent_m.toFixed(2)} m${job.validation_psnr?' · 留出 PSNR '+job.validation_psnr.toFixed(2)+' dB':''}`;row.append(q);}row.append(actions);$('jobs').append(row);
 }if(!jobs.length)$('jobs').textContent='还没有重建任务';}
async function refresh(){try{const data=await api('gaussian/status');$('hardware').textContent=data.trainer_ready?'训练器已就绪 · 当前使用 CPU，耗时按实际训练进度估算。':'训练器正在部署，采集数据可先保存。';$('train').disabled=!data.trainer_ready||data.jobs.some(j=>['queued','exporting','training'].includes(j.state));$('captureStatus').textContent=data.capture_recording?'● 正在采集同步图像与位姿':'○ 采集已停止';
 const selected=$('recording').value;const options=data.recordings.map(x=>x.id);if(options.join('|')!==[...$('recording').options].map(o=>o.value).join('|')){$('recording').replaceChildren(...options.map(name=>{const o=document.createElement('option');o.value=name;o.textContent=name;return o;}));if(options.includes(selected))$('recording').value=selected;}
 renderJobs(data.jobs);const latest=data.jobs.find(j=>j.model_ready);if(latest&&!viewer&&!loading)await loadModel(latest);
 const exported=data.jobs.find(j=>j.quality);if(exported&&!viewer){$('reference').src='/api/gaussian/'+exported.id+'/reference.jpg';$('reference').hidden=false;}
}catch(error){message(error.message,true);}}
$('photoStart').onclick=async()=>{try{message('正在连接设备并启动照片采集…');await api('action/photo-start',{});message('照片采集已开始，请缓慢移动并保持视角重叠。');await refresh();}catch(e){message(e.message,true);}};
$('photoStop').onclick=async()=>{try{await api('action/record-stop',{});message('采集完成，原始数据已保存。');await refresh();}catch(e){message(e.message,true);}};
$('train').onclick=async()=>{try{const preset={preview:{iterations:500,width:640,downscale:2,seed_points:12000},standard:{iterations:3000,width:960,downscale:2,seed_points:30000},high:{iterations:15000,width:960,downscale:1,seed_points:60000}}[$('quality').value];await api('gaussian/start',{recording:$('recording').value,...preset});message('真实重建任务已启动，窗口关闭后仍会继续。');await refresh();}catch(error){message(error.message,true);}};
$('closeComparison').onclick=()=>{$('comparison').hidden=true;};
$('cameraReset').onclick=()=>{if(viewer?.camera){viewer.camera.position.fromArray(initial.position);viewer.controls?.target.fromArray(initial.target);viewer.controls?.update();}};
await refresh();setInterval(refresh,3000);
