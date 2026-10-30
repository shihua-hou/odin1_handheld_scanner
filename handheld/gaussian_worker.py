"""Real OpenSplat training, with atomic checkpoints and auditable input data."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import traceback
from .core import atomic_json
from .photogrammetry import export_dataset,ply_to_splat


def main(directory):
    directory=Path(directory).resolve();root=directory.parents[1]
    job=json.loads((directory/'job.json').read_text())
    process=None
    def update(**changes):
        job.update(changes,updated_at=time.time());atomic_json(directory/'job.json',job)
    def terminate(signum,frame):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,terminate)
    try:
        dataset=root/'datasets'/job.get('dataset',job['id'])
        update(state='exporting',message='正在同步原始图像与相机位姿')
        if not (dataset/'transforms.json').is_file():
            quality=export_dataset(root/'recordings'/job['recording'],dataset,
                root/'runtime/calibration/calib.yaml',width=job['width'],seed_points=job['seed_points'],
                progress=lambda message,p:update(message=message,progress=p*.1))
        else:
            quality=json.loads((dataset/'quality.json').read_text())
        if quality.get('limited_baseline_diagnostic_only'):
            raise ValueError('该数据集只有诊断视角，不能当作照片级重建训练数据')
        update(dataset=dataset.name,quality=quality,state='training',message='优化真实 3D 高斯参数',progress=.1)
        binary=root/'third_party/OpenSplat/build/opensplat'
        if not binary.is_file():raise ValueError('OpenSplat 训练器尚未编译完成')
        transforms=json.loads((dataset/'transforms.json').read_text())
        import numpy as np
        first=np.asarray(transforms['frames'][0]['transform_matrix'])
        atomic_json(directory/'view.json',dict(position=first[:3,3].tolist(),
                    target=(first[:3,3]-first[:3,2]*3).tolist(),up=first[:3,1].tolist()))
        validation=transforms['frames'][len(transforms['frames'])//2]['file_path']
        # Never use a held-out validation image as a training image.
        arguments=[str(binary),str(dataset),'-n',str(job['iterations']),'-d',str(job['downscale']),
                   '--cpu','--sh-degree','2','--max-gaussians','250000','--save-every','250',
                   '--val-image',Path(validation).name,'--val-render',str(directory/'validation'),
                   '--output-cameras',str(directory/'cameras.json'),'-o',str(directory/'model.ply')]
        (directory/'command.json').write_text(json.dumps(arguments,indent=2))
        environment=os.environ.copy();environment.update(OMP_NUM_THREADS='2',MKL_NUM_THREADS='2',OPENBLAS_NUM_THREADS='1')
        start=time.monotonic();losses=[];last_checkpoint=0
        process=subprocess.Popen(arguments,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,env=environment)
        update(trainer_pid=process.pid)
        with (directory/'training.log').open('w',buffering=1) as log:
            for line in process.stdout:
                log.write(line)
                match=re.search(r'Step (\d+): ([0-9.eE+-]+)',line)
                if match:
                    step,loss=int(match[1]),float(match[2]);losses.append([step,loss])
                    elapsed=time.monotonic()-start
                    update(step=step,loss=loss,elapsed_seconds=elapsed,
                           eta_seconds=elapsed/max(step,1)*(job['iterations']-step),
                           progress=.1+.9*step/job['iterations'])
                    checkpoint=(step-1)//250*250
                    path=directory/f'model_{checkpoint}.ply'
                    if checkpoint>last_checkpoint and path.is_file():
                        try:
                            count=ply_to_splat(path,directory/'model.splat.tmp')
                            os.replace(directory/'model.splat.tmp',directory/'model.splat')
                            # Serve the checkpoint as PLY without exposing a partially written file.
                            import shutil
                            shutil.copyfile(path,directory/'checkpoint.ply')
                            os.replace(directory/'checkpoint.ply',directory/'preview.ply')
                            last_checkpoint=checkpoint
                            update(model_ready=True,gaussians=count,checkpoint_step=checkpoint)
                        except (OSError,ValueError):pass
                psnr=re.search(r'validation PSNR: ([0-9.eE+-]+)',line)
                if psnr:update(validation_psnr=float(psnr[1]))
        if process.wait()!=0:raise RuntimeError('训练器失败，请查看 training.log')
        count=ply_to_splat(directory/'model.ply',directory/'model.splat.tmp')
        os.replace(directory/'model.splat.tmp',directory/'model.splat')
        atomic_json(directory/'losses.json',{'training_loss':losses})
        # Side-by-side held-out real photograph and trained rendering.
        import cv2
        outputs=sorted((directory/'validation').glob('*.png'),key=lambda p:int(p.stem))
        if outputs:
            reference=cv2.imread(str(dataset/validation));render=cv2.imread(str(outputs[-1]))
            reference=cv2.resize(reference,(render.shape[1],render.shape[0]))
            import numpy as np
            comparison=np.concatenate([reference,render],axis=1)
            cv2.imwrite(str(directory/'comparison.jpg'),comparison,[cv2.IMWRITE_JPEG_QUALITY,95])
        update(state='completed',progress=1.,model_ready=True,gaussians=count,
               elapsed_seconds=time.monotonic()-start,message='高斯模型训练完成，可在网页浏览；请对照留出视角评估质量')
    except KeyboardInterrupt:
        if process and process.poll() is None:process.terminate()
        update(state='cancelled',message='重建任务已停止')
    except Exception as error:
        if process and process.poll() is None:process.terminate()
        (directory/'error.log').write_text(traceback.format_exc())
        update(state='failed',message=str(error))
        raise


if __name__=='__main__':main(sys.argv[1])
