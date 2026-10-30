#!/usr/bin/env bash
set -eo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source /opt/ros/humble/setup.bash
cd "$ROOT"
cp src/odin_ros_driver/package_ros2.xml src/odin_ros_driver/package.xml
export CMAKE_BUILD_PARALLEL_LEVEL=2
colcon build --packages-select odin_ros_driver --cmake-args -DBUILD_SYSTEM=ROS2 \
  -DCMAKE_BUILD_TYPE=Release -DODIN_ENABLE_BACKTRACE=OFF
