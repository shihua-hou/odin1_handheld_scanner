export function createSceneManager({api, action, $, toast, setDisplayMode, clearMeasurement}) {
  async function refresh() {
    const items=await api.scenes();
    $('archives').replaceChildren();
    if (!items.length) { const p=document.createElement('p'); p.className='muted'; p.textContent='还没有保存的场景'; $('archives').append(p); }
    for (const item of items.slice(0,40)) {
      const row=document.createElement('div'); row.className='archive';
      const name=document.createElement('span'); name.className='archive-title'; name.textContent=item.name; name.title=item.id;
      const detail=document.createElement('small'); detail.textContent=new Date(item.saved_at*1000).toLocaleString()+' · '+item.points.toLocaleString()+' 点';
      const buttons=document.createElement('div'); buttons.className='button-row';
      const open=document.createElement('button'); open.textContent='打开';
      open.onclick=async()=>{try{await action('load',{id:item.id});setDisplayMode('points');clearMeasurement();toast('已打开点云副本；已保存的表面可单独下载');}catch(error){toast(error.message,true);}};
      buttons.append(open);
      if (item.olx?.available) {
        const b=document.createElement('button'); b.textContent='MindCloud OLX';
        b.onclick=async()=>{try{const r=await fetch('/api/scenes/'+encodeURIComponent(item.id)+'/olx');if(!r.ok)throw Error('OLX 数据包下载失败');const u=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=u;a.download=item.id+'.olx.tar.gz';a.click();setTimeout(()=>URL.revokeObjectURL(u),30000);}catch(e){toast(e.message,true);}};
        buttons.append(b);
      }
      for (const format of (item.surface_triangles?['ply','pcd','mesh']:['ply','pcd'])) {
        const b=document.createElement('button'); b.textContent=format==='mesh'?'表面 PLY':format.toUpperCase();
        b.onclick=async()=>{try{const res=await fetch('/api/scenes/'+encodeURIComponent(item.id)+'/'+format);if(!res.ok)throw Error('下载失败');const url=URL.createObjectURL(await res.blob());const a=document.createElement('a');a.href=url;a.download=item.id+(format==='mesh'?'-surface.ply':'.'+format);a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(error){toast(error.message,true);}};
        buttons.append(b);
      }
      row.append(name,detail,buttons); $('archives').append(row);
    }
  }
  return {refresh};
}
