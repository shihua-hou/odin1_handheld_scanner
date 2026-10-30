#!/usr/bin/env bash
set -eo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source /opt/ros/humble/setup.bash
if [ -f "$ROOT/install/setup.bash" ]; then source "$ROOT/install/setup.bash"; fi
export ROS_LOCALHOST_ONLY=1
export ROS_DOMAIN_ID=41
export PYTHONNOUSERSITE=1
export ODIN_CALIB_DIR="$ROOT/runtime/calibration"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m handheld.server
