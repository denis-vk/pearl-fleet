#!/usr/bin/env bash
# build_hip.sh -- build libp40cuda.so (HIP/ROCm backend) for AMD GPUs.
#
# RUN ON A LINUX MACHINE WITH ROCm INSTALLED (hipcc in PATH, e.g. from
# /opt/rocm). Produces the drop-in HIP backend: same p40_* ABI as the CUDA
# p40cuda.dll, detected by python/cuda_capi.py via p40_backend() == "hip".
#
# Target archs (exact -- AMD has no PTX forward-compat): default covers
# RDNA2 (gfx1030: 6800/6900) and RDNA3 (gfx1100: 7900, gfx1102: 7600/7700,
# gfx1103: 780M). Override:  HIP_ARCHES="gfx1100 gfx1101" bash packaging/build_hip.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ARCHES=${HIP_ARCHES:-"gfx1030 gfx1100 gfx1102 gfx1103"}
BUILD=build_hip
OUT=libp40cuda.so   # drop-in name: the agent/miner load it unchanged

# ---- 1. stage + convert the CUDA sources -----------------------------------
# hipify-perl if available; otherwise a deterministic sed table (the kernel
# set uses only this small API surface).
mkdir -p $BUILD/gemm $BUILD/blake3 $BUILD/capi

convert() {  # convert <src> <dst>
  local src=$1 dst=$2
  if command -v hipify-perl >/dev/null 2>&1; then
    hipify-perl "$src" > "$dst"
  else
    sed -e 's/\bcudaMalloc\b/hipMalloc/g; s/\bcudaFree\b/hipFree/g' \
        -e 's/\bcudaMemcpy\b/hipMemcpy/g; s/cudaMemcpyHostToDevice/hipMemcpyHostToDevice/g; s/cudaMemcpyDeviceToHost/hipMemcpyDeviceToHost/g' \
        -e 's/\bcudaMemset\b/hipMemset/g' \
        -e 's/\bcudaDeviceSynchronize\b/hipDeviceSynchronize/g' \
        -e 's/\bcudaGetLastError\b/hipGetLastError/g' \
        -e 's/\bcudaStream_t\b/hipStream_t/g; s/\bcudaStreamSynchronize\b/hipStreamSynchronize/g' \
        -e 's/\bcudaGetDeviceCount\b/hipGetDeviceCount/g' \
        -e 's/\bcudaSetDeviceFlags\b/hipSetDeviceFlags/g' \
        -e 's/cudaDeviceScheduleBlockingSync/hipDeviceScheduleBlockingSync/g' \
        -e 's/#include <cuda_runtime.h>/#include <hip\/hip_runtime.h>/' "$src" > "$dst"
  fi
  # normalize HIP-build hazards regardless of the converter:
  #  - drop CUTLASS/CuTe includes (hip_compat.h provides the shim)
  #  - route every arch guard through P40_SDOT4 (gfx11+ = intrinsic path); the
  #    ">= 610" numeric tail must go too, or gfx11 would fall to the emulation
  #  - route every __dp4a through p40_dp4a (sdot4 or exact emulation)
  sed -i -E '/#include [<"](cute|cutlass)\//d' "$dst"
  sed -i 's/__CUDA_ARCH__/P40_SDOT4/g; s/__HIP_ARCH__/P40_SDOT4/g; s/__dp4a(/p40_dp4a(/g' "$dst"
  sed -i -E 's/defined\(P40_SDOT4\) && P40_SDOT4 >= [0-9]+/defined(P40_SDOT4)/' "$dst"
}

for f in gemm/rng_fill_sm61.cu gemm/noise_gemm_sm61.cu gemm/noising_sm61.cu \
         gemm/pearl_gemm_only_sm61.cu gemm/pearl_blake3_sm61.cu blake3/blake3.cu; do
  convert "csrc/$f" "$BUILD/$f"
done
convert csrc/blake3/blake3.cuh $BUILD/blake3/blake3.cuh
cp csrc/blake3/blake3_constants.hpp $BUILD/blake3/blake3_constants.hpp

# ---- 2. compile --------------------------------------------------------------
ARCH_FLAGS=""
for a in $ARCHES; do ARCH_FLAGS+=" --offload-arch=$a"; done

hipcc -shared -o "$OUT" \
  csrc/capi/p40_hip_capi.cu \
  csrc/amd/noise_gen_hip.cu \
  $BUILD/gemm/rng_fill_sm61.cu \
  $BUILD/gemm/noise_gemm_sm61.cu \
  $BUILD/gemm/noising_sm61.cu \
  $BUILD/gemm/pearl_gemm_only_sm61.cu \
  $BUILD/gemm/pearl_blake3_sm61.cu \
  $BUILD/blake3/blake3.cu \
  -I $BUILD -I csrc -include csrc/amd/hip_compat.h \
  -std=c++20 -O3 -DNDEBUG -DP40_NO_TORCH -fPIC $ARCH_FLAGS

echo "built $OUT for: $ARCHES"
echo "quick check: python tools/test_hip_engine.py   (runs the bit-exact suite)"
