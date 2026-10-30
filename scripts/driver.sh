#!/usr/bin/env bash
set -eo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source /opt/ros/humble/setup.bash
source "$ROOT/install/setup.bash"
export ROS_LOCALHOST_ONLY=1
export ROS_DOMAIN_ID=41
export ODIN_CALIB_DIR="$ROOT/runtime/calibration"
mkdir -p "$ODIN_CALIB_DIR" "$ROOT/device_maps"
cd "$ROOT"
exec ros2 run odin_ros_driver host_sdk_sample --ros-args -p config_file:="${ODIN_CONFIG_FILE:-$ROOT/config/handheld.yaml}"
