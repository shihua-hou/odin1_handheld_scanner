(function(){
  const box=document.createElement('div');box.className='focus-card focus-stats';box.innerHTML='<span>TF <b id="focusTf">等待</b></span><span>map→odom <b id="focusMapOdom">—</b></span><span>odom→base_link <b id="focusBase">—</b></span>';document.querySelector('.focus-top')?.append(box);
  const show=(id,text,good)=>{const e=document.getElementById(id);if(e){e.textContent=text;e.style.color=good?'#78e7bd':'#f5c76b';}};
  async function refresh(){try{const r=await fetch('/api/frames');const frames=await r.json();const map=frames.odom?.parent==='map'||frames['odom']?.parent==='map';const base=frames.base_link?.parent==='odom'||frames.base_link?.parent==='base_footprint';show('focusTf',Object.keys(frames).length?'已发现':'未发现',Object.keys(frames).length>0);show('focusMapOdom',map?'已连接':'未提供',map);show('focusBase',frames.base_link?'已连接':'未提供',!!frames.base_link);}catch(e){show('focusTf','异常',false);}}
  refresh();setInterval(refresh,1500);
})();