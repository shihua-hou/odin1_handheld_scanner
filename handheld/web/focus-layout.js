(function () {
  const q = id => document.getElementById(id);
  const shell = document.querySelector('.focus-shell');
  if (!shell) return;
  const stylesheet = document.createElement('link');
  stylesheet.rel = 'stylesheet';
  stylesheet.href = '/static/focus-layout.css?v=camera-axes-5';
  document.head.append(stylesheet);

  const panel = q('focusStudioPanel');
  const sections = [...panel.querySelectorAll('.focus-studio-section')];
  const tabs = document.createElement('div');
  tabs.className = 'focus-studio-tabs';
  tabs.setAttribute('role', 'tablist');
  tabs.setAttribute('aria-label', '工作室功能');
  const parameters = document.createElement('div');
  parameters.className = 'focus-studio-section is-active';
  parameters.append(...sections[1].children, ...sections[0].children);
  sections[0].remove();
  sections[1].remove();
  const pages = [parameters, sections[2], sections[3]];
  sections[2].classList.add('focus-history-page');
  panel.append(...pages);
  pages.forEach((page, index) => {
    page.id = 'studio-page-' + index;
    page.setAttribute('role', 'tabpanel');
    page.classList.toggle('is-active', index === 0);
    const button = document.createElement('button');
    button.textContent = ['参数设置', '历史场景', '当前信息'][index];
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-controls', page.id);
    button.setAttribute('aria-selected', String(index === 0));
    button.classList.toggle('active', index === 0);
    button.onclick = () => {
      pages.forEach((item, position) => item.classList.toggle('is-active', index === position));
      [...tabs.children].forEach((item, position) => {
        item.classList.toggle('active', index === position);
        item.setAttribute('aria-selected', String(index === position));
      });
      panel.scrollTop = 0;
    };
    tabs.append(button);
  });
  panel.querySelector('h3').after(tabs);
  panel.querySelector('h3').textContent = '工作室';
  q('focusEditPanel').querySelector('h3').textContent = '点云编辑';
  q('focusEditPanel').querySelector('p').textContent = '暂停采集后编辑，保存可创建场景副本。';
  document.querySelectorAll('[data-mode]').forEach(button => {
    const labels = {view:'Viewer', edit:'Editor', studio:'Studio'};
    button.textContent = labels[button.dataset.mode];
    button.setAttribute('aria-label', labels[button.dataset.mode]);
    const previous = button.onclick;
    button.onclick = event => {
      previous?.call(button, event);
      // Leave destructive tools when changing workspaces.
      if (button.dataset.mode !== 'edit') document.querySelector('[data-tool="orbit"]')?.click();
    };
  });

  const notices = document.querySelector('.focus-notices');
  const title = notices.querySelector('h3');
  const heading = document.createElement('div');
  heading.className = 'focus-notices-title';
  const toggle = document.createElement('button');
  toggle.className = 'focus-notices-toggle';
  const toggleGlyph = collapsed => '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="'+(collapsed?'m6 15 6-6 6 6':'m6 9 6 6 6-6')+'"/></svg>';
  toggle.innerHTML = toggleGlyph(true);
  toggle.title = '展开系统通知';
  toggle.setAttribute('aria-label', '展开系统通知');
  toggle.setAttribute('aria-controls', 'focusNoticeList');
  toggle.setAttribute('aria-expanded', 'false');
  notices.classList.add('is-collapsed');
  title.before(heading);
  heading.append(title, toggle);
  toggle.onclick = () => {
    const collapsed = notices.classList.toggle('is-collapsed');
    toggle.innerHTML = toggleGlyph(collapsed);
    toggle.title = collapsed ? '展开系统通知' : '收起系统通知';
    toggle.setAttribute('aria-label', toggle.title);
    toggle.setAttribute('aria-expanded', String(!collapsed));
  };
  // Keep the real toolbar above the full-screen overlay so it accepts input.
  const tools = document.querySelector('.view-tools');
  const home = document.createComment('view-tools');
  tools.before(home);
  const syncLayout = () => {
    const focused = document.body.classList.contains('focus-mode');
    if (focused && tools.parentNode !== shell) shell.append(tools);
    if (!focused && tools.parentNode === shell) home.after(tools);
  };
  new MutationObserver(syncLayout).observe(document.body, {attributes:true, attributeFilter:['class']});
  syncLayout();
  const statusLabels = {fvOdin:'Odin1',fvCapture:'采集',fvPoints:'点云',fvFrames:'帧数',fvFrame:'坐标系'};
  const statusPill = document.querySelector('.focus-statusbar');
  for (const [id,label] of Object.entries(statusLabels)) {
    const value = q(id);
    const cell = value.parentElement;
    for (const child of [...cell.childNodes]) if(child !== value)child.remove();
    const caption = document.createElement('span');
    caption.className = 'focus-status-caption';
    caption.textContent = label;
    cell.className = 'focus-status-cell';
    cell.dataset.field = id;
    cell.prepend(caption);
  }
  const tone = (id,value) => { q(id).dataset.tone = value; };
  const syncTelemetry = () => {
    const state = window.odinFocusState || {};
    tone('fvOdin',state.error || state.ros_error ? 'error' : state.connected ? 'online' : state.usb_connected ? 'waiting' : 'offline');
    tone('fvCapture',state.capturing ? (state.connected ? 'online' : 'waiting') : 'idle');
    tone('fvPoints','points'); tone('fvFrames','frames'); tone('fvFrame','coordinate');
    statusPill.title = state.error || state.ros_error || '设备连接、采集和场景数据';
  };
  // Values continue to come from the existing live status handler.
  new MutationObserver(syncTelemetry).observe(statusPill,{childList:true,subtree:true});
  syncTelemetry();
  // Local SVG paths keep icons sharp without fonts or a network dependency.
  const icons = {
    play:'<path d="m9 5 11 7-11 7Z"/>',
    pause:'<path d="M8 5v14M16 5v14"/>',
    stop:'<rect x="6" y="6" width="12" height="12" rx="2"/>',
    save:'<path d="M5 3h12l4 4v14H3V3h2ZM7 3v6h10V3M7 21v-8h10v8"/>',
    plus:'<path d="M12 5v14M5 12h14"/>',
    exit:'<path d="M8 3v5H3M16 3v5h5M8 21v-5H3M16 21v-5h5"/>',
    cloud:'<circle cx="5" cy="8" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="19" cy="8" r="1"/><circle cx="5" cy="16" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="16" r="1"/><circle cx="12" cy="20" r="1"/>',
    cube:'<path d="m12 3 9 5v9l-9 5-9-5V8l9-5ZM3 8l9 5 9-5M12 13v9"/>',
    top:'<path d="m12 12 9 5-9 5-9-5 9-5ZM12 2v7M9 6l3 3 3-3"/>',
    grid:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M15 3v18M3 9h18M3 15h18"/>',
    palette:'<path d="M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 0-4h-1a2 2 0 0 1 0-4h5a4 4 0 0 0 4-4c0-3-4-6-9-6Z"/><path d="M7 9h.01M10 6h.01M15 7h.01M6 13h.01"/>',
    height:'<path d="M5 4v16M2 7l3-3 3 3M2 17l3 3 3-3M12 20v-6h3v6M17 20V9h3v11M10 20h12"/>',
    orbit:'<ellipse cx="12" cy="12" rx="10" ry="4" transform="rotate(-30 12 12)"/><ellipse cx="12" cy="12" rx="4" ry="10" transform="rotate(-30 12 12)"/><circle cx="12" cy="12" r="1"/>',
    ruler:'<path d="m3 16 13-13 5 5L8 21l-5-5ZM13 6l2 2M10 9l2 2M7 12l2 2M4 15l2 2"/>',
    eraser:'<path d="m14 3 7 7-11 11H6l-4-4L14 3ZM8 11l7 7M10 21h12"/>',
    undo:'<path d="M9 5 3 11l6 6M3 11h12a6 6 0 0 1 0 12" transform="translate(0 -3)"/>',
    refresh:'<path d="M20 7v5h-5M4 17v-5h5M19 11a7 7 0 0 0-12-6L4 8M5 13a7 7 0 0 0 12 6l3-3"/>',
    target:'<circle cx="12" cy="12" r="7"/><path d="M12 2v5M12 17v5M2 12h5M17 12h5"/>',
    check:'<path d="m5 12 4 4L19 6"/>',
    open:'<path d="M3 7V5h6l2 2h10v13H3V7ZM7 14h10M13 10l4 4-4 4"/>',
    download:'<path d="M12 3v12M8 11l4 4 4-4M4 16v5h16v-5"/>'
  };
  const svg = name => '<svg class="focus-button-icon" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+icons[name]+'</svg>';
  const decorate = (button, name, label, onlyIcon = true) => {
    if (!button || (button.dataset.icon === name && button.querySelector('svg'))) return;
    button.dataset.icon = name;
    button.classList.toggle('focus-icon-only', onlyIcon);
    button.title = label;
    button.setAttribute('aria-label', label);
    button.innerHTML = svg(name);
    const caption = document.createElement('span');
    caption.className = 'focus-button-label';
    caption.textContent = label;
    button.append(caption);
  };
  const staticIcons = [
    ['focusStart','play','开始扫描'],['focusStop','stop','停止驱动'],
    ['focusSave','save','保存场景'],['focusNew','plus','新建场景'],
    ['focusExit','exit','完整功能界面'],['pointsMode','cloud','空间点云'],
    ['perspective','cube','三维视角'],['top','top','俯视'],['grid','grid','显示网格'],
    ['focusUndo','undo','撤销上一步',false],['focusRefreshScenes','refresh','刷新场景',false],
    ['focusApplyVoxel','check','应用体素',true]
  ];
  staticIcons.forEach(([id,name,label,onlyIcon]) => decorate(q(id),name,label,onlyIcon));
  const styleArchiveActions = () => {
    document.querySelectorAll('.focus-archive-actions').forEach(actions => {
      const buttons = [...actions.querySelectorAll('button')];
      decorate(buttons[0], 'open', '导入场景查看');
      buttons.slice(1).forEach(button => {
        if(button.querySelector('svg'))return;
        const format = button.textContent.trim();
        decorate(button,'download',format,false);
        button.title = format === 'PLY' ? '下载 PLY 点云文件（通用三维格式）' : format === 'PCD' ? '下载 PCD 点云文件（点云处理格式）' : '下载表面 PLY 模型';
        button.setAttribute('aria-label',button.title);
      });
    });
  };
  new MutationObserver(styleArchiveActions).observe(q('focusArchives'),{childList:true,subtree:true});
  styleArchiveActions();
  document.querySelectorAll('[data-focus-tool]').forEach(button => {
    const mapping = {orbit:['orbit','浏览'],measure:['ruler','测距'],erase:['eraser','删点']};
    const [name,label] = mapping[button.dataset.focusTool];
    decorate(button,name,label,false);
  });
  const fit = document.createElement('button');
  fit.id = 'focusFitView';
  decorate(fit,'target','适应视图 · F / R');
  fit.onclick = () => q('fit')?.click();
  tools.append(fit);
  // The existing status handler changes this button's text on every refresh.
  const syncPauseIcon = () => {
    const capturing = !!window.odinFocusState?.capturing;
    decorate(q('focusPause'), capturing?'pause':'play', capturing?'暂停扫描':'继续扫描');
  };
  q('focusPause').onclick = () => q(window.odinFocusState?.capturing?'pause':'start')?.click();
  new MutationObserver(syncPauseIcon).observe(q('focusPause'), {childList:true});
  syncPauseIcon();
  const colorButton = q('colorMode');
  const syncColorIcon = () => {
    if (colorButton.querySelector('svg')) return;
    const height = colorButton.textContent.includes('高度');
    decorate(colorButton,height?'height':'palette',height?'高度着色 · 点击切换 RGB':'RGB 着色 · 点击切换高度');
  };
  new MutationObserver(syncColorIcon).observe(colorButton,{childList:true});
  syncColorIcon();
  const syncPressed = button => {
    button.setAttribute('aria-pressed', String(button.classList.contains('active')));
  };
  tools.querySelectorAll('button').forEach(button => {
    syncPressed(button);
    new MutationObserver(() => syncPressed(button)).observe(button,{attributes:true,attributeFilter:['class']});
  });
  // Surface measurements and selection within the edit workspace.
  const feedback = document.createElement('small');
  feedback.setAttribute('aria-live', 'polite');
  q('focusEditPanel').querySelector('.focus-edit-tools').after(feedback);
  const syncState = () => {
    const state = window.odinFocusState;
    if (!state) return;
    feedback.textContent = q('measurement').textContent + ' · ' + q('selection').textContent;
    for (const id of ['focusCrop','focusDeleteBox','focusDownsample']) q(id).disabled = !!state.capturing;
    q('focusUndo').disabled = !!state.capturing || !state.undo;
    q('focusApplyVoxel').disabled = !!state.capturing;
    document.querySelectorAll('[data-studio-preset]').forEach(button => button.disabled = !!state.capturing);
    const voxel = q('focusMappingVoxel');
    if (!voxel.dataset.draft && state.voxel) {
      voxel.value = Number((state.voxel * 1000).toFixed(2));
      q('focusMappingVoxelRange').value = voxel.value;
      const presetValues = {detail:3,indoor:5,large:30};
      const presetNames = {detail:'细节',indoor:'室内',large:'大场景'};
      let matched = false;
      document.querySelectorAll('[data-studio-preset]').forEach(button => {
        const selected = Number(voxel.value) === presetValues[button.dataset.studioPreset];
        button.classList.toggle('active', selected);
        if(selected) { matched = true; q('focusPresetHint').textContent = presetNames[button.dataset.studioPreset]+'：'+voxel.value+' mm 体素。'; }
      });
      if(!matched)q('focusPresetHint').textContent = '自定义：'+voxel.value+' mm 体素。';
    }
  };
  ['focusMappingVoxel','focusMappingVoxelRange'].forEach(id => q(id).addEventListener('input', () => q('focusMappingVoxel').dataset.draft = 'true'));
  setInterval(() => { if (document.body.classList.contains('focus-mode')) syncState(); }, 500);
})();
