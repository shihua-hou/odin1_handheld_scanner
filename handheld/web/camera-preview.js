(function(){
  const shell=document.querySelector('.focus-shell');
  if(!shell)return;
  const panel=document.createElement('aside');panel.className='focus-camera is-collapsed';
  panel.setAttribute('aria-label','实时画面');
  panel.innerHTML='<div class="focus-camera-heading"><h3><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 7h4l2-3h6l2 3h4v14H3Z"/><circle cx="12" cy="13" r="4"/></svg>实时画面</h3><button type="button" aria-controls="focusCameraBody" aria-expanded="false" aria-label="展开实时画面" title="展开实时画面"></button></div><div class="focus-camera-body" id="focusCameraBody"><img alt="Odin1 实时相机画面" hidden><p role="status">等待相机画面</p></div>';
  shell.append(panel);
  const toggle=panel.querySelector('button'),image=panel.querySelector('img'),hint=panel.querySelector('p');
  let expanded=false,timer=null,controller=null,objectUrl=null,epoch=0;
  const glyph=()=>'<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="'+(expanded?'m6 9 6 6 6-6':'m6 15 6-6 6 6')+'"/></svg>';
  toggle.innerHTML=glyph();
  const active=()=>expanded&&document.body.classList.contains('focus-mode')&&!document.body.classList.contains('focus-edit-mode')&&!document.body.classList.contains('focus-studio-mode')&&!document.hidden;
  const clearImage=()=>{image.hidden=true;image.removeAttribute('src');if(objectUrl){URL.revokeObjectURL(objectUrl);objectUrl=null;}};
  const stop=()=>{epoch++;clearTimeout(timer);timer=null;controller?.abort();controller=null;clearImage();hint.hidden=false;};
  const poll=async()=>{
    if(!active()||controller)return;
    const version=epoch,request=new AbortController();controller=request;
    try{
      const response=await fetch('/api/camera/frame',{cache:'no-store',signal:request.signal});
      if(version!==epoch)return;
      if(response.status===204){clearImage();hint.hidden=false;hint.textContent='等待相机画面 · 请确认设备驱动已启动';}
      else if(!response.ok)throw Error('预览暂不可用');
      else{
        const blob=await response.blob();
        if(version!==epoch||!active())return;
        if(!blob.type.startsWith('image/'))throw Error('未收到有效图像');
        const previous=objectUrl;objectUrl=URL.createObjectURL(blob);image.src=objectUrl;
        image.hidden=false;hint.hidden=true;if(previous)URL.revokeObjectURL(previous);
      }
    }catch(error){if(error.name!=='AbortError'&&version===epoch){clearImage();hint.hidden=false;hint.textContent='相机预览连接中，请稍候';}}
    finally{if(controller===request)controller=null;if(version===epoch&&active())timer=setTimeout(poll,250);}
  };
  const sync=()=>{if(active()){if(!controller){clearTimeout(timer);void poll();}}else stop();};
  toggle.onclick=()=>{
    expanded=!expanded;panel.classList.toggle('is-collapsed',!expanded);toggle.innerHTML=glyph();
    toggle.setAttribute('aria-expanded',String(expanded));toggle.title=expanded?'收起实时画面':'展开实时画面';toggle.setAttribute('aria-label',toggle.title);sync();
  };
  image.onerror=()=>{clearImage();hint.hidden=false;hint.textContent='相机图像无法显示，正在重试';};
  new MutationObserver(sync).observe(document.body,{attributes:true,attributeFilter:['class']});
  document.addEventListener('visibilitychange',sync);
  window.addEventListener('pagehide',stop);
})();
