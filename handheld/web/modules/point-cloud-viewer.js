export function createPointCloudViewer({spatial, camera, controls, renderer, setDirty}) {
  return {
    fit(){ spatial.fit(); setDirty?.(); },
    setColorMode(mode){ spatial.setColorMode(mode); setDirty?.(); },
    setPointSize(size){ spatial.material.size=Number(size); spatial.dirty=true; setDirty?.(); },
    get spatial(){ return spatial; },
    get group(){ return spatial.group; }
  };
}
