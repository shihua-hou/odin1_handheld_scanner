import * as THREE from 'three';

// Visibility and projected spacing determine detail. There is no point budget.
export class SpatialCloud {
  constructor({scene,camera,renderer,controls,onChange}) {
    Object.assign(this,{camera,renderer,controls,onChange});
    this.group=new THREE.Group();scene.add(this.group);
    this.tiles=new Map();this.pages=new Map();this.resident=new Map();this.active=new Map();
    this.queue=[];this.visible=new Set();this.manifest=null;this.epoch=0;
    this.moving=false;this.dirty=true;this.viewDirty=true;this.lastView=0;
    this.lastPoll=0;this.polling=false;this.renderedFrames=0;this.bounds=new THREE.Box3();
    this.uniforms={uHeightMode:{value:0},uLow:{value:0},uHigh:{value:1}};
    this.material=new THREE.PointsMaterial({size:2,sizeAttenuation:false,vertexColors:true});
    this.material.onBeforeCompile=shader=>{
      Object.assign(shader.uniforms,this.uniforms);
      shader.vertexShader='uniform float uHeightMode;\nuniform float uLow;\nuniform float uHigh;\n'+shader.vertexShader;
      shader.vertexShader=shader.vertexShader.replace('#include <color_vertex>',`#include <color_vertex>
        if(uHeightMode>0.5){float t=clamp((position.z-uLow)/max(uHigh-uLow,0.01),0.0,1.0);
          vColor=mix(vec3(0.15,0.35,0.95),vec3(0.95,0.8,0.15),t);}`);
    };
    controls.addEventListener('start',()=>{clearTimeout(this.settle);this.moving=true;this.viewDirty=true;});
    controls.addEventListener('end',()=>{clearTimeout(this.settle);this.settle=setTimeout(()=>{this.moving=false;this.viewDirty=true;},350);});
    controls.addEventListener('change',()=>{this.dirty=true;this.viewDirty=true;});
  }

  async poll() {
    if(this.polling)return;
    this.polling=true;
    try {
      const response=await fetch('/api/tiles',{cache:'no-store'});
      if(!response.ok)throw Error('空间索引暂不可用');
      const manifest=await response.json();
      const key=manifest.session+':'+manifest.generation;
      if(key!==this.key){
        const changedSession=this.manifest?.session!==manifest.session;
        this.epoch++;this.clear();this.key=key;
        if(changedSession)this.autoFit=true;
      }
      this.manifest=manifest;
      this.tiles.clear();this.pages.clear();this.bounds.makeEmpty();
      for(const item of manifest.tiles){
        item.box=new THREE.Box3(new THREE.Vector3(...item.min),new THREE.Vector3(...item.max));
        item.sphere=item.box.getBoundingSphere(new THREE.Sphere());
        for(const page of item.pages){
          page.sourceId=page.id;page.id='p:'+page.id;
          page.box=new THREE.Box3(new THREE.Vector3(...page.min),new THREE.Vector3(...page.max));
          page.sphere=page.box.getBoundingSphere(new THREE.Sphere());this.pages.set(page.id,page);
        }
        this.tiles.set(item.id,item);this.bounds.union(item.box);
      }
      if(!this.bounds.isEmpty()){
        this.uniforms.uLow.value=this.bounds.min.z;this.uniforms.uHigh.value=this.bounds.max.z;
        if(this.autoFit){this.autoFit=false;this.fit();}
      }
      for(const id of this.resident.keys())if(!this.tiles.has(id)&&!this.pages.has(id))this.evict(id);
      this.viewDirty=true;this.notify();
    }catch(error){this.lastError=error.message;}
    finally{this.polling=false;}
  }

  clear(){
    for(const request of this.active.values())request.abort();
    this.active.clear();this.queue=[];
    for(const id of this.resident.keys())this.evict(id);
    this.visible.clear();this.tiles.clear();this.pages.clear();
  }

  evict(id){const mesh=this.resident.get(id);if(mesh){this.group.remove(mesh);if(mesh.userData.index)mesh.geometry.setIndex(mesh.userData.index);mesh.geometry.dispose();this.resident.delete(id);this.dirty=true;}}

  tick(now){
    if(now-this.lastPoll>1000){this.lastPoll=now;void this.poll();}
    if(this.viewDirty&&now-this.lastView>150){this.viewDirty=false;this.lastView=now;this.select();}
  }

  select(){
    if(!this.manifest)return;
    this.camera.updateMatrixWorld();
    const matrix=new THREE.Matrix4().multiplyMatrices(this.camera.projectionMatrix,this.camera.matrixWorldInverse);
    const frustum=new THREE.Frustum().setFromProjectionMatrix(matrix);
    const height=Math.max(1,this.renderer.domElement.clientHeight);
    const projection=2*Math.tan(THREE.MathUtils.degToRad(this.camera.fov/2))/height;
    const visible=new Set(),queue=[];
    for(const item of this.tiles.values()){
      if(!frustum.intersectsBox(item.box))continue;
      const distance=Math.max(this.camera.near,this.camera.position.distanceTo(item.sphere.center));
      const spacing=distance*projection*(this.moving?6:2);
      let level=0;
      for(let i=1;i<this.manifest.spacing.length;i++)if(this.manifest.spacing[i]<=spacing)level=i;
      if(level===0){
        let complete=true;
        for(const page of item.pages){
          if(!frustum.intersectsBox(page.box))continue;
          visible.add(page.id);const mesh=this.resident.get(page.id);
          if(mesh)this.decimate(mesh,0);
          if(!mesh||mesh.userData.version<page.version){
            complete=false;queue.push({item:page,level:0,priority:5e5+distance});
          }
        }
        if(!complete){
          visible.add(item.id);
          if(!this.resident.has(item.id))queue.push({item,level:2,priority:distance});
        }
      }else{
        visible.add(item.id);const mesh=this.resident.get(item.id);
        if(mesh)this.decimate(mesh,level);
        const target=mesh?level:Math.min(level+1,this.manifest.spacing.length-1);
        if(!mesh||mesh.userData.version<item.version||mesh.userData.level!==level){
          queue.push({item,level:target,priority:(mesh?1e6:0)+distance});
        }
      }
    }
    this.visible=visible;
    for(const id of this.resident.keys())if(!visible.has(id))this.evict(id);
    for(const [id,controller] of this.active)if(!visible.has(id))controller.abort();
    this.queue=queue.sort((a,b)=>a.priority-b.priority);this.notify();this.pump();
  }

  pump(){
    // Bound concurrent network requests, never the number of displayed points.
    while(this.active.size<2&&this.queue.length){
      const job=this.queue.shift();const id=job.item.id;
      if(this.active.has(id)||!this.visible.has(id))continue;
      const controller=new AbortController();this.active.set(id,controller);
      const epoch=this.epoch,generation=this.manifest.generation;
      const path=id.startsWith('b:')?'blocks/'+encodeURIComponent(id.slice(2)):'tiles/'+job.item.sourceId;
      const url=`/api/${path}?level=${job.level}&generation=${generation}&version=${job.item.version}`;
      void fetch(url,{signal:controller.signal}).then(async response=>{
        if(!response.ok){this.viewDirty=true;return;}
        const buffer=await response.arrayBuffer();
        if(epoch!==this.epoch||!this.visible.has(id))return;
        if(buffer.byteLength%16)throw Error('无效点云块');
        const n=buffer.byteLength/16,position=new Float32Array(n*3),color=new Uint8Array(n*3);
        const data=new DataView(buffer);
        for(let i=0;i<n;i++){
          const b=i*16,a=i*3;
          position[a]=data.getFloat32(b,true);position[a+1]=data.getFloat32(b+4,true);position[a+2]=data.getFloat32(b+8,true);
          color[a]=data.getUint8(b+12);color[a+1]=data.getUint8(b+13);color[a+2]=data.getUint8(b+14);
        }
        const geometry=new THREE.BufferGeometry();
        geometry.setAttribute('position',new THREE.BufferAttribute(position,3));
        geometry.setAttribute('color',new THREE.BufferAttribute(color,3,true));
        geometry.boundingBox=job.item.box.clone();geometry.boundingSphere=job.item.sphere.clone();
        const actualVersion=Number(response.headers.get('X-Point-Version'))||job.item.version;
        const mesh=new THREE.Points(geometry,this.material);mesh.userData={version:actualVersion,level:job.level};
        this.evict(id);this.resident.set(id,mesh);this.group.add(mesh);
        this.dirty=true;this.viewDirty=true;this.notify();
      }).catch(error=>{if(error.name!=='AbortError')this.lastError=error.message;}).finally(()=>{
        if(this.active.get(id)===controller)this.active.delete(id);
        this.pump();
      });
    }
  }

  notify(){
    let points=0,bytes=0;
    for(const mesh of this.resident.values()){
      points+=Math.min(mesh.geometry.index?.count??mesh.geometry.attributes.position.count,mesh.geometry.drawRange.count);
      bytes+=mesh.geometry.attributes.position.array.byteLength+mesh.geometry.attributes.color.array.byteLength+(mesh.userData.index?.array.byteLength||0);
    }
    this.metrics={visiblePoints:points,totalPoints:this.manifest?.total||0,residentTiles:this.resident.size,
      visibleTiles:this.visible.size,totalTiles:this.tiles.size,totalPages:this.pages.size,geometryBytes:bytes,moving:this.moving,
      renderedFrames:this.renderedFrames,levels:[...this.resident.values()].map(mesh=>mesh.userData.level),error:this.lastError||''};
    this.onChange?.(this.metrics);
  }

  decimate(mesh,level){
    const current=this.manifest.spacing[mesh.userData.level]||this.manifest.voxel;
    const wanted=this.manifest.spacing[level]||this.manifest.voxel;
    const stride=Math.max(1,Math.floor((wanted/current)**2));
    if((mesh.userData.stride||1)===stride)return;
    const n=mesh.geometry.attributes.position.count;
    if(stride===1){mesh.geometry.setIndex(null);mesh.geometry.setDrawRange(0,n);}
    else{
      const attribute=mesh.userData.index||(mesh.userData.index=new THREE.BufferAttribute(new Uint32Array(n),1).setUsage(THREE.DynamicDrawUsage));
      const count=Math.ceil(n/stride);
      for(let i=0;i<count;i++)attribute.array[i]=i*stride;
      attribute.clearUpdateRanges();attribute.addUpdateRange(0,count);attribute.needsUpdate=true;
      mesh.geometry.setIndex(attribute);mesh.geometry.setDrawRange(0,count);
    }
    mesh.userData.stride=stride;this.dirty=true;
  }

  fit(){
    if(this.bounds.isEmpty())return;
    const center=this.bounds.getCenter(new THREE.Vector3()),size=this.bounds.getSize(new THREE.Vector3()).length();
    this.controls.target.copy(center);
    this.camera.position.copy(center).add(new THREE.Vector3(.7,-1,.7).normalize().multiplyScalar(Math.max(size*1.1,2)));
    this.camera.near=Math.max(.01,size/10000);this.camera.far=Math.max(1000,size*10);this.camera.updateProjectionMatrix();
    this.controls.update();this.dirty=true;this.viewDirty=true;
  }

  setColorMode(mode){this.uniforms.uHeightMode.value=mode==='rgb'?0:1;this.dirty=true;}
  pick(raycaster){return raycaster.intersectObjects([...this.resident.values()],false)[0];}
}
