#!/usr/bin/env bash
set -eo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TORCH_ROOT="${ODIN_TORCH_ROOT:?Set ODIN_TORCH_ROOT to a compatible LibTorch installation}"
cmake -S "$ROOT/third_party/OpenSplat" -B "$ROOT/third_party/OpenSplat/build" \
  -DGPU_RUNTIME=CPU -DTorch_DIR="$ROOT/config/torch-cpu" -DODIN_TORCH_ROOT="$TORCH_ROOT" \
  -DOPENSPLAT_USE_PCH=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build "$ROOT/third_party/OpenSplat/build" -j2
"$ROOT/third_party/OpenSplat/build/opensplat" --version
