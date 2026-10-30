export function installDeviceControls({$, action, toast, refreshArchives, clearMeasurement}) {
  const bind=(id,callback)=>$(id)?.addEventListener('click',async()=>{try{await callback();}catch(error){toast(error.message,true);}});
  bind('start',async()=>{await action('start');toast('采集已启动，等待设备 SLAM 点云');});
  bind('pause',async()=>{await action('pause');toast('已暂停，可编辑或保存当前点云');});
  bind('new',async()=>{if(!confirm('创建新场景？当前非空场景将自动保存。请先暂停采集。'))return;await action('new');clearMeasurement();await refreshArchives();toast('新场景已就绪');});
  bind('save',async()=>{const result=await action('save',{name: $('sceneName').value});if(result){await refreshArchives();toast('场景已保存；点云与三角表面可分别导出');}});
  bind('stopDriver',async()=>{if(confirm('停止设备驱动？当前场景仍可编辑保存，重新采集需创建新场景。')){await action('driver-stop');toast('驱动已停止');}});
  $('record')?.addEventListener('change',async()=>{try{await action($('record').checked?'record-start':'record-stop');toast('录制状态已更新');}catch(error){$('record').checked=!$('record').checked;toast(error.message,true);}});
  $('olxRecord')?.addEventListener('change',async()=>{try{await action('olx-enable',{enabled:$('olxRecord').checked});$('olxRecordState').textContent=$('olxRecord').checked?'ON':'OFF';toast('OLX 录制选项已更新');}catch(error){$('olxRecord').checked=!$('olxRecord').checked;toast(error.message,true);}});
}
