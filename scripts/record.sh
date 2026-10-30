#!/usr/bin/env bash
set -eo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source /opt/ros/humble/setup.bash
source "$ROOT/install/setup.bash"
export ROS_LOCALHOST_ONLY=1 ROS_DOMAIN_ID=41
mkdir -p "$ROOT/recordings"
exec ros2 bag record -o "$ROOT/recordings/scan-$(date +%Y%m%d-%H%M%S)-$$" \
  --qos-profile-overrides-path "$ROOT/src/odin_ros_driver/script/rosbag2_qos.yaml" \
  /odin1/cloud_slam /odin1/cloud_raw /odin1/imu /odin1/odometry \
  /odin1/odometry_highfreq /odin1/image/compressed /tf /tf_static
