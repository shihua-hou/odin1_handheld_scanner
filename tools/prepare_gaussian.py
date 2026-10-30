import argparse
from handheld.photogrammetry import export_dataset
parser=argparse.ArgumentParser()
parser.add_argument('bag');parser.add_argument('destination')
parser.add_argument('--width',type=int,default=960)
parser.add_argument('--seeds',type=int,default=20000)
parser.add_argument('--calibration',default='runtime/calibration/calib.yaml')
args=parser.parse_args()
print(export_dataset(args.bag,args.destination,args.calibration,width=args.width,
                     seed_points=args.seeds,progress=lambda label,p:print(f'{p:.3f} {label}',flush=True)),flush=True)
