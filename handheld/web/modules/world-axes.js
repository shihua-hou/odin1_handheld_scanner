import * as THREE from 'three';

// Direction reference at the world origin; lengths adapt to camera distance.
export function createWorldAxes({camera, viewport}) {
  const colors = ['#ff7f91', '#b4ed79', '#76d1ff'];
  const overlay = document.createElement('div');
  overlay.className = 'world-axis-labels';
  overlay.setAttribute('aria-label', '世界坐标方向：X 红色、Y 绿色、Z 蓝色；参考轴长度随视距调整');
  overlay.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:2;overflow:hidden;';
  viewport.append(overlay);
  const ns = 'http://www.w3.org/2000/svg';
  const drawing = document.createElementNS(ns,'svg');
  drawing.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;overflow:visible;';
  drawing.setAttribute('aria-hidden','true');overlay.append(drawing);
  const shafts = colors.map(color => {
    const group = document.createElementNS(ns,'g');
    const halo = document.createElementNS(ns,'line');
    halo.setAttribute('stroke','#0d1521');halo.setAttribute('stroke-width','7');halo.setAttribute('stroke-opacity','.8');
    const line = document.createElementNS(ns,'line');
    line.setAttribute('stroke',color);line.setAttribute('stroke-width','3');line.setAttribute('stroke-linecap','round');
    line.style.filter = `drop-shadow(0 0 3px ${color})`;
    const tip = document.createElementNS(ns,'circle');
    tip.setAttribute('r','2.5');tip.setAttribute('fill',color);
    group.append(halo,line,tip);drawing.append(group);return {group,halo,line,tip};
  });
  const endpoints = [new THREE.Vector3(1,0,0),new THREE.Vector3(0,1,0),new THREE.Vector3(0,0,1)];
  const labels = ['X','Y','Z'].map((axis,index) => {
    const label=document.createElement('span');label.textContent=axis;
    label.style.cssText=`position:absolute;color:${colors[index]};font:700 18px/22px 'Segoe UI',sans-serif;text-shadow:0 1px 3px #07111c,0 0 5px #07111c,0 0 8px ${colors[index]}70;transform:translate(-50%,-50%);user-select:none;`;
    overlay.append(label);return label;
  });
  const origin = new THREE.Vector3(), projected = new THREE.Vector3(), cameraPoint = new THREE.Vector3();
  function update() {
    camera.updateMatrixWorld();
    const width=viewport.clientWidth,height=viewport.clientHeight;
    origin.set(0,0,0).project(camera);
    const length=Math.max(1,camera.position.length()*2*Math.tan(THREE.MathUtils.degToRad(camera.fov/2))/Math.max(height,1)*64);
    const ox=(origin.x+1)*width/2,oy=(1-origin.y)*height/2;
    endpoints.forEach((endpoint,index)=>{
      projected.copy(endpoint).multiplyScalar(length);
      cameraPoint.copy(projected).applyMatrix4(camera.matrixWorldInverse);projected.project(camera);
      const visible=cameraPoint.z<0&&origin.z>=-1&&origin.z<=1&&projected.z>=-1&&projected.z<=1;
      const shaft=shafts[index],label=labels[index];
      shaft.group.style.display=visible?'':'none';label.hidden=!visible;
      const x=(projected.x+1)*width/2,y=(1-projected.y)*height/2;
      for(const line of [shaft.halo,shaft.line]){
        line.setAttribute('x1',String(ox));line.setAttribute('y1',String(oy));line.setAttribute('x2',String(x));line.setAttribute('y2',String(y));
      }
      shaft.tip.setAttribute('cx',String(x));shaft.tip.setAttribute('cy',String(y));
      const dx=x-ox,dy=y-oy,size=Math.hypot(dx,dy);
      label.style.left=(x+(size>1?dx/size*15:0))+'px';label.style.top=(y+(size>1?dy/size*15:-16))+'px';
    });
  }
  return {update};
}
