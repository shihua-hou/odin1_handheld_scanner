"""Spatial point pages on SQLite/WAL; no whole-map arrays or voxel hash in RAM."""
import io
import json
import sqlite3
import struct
import time
import uuid
import zipfile
import weakref
import numpy as np
from .core import Scene, DEFAULT_VOXEL, MIN_VOXEL, MAX_VOXEL, atomic_json

RECORD=np.dtype([('xyz','<f4',(3,)),('rgb','u1',(4,))])
PAGE_POINTS=32768
BLOCK_METRES=2.
LOD_SPACING=(0.,.002,.008,.032,.128,.512,2.048)
COLUMNS='id,bx,by,bz,n,version,x0,y0,z0,x1,y1,z1,data'


class DiskArray:
    def __init__(self,scene,field):self.scene,self.field=weakref.proxy(scene),field
    def __len__(self):return self.scene.count
    def copy(self):return self.scene.snapshot()[0 if self.field=='xyz' else 1]
    def __array__(self,dtype=None):return np.asarray(self.copy(),dtype=dtype)


class DiskScene(Scene):
    def __del__(self):
        if hasattr(self,'db'):
            try:self.db.close()
            except sqlite3.Error:pass

    def __init__(self,directory,voxel=DEFAULT_VOXEL,limit=None):
        super().__init__(directory,voxel,limit)
        self.path=self.directory.parent/'runtime'/'point-pages.sqlite'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA cache_size=-16384')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY AUTOINCREMENT,bx INTEGER,by INTEGER,bz INTEGER,
          n INTEGER,version INTEGER,x0 REAL,y0 REAL,z0 REAL,x1 REAL,y1 REAL,z1 REAL,data BLOB);
        CREATE INDEX IF NOT EXISTS spatial_bucket ON chunks(bx,by,bz);
        CREATE TABLE IF NOT EXISTS voxels(kx INTEGER,ky INTEGER,kz INTEGER,owner INTEGER,token INTEGER,ordinal INTEGER,
          PRIMARY KEY(kx,ky,kz)) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS voxel_owner ON voxels(owner);
        CREATE TABLE IF NOT EXISTS lod(id INTEGER,version INTEGER,level INTEGER,n INTEGER,data BLOB,
          PRIMARY KEY(id,level));
        CREATE TABLE IF NOT EXISTS block_lod(key TEXT,version INTEGER,level INTEGER,data BLOB,PRIMARY KEY(key,level));
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,voxel REAL);
        CREATE TABLE IF NOT EXISTS history_chunks AS SELECT 0 AS entry,* FROM chunks WHERE 0;
        ''')
        self.count=self.db.execute('SELECT COALESCE(SUM(n),0) FROM chunks').fetchone()[0]
        saved=self.db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
        if saved:
            meta=json.loads(saved[0])
            for key in ('voxel','revision','topology','session'):setattr(self,key,meta[key])
        self.points=DiskArray(self,'xyz');self.colors=DiskArray(self,'rgb')
        self.recovered=bool(saved);self.loaded=bool(self.count)
        trajectory=self.db.execute("SELECT value FROM metadata WHERE key='trajectory'").fetchone()
        if trajectory:self.trajectory=json.loads(trajectory[0])
        self.history=[r[0] for r in self.db.execute('SELECT id FROM history ORDER BY id')]
        self._serial=max([time.time_ns(),*self.history])
        self._persist();self.db.commit()

    def _persist(self):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES('state',?)",(json.dumps(
            {k:getattr(self,k) for k in ('voxel','revision','topology','session')}),))

    def _rows(self):
        # Fetch one bounded page per iteration; cursor never materialises all blobs.
        cursor=self.db.execute('SELECT id,data FROM chunks ORDER BY id')
        for ident,data in cursor:yield ident,np.frombuffer(data,RECORD)

    def _write(self,ident,records):
        self.db.execute('DELETE FROM lod WHERE id=?',(ident,))
        if not len(records):
            self.db.execute('DELETE FROM chunks WHERE id=?',(ident,));return
        lo=records['xyz'].min(axis=0);hi=records['xyz'].max(axis=0)
        self.db.execute('UPDATE chunks SET n=?,version=version+1,x0=?,y0=?,z0=?,x1=?,y1=?,z1=?,data=? WHERE id=?',
            (len(records),*map(float,lo),*map(float,hi),records.tobytes(),ident))

    def _keys(self,ident,records,voxel=None):
        cells=np.floor(records['xyz']/(voxel or self.voxel)).astype(np.int64)
        token=uuid.uuid4().int & ((1<<63)-1)
        self.db.executemany('INSERT OR IGNORE INTO voxels VALUES(?,?,?,?,?,?)',
            ((int(x),int(y),int(z),ident,token,i) for i,(x,y,z) in enumerate(cells)))
        accepted=[r[0] for r in self.db.execute('SELECT ordinal FROM voxels WHERE owner=? AND token=? ORDER BY ordinal',(ident,token))]
        return records[accepted]

    def _append(self,xyz,rgb,deduplicate=True):
        buckets=np.floor(xyz/BLOCK_METRES).astype(np.int64)
        keys,group=np.unique(buckets,axis=0,return_inverse=True)
        order=np.argsort(group,kind='stable');ends=np.searchsorted(group[order],np.arange(len(keys)+1))
        added=0
        for i,key in enumerate(keys):
            indices=order[ends[i]:ends[i+1]]
            for start in range(0,len(indices),PAGE_POINTS):
                selected=indices[start:start+PAGE_POINTS]
                page=self.db.execute('SELECT id,data FROM chunks WHERE bx=? AND by=? AND bz=? AND n+?<=? ORDER BY id DESC LIMIT 1',
                    (*map(int,key),len(selected),PAGE_POINTS)).fetchone()
                if page:ident,raw=page;existing=np.frombuffer(raw,RECORD)
                else:
                    ident=self.db.execute('INSERT INTO chunks(bx,by,bz,n,version) VALUES(?,?,?,0,0)',tuple(map(int,key))).lastrowid
                    existing=np.empty(0,RECORD)
                candidate=np.empty(len(selected),RECORD);candidate['xyz']=xyz[selected]
                candidate['rgb'][:,:3]=rgb[selected];candidate['rgb'][:,3]=255
                accepted=self._keys(ident,candidate) if deduplicate else candidate
                if len(accepted):self._write(ident,np.concatenate([existing,accepted]));added+=len(accepted)
                elif not len(existing):self.db.execute('DELETE FROM chunks WHERE id=?',(ident,))
        self.count+=added
        return added

    def import_records(self,xyz,rgb):
        """Lossless import of a paused, already deduplicated legacy scene.

        Imported maps are edit-only; new capture requires new(), which starts
        a fresh voxel index. Keep every original record during migration.
        """
        with self.lock,self.db:
            self._append(xyz,rgb,deduplicate=False);self.revision+=1;self.loaded=True;self._persist()

    def ingest(self,xyz,colors,frame,stamp=None):
        with self.lock:
            self.received_frames+=1;self.last_cloud=time.time()
            if not self.capturing:return
            if frame!='odom':self.error=f'Rejected frame {frame!r}: expected official cloud_slam in odom';return
            if stamp is not None and self.last_stamp is not None and stamp<=self.last_stamp:
                self.capturing=False;self.loaded=True;self.error='Device timestamp reset/out of order. Save scene and start a new session.';return
            good=np.isfinite(xyz).all(axis=1)&(np.abs(xyz)<10000).all(axis=1)
            with self.db:
                if self._append(xyz[good],colors[good]):self.revision+=1
                self._persist()
            self.last_stamp=stamp;self.frame=frame;self.frames+=1

    def status(self):
        with self.lock:
            status=super().status()
            status.update(storage='spatial-disk',chunks=self.db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0])
            return status

    def manifest(self):
        with self.lock:
            rows=self.db.execute('SELECT id,n,version,x0,y0,z0,x1,y1,z1,bx,by,bz FROM chunks ORDER BY id').fetchall()
            blocks={}
            for r in rows:
                key=','.join(map(str,r[9:12]))
                page=dict(id=r[0],count=r[1],version=r[2],min=r[3:6],max=r[6:9])
                if key not in blocks:blocks[key]=dict(id='b:'+key,count=0,version=0,min=list(r[3:6]),max=list(r[6:9]),pages=[])
                block=blocks[key];block['count']+=r[1];block['version']+=r[2];block['pages'].append(page)
                block['min']=[min(a,b) for a,b in zip(block['min'],r[3:6])]
                block['max']=[max(a,b) for a,b in zip(block['max'],r[6:9])]
            return dict(session=self.session,generation=self.topology,revision=self.revision,total=self.count,
                spacing=LOD_SPACING,voxel=self.voxel,tiles=list(blocks.values()))

    def block_tile(self,key,level,generation,version,relaxed=False):
        bucket=tuple(map(int,key.split(',')))
        if len(bucket)!=3 or not 1<=level<len(LOD_SPACING):raise ValueError('Invalid spatial block')
        with self.lock:
            if generation!=self.topology:raise ValueError('Scene changed')
            rows=self.db.execute('SELECT id,version FROM chunks WHERE bx=? AND by=? AND bz=? ORDER BY id',bucket).fetchall()
            actual=sum(r[1] for r in rows)
            if not rows or (not relaxed and actual!=version):raise ValueError('Block changed')
            version=actual
            cached=self.db.execute('SELECT data FROM block_lod WHERE key=? AND version=? AND level=?',(key,version,level)).fetchone()
            if cached:return (cached[0],version) if relaxed else cached[0]
        spacing=LOD_SPACING[level];origin=np.floor(np.array(bucket)*BLOCK_METRES/spacing).astype(np.int64)-1
        dimension=int(np.ceil(BLOCK_METRES/spacing))+3
        seen=set();parts=[]
        for ident,_ in rows:
            with self.lock:
                if generation!=self.topology:raise ValueError('Scene changed')
                cached=self.db.execute('SELECT l.data FROM lod l JOIN chunks c ON l.id=c.id AND l.version=c.version WHERE l.id=? AND l.level=?',(ident,level)).fetchone()
                record=None if cached else self.db.execute('SELECT version,data FROM chunks WHERE id=?',(ident,)).fetchone()
                if not cached and not record:raise ValueError('Block changed')
            records=np.frombuffer(cached[0] if cached else record[1],RECORD)
            cells=np.floor(records['xyz'].astype(np.float64)/spacing).astype(np.int64)-origin
            hashes=cells[:,0]+dimension*(cells[:,1]+dimension*cells[:,2])
            if not cached:
                hashes,indices=np.unique(hashes,return_index=True)
                records=records[indices]
                with self.lock:
                    current=self.db.execute('SELECT version FROM chunks WHERE id=?',(ident,)).fetchone()
                    if current and current[0]==record[0]:
                        with self.db:self.db.execute('INSERT OR REPLACE INTO lod VALUES(?,?,?,?,?)',(ident,record[0],level,len(records),records.tobytes()))
            selected=[]
            for index,code in enumerate(hashes):
                value=int(code)
                if value not in seen:seen.add(value);selected.append(index)
            if selected:parts.append(records[selected].tobytes())
        raw=b''.join(parts)
        with self.lock:
            current=self.db.execute('SELECT COALESCE(SUM(version),0) FROM chunks WHERE bx=? AND by=? AND bz=?',bucket).fetchone()[0]
            if generation!=self.topology or (not relaxed and current!=version):raise ValueError('Block changed')
            if current==version:
                with self.db:self.db.execute('INSERT OR REPLACE INTO block_lod VALUES(?,?,?,?)',(key,version,level,raw))
        return (raw,version) if relaxed else raw

    def tile(self,ident,level,generation,version,relaxed=False):
        with self.lock:
            if generation!=self.topology:raise ValueError('Scene changed; refresh manifest')
            row=self.db.execute('SELECT version,data FROM chunks WHERE id=?',(ident,)).fetchone()
            if not row or (not relaxed and version!=row[0]):raise ValueError('Tile changed; refresh manifest')
            version=row[0]
            if not 0<=level<len(LOD_SPACING):raise ValueError('Invalid detail level')
            if level:
                cached=self.db.execute('SELECT data FROM lod WHERE id=? AND version=? AND level=?',(ident,version,level)).fetchone()
                if cached:return (cached[0],version) if relaxed else cached[0]
                records=np.frombuffer(row[1],RECORD)
                _,indices=np.unique(np.floor(records['xyz'].astype(np.float64)/LOD_SPACING[level]).astype(np.int64),axis=0,return_index=True)
                raw=records[indices].tobytes()
                with self.db:self.db.execute('INSERT OR REPLACE INTO lod VALUES(?,?,?,?,?)',(ident,version,level,len(indices),raw))
                return (raw,version) if relaxed else raw
            return (row[1],version) if relaxed else row[1]

    def snapshot(self):
        with self.lock:
            arrays=[records.copy() for _,records in self._rows()]
            data=np.concatenate(arrays) if arrays else np.empty(0,RECORD)
            return data['xyz'].copy(),data['rgb'][:,:3].copy()

    def point_packet(self,offset,maximum=50000):
        with self.lock:
            blocks=[];skip=offset;remaining=maximum
            for ident,n in self.db.execute('SELECT id,n FROM chunks ORDER BY id'):
                if skip>=n:skip-=n;continue
                raw=self.db.execute('SELECT data FROM chunks WHERE id=?',(ident,)).fetchone()[0]
                take=min(n-skip,remaining);blocks.append(raw[skip*16:(skip+take)*16]);remaining-=take;skip=0
                if not remaining:break
            count=maximum-remaining
            return np.array([0x4F444E32,self.topology,self.revision,offset,count,self.count],'<u4').tobytes()+b''.join(blocks)

    def _remember(self,entry,ident):
        self.db.execute('INSERT INTO history_chunks SELECT ?,* FROM chunks WHERE id=?',(entry,ident))

    def _replace(self,ident,records):
        self.db.execute('DELETE FROM voxels WHERE owner=?',(ident,))
        records=self._keys(ident,records)
        self._write(ident,records)

    def _finish_edit(self):
        self.count=self.db.execute('SELECT COALESCE(SUM(n),0) FROM chunks').fetchone()[0]
        self.db.execute('DELETE FROM block_lod')
        self.revision+=1;self.topology+=1;self._persist()

    def edit(self,action,data):
        with self.lock,self.db:
            if self.capturing:raise ValueError('Pause capture before editing')
            if action=='undo':
                if not self.history:raise ValueError('No undo history')
                entry=self.history.pop()
                for row in self.db.execute(f'SELECT {COLUMNS} FROM history_chunks WHERE entry=?',(entry,)):
                    ident=row[0]
                    self.db.execute('DELETE FROM voxels WHERE owner=?',(ident,))
                    self.db.execute('DELETE FROM chunks WHERE id=?',(ident,))
                    self.db.execute(f'INSERT INTO chunks({COLUMNS}) VALUES({",".join("?"*13)})',row)
                    self._replace(ident,np.frombuffer(row[-1],RECORD))
                self.db.execute('DELETE FROM history_chunks WHERE entry=?',(entry,));self.db.execute('DELETE FROM history WHERE id=?',(entry,))
                self._finish_edit();return
            if not self.count:raise ValueError('Scene is empty')
            if action not in ('crop','delete_box','delete_sphere','downsample'):raise ValueError('Unknown edit operation')
            if action=='delete_sphere':
                center=np.asarray(data['center'],float);radius=float(data['radius'])
                if center.shape!=(3,) or not np.isfinite(center).all() or not 0<radius<=100:raise ValueError('Invalid sphere')
                low,high=center-radius,center+radius
            else:
                low=np.asarray(data.get('min',[-10000]*3),float);high=np.asarray(data.get('max',[10000]*3),float)
                if low.shape!=(3,) or high.shape!=(3,) or not np.isfinite([low,high]).all() or (low>high).any():raise ValueError('Invalid XYZ bounds')
            if action=='downsample':
                size=float(data['voxel'])
                if not .001<=size<=2:raise ValueError('Voxel size must be 0.001..2 metres')
                self.db.execute('DROP TABLE IF EXISTS temp.sample_keys')
                self.db.execute('CREATE TEMP TABLE sample_keys(x INTEGER,y INTEGER,z INTEGER,owner INTEGER,ordinal INTEGER,PRIMARY KEY(x,y,z)) WITHOUT ROWID')
                self.db.execute('CREATE INDEX temp.sample_owner ON sample_keys(owner)')
            self._serial+=1;entry=self._serial;self.db.execute('INSERT INTO history VALUES(?,?)',(entry,self.voxel))
            metadata=self.db.execute('SELECT id,x0,y0,z0,x1,y1,z1 FROM chunks ORDER BY id').fetchall()
            for row in metadata:
                ident=row[0];lo=np.array(row[1:4]);hi=np.array(row[4:7])
                intersects=(hi>=low).all() and (lo<=high).all()
                if not intersects:
                    if action=='crop':
                        self._remember(entry,ident);self._replace(ident,np.empty(0,RECORD))
                    continue
                records=np.frombuffer(self.db.execute('SELECT data FROM chunks WHERE id=?',(ident,)).fetchone()[0],RECORD)
                xyz=records['xyz'];inside=((xyz>=low)&(xyz<=high)).all(axis=1)
                if action=='delete_sphere':keep=np.sum((xyz-center)**2,axis=1)>radius**2
                elif action=='crop':keep=inside
                elif action=='delete_box':keep=~inside
                else:
                    selected=np.flatnonzero(inside);cells=np.floor(xyz[selected]/size).astype(np.int64)
                    self.db.executemany('INSERT OR IGNORE INTO sample_keys VALUES(?,?,?,?,?)',
                        ((int(x),int(y),int(z),ident,int(i)) for i,(x,y,z) in zip(selected,cells)))
                    accepted=[r[0] for r in self.db.execute('SELECT ordinal FROM sample_keys WHERE owner=?',(ident,))]
                    keep=~inside;keep[accepted]=True
                if not keep.all():self._remember(entry,ident);self._replace(ident,records[keep])
            self.history.append(entry)
            while len(self.history)>3:
                oldest=self.history.pop(0);self.db.execute('DELETE FROM history_chunks WHERE entry=?',(oldest,));self.db.execute('DELETE FROM history WHERE id=?',(oldest,))
            self._finish_edit()

    def set_voxel(self,value):
        value=float(value)
        if not np.isfinite(value) or not MIN_VOXEL<=value<=MAX_VOXEL:raise ValueError('建图体素范围为 1 mm 到 1 m')
        with self.lock:
            if self.capturing:raise ValueError('请先暂停采集，再修改建图体素')
            if value==self.voxel:return
            if self.count:self.save('autosave-before-voxel-change')
            with self.db:
                self.voxel=value;self.db.execute('DELETE FROM voxels')
                for ident,records in self._rows():self._write(ident,self._keys(ident,records))
                self.history.clear();self.db.execute('DELETE FROM history');self.db.execute('DELETE FROM history_chunks');self._finish_edit()

    def _export(self,dest,source,n):
        def rows():
            for ident,data in source.execute('SELECT id,data FROM chunks ORDER BY id'):
                yield ident,np.frombuffer(data,RECORD)
        header=('ply\nformat binary_little_endian 1.0\n'+f'element vertex {n}\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n')
        pcd=('# .PCD v0.7\nVERSION 0.7\nFIELDS x y z rgb\nSIZE 4 4 4 4\nTYPE F F F U\nCOUNT 1 1 1 1\n'+f'WIDTH {n}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS {n}\nDATA binary\n')
        with open(dest/'scene.ply','wb') as ply,open(dest/'scene.pcd','wb') as cloud:
            ply.write(header.encode());cloud.write(pcd.encode())
            for _,records in rows():
                compact=np.empty(len(records),dtype=[('xyz','<f4',(3,)),('rgb','u1',(3,))]);compact['xyz']=records['xyz'];compact['rgb']=records['rgb'][:,:3];ply.write(compact.tobytes())
                packed=np.empty(len(records),dtype=[('xyz','<f4',(3,)),('rgb','<u4')]);packed['xyz']=records['xyz'];c=records['rgb'].astype(np.uint32)
                packed['rgb']=(c[:,0]<<16)|(c[:,1]<<8)|c[:,2];cloud.write(packed.tobytes())
        with zipfile.ZipFile(dest/'scene.npz','w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as archive:
            for name,dtype in [('points','<f4'),('colors','|u1')]:
                with archive.open(name+'.npy','w',force_zip64=True) as out:
                    np.lib.format.write_array_header_1_0(out,dict(descr=dtype,fortran_order=False,shape=(n,3)))
                    for _,records in rows():out.write((records['xyz'] if name=='points' else records['rgb'][:,:3]).tobytes())

    def save(self,name='scan'):
        with self.lock:
            if not self.count:raise ValueError('Cannot save empty scene')
            safe=''.join(c for c in str(name) if c.isalnum() or c in '-_')[:48] or 'scan'
            ident=time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'-'+safe
            dest=self.directory/ident;dest.mkdir()
            meta=self.status();meta.update(id=ident,name=str(name)[:100],saved_at=time.time(),trajectory=self.trajectory.copy())
            self.db.commit()
            target=sqlite3.connect(dest/'pages.sqlite')
            try:self.db.backup(target)
            finally:target.close()
        source=sqlite3.connect(dest/'pages.sqlite')
        try:self._export(dest,source,meta['points'])
        finally:source.close()
        atomic_json(dest/'metadata.json',meta)
        return meta

    def checkpoint(self):
        with self.lock:
            self._persist()
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES('trajectory',?)",(json.dumps(self.trajectory),))
            self.db.commit();self.db.execute('PRAGMA wal_checkpoint(PASSIVE)')

    def _clear(self):
        for table in ('voxels','chunks','lod','block_lod','history','history_chunks'):self.db.execute('DELETE FROM '+table)
        self.db.execute("DELETE FROM metadata WHERE key='trajectory'")
        self.count=0;self.history.clear()

    def load(self,scene_id):
        with self.lock:
            if self.capturing:raise ValueError('Pause capture before loading')
            path=self.archive_path(scene_id)
            if self.count:self.save('autosave-before-load')
            meta=json.loads((path/'metadata.json').read_text(encoding='utf8'));self.voxel=float(meta.get('voxel',DEFAULT_VOXEL))
            self.db.commit()
            if (path/'pages.sqlite').is_file():
                source=sqlite3.connect(path/'pages.sqlite')
                try:source.backup(self.db)
                finally:source.close()
                self.db.execute('CREATE TABLE IF NOT EXISTS block_lod(key TEXT,version INTEGER,level INTEGER,data BLOB,PRIMARY KEY(key,level))')
                self.db.commit()
                self.count=self.db.execute('SELECT COALESCE(SUM(n),0) FROM chunks').fetchone()[0]
            else:
                with self.db:
                    self._clear()
                    with zipfile.ZipFile(path/'scene.npz') as archive,archive.open('points.npy') as points,archive.open('colors.npy') as colors:
                        def header(stream):
                            version=np.lib.format.read_magic(stream)
                            return np.lib.format._read_array_header(stream,version)
                        shape,order,dt=header(points);rgb_shape,rgb_order,rgb_dt=header(colors)
                        if order or rgb_order or shape!=rgb_shape or len(shape)!=2 or shape[1]!=3:raise ValueError('Unsupported legacy array layout')
                        for start in range(0,shape[0],PAGE_POINTS):
                            n=min(PAGE_POINTS,shape[0]-start)
                            xyz=np.frombuffer(points.read(n*3*dt.itemsize),dt).reshape(n,3).astype(np.float32)
                            rgb=np.frombuffer(colors.read(n*3*rgb_dt.itemsize),rgb_dt).reshape(n,3).astype(np.uint8)
                            self._append(xyz,rgb,deduplicate=False)
            with self.db:
                self.history.clear();self.db.execute('DELETE FROM history');self.db.execute('DELETE FROM history_chunks')
                self.session=uuid.uuid4().hex[:12];self.topology+=1;self.revision+=1;self._persist()
            self.trajectory=meta.get('trajectory',[]);self.error='';self.loaded=True;self.last_stamp=None;self.frames=0;self.full=False

    def new(self):
        with self.lock:
            if self.capturing:raise ValueError('Pause capture before creating a scene')
            if self.count:self.save('autosave-before-new')
            with self.db:
                self._clear();self.trajectory.clear();self.session=uuid.uuid4().hex[:12];self.revision+=1;self.topology+=1;self._persist()
            self.frames=0;self.full=False;self.error='';self.last_stamp=None;self.loaded=False
