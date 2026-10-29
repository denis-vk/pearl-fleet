#!/usr/bin/env bash
# check_hip_offline.sh -- validate the HIP port WITHOUT ROCm or any GPU.
#
# Runs the EXACT conversion pipeline from packaging/build_hip.sh (the sed
# table + guard normalization), then clang++ -fsyntax-only over every converted
# TU plus the hand-written HIP files, with tools/hip_stub as the runtime stub.
# Launch syntax `<<<...>>>` is stripped (kernels become plain function calls),
# which keeps everything else -- types, templates, signatures -- checked.
#
# Catches typos, missing includes, extern/definition signature mismatches and
# template errors BEFORE the first real hipcc run on the Windows build box.
set -euo pipefail
cd "$(dirname "$0")/.."

BUILD=build_hip_check
STUB=tools/hip_stub
rm -rf "$BUILD"
mkdir -p $BUILD/gemm $BUILD/blake3 $BUILD/capi $BUILD/amd

# portable in-place sed (GNU vs BSD)
if sed --version >/dev/null 2>&1; then SED_I=(-i); else SED_I=(-i ''); fi

convert() {  # convert <src> <dst>  (same rules as packaging/build_hip.sh)
  local src=$1 dst=$2
  # NOTE: [[:<:]]/[[:>:]] word boundaries work in BOTH GNU and BSD sed (\b is
  # GNU-only); production build_hip.sh runs on GNU sed where \b also works.
  sed -e 's/[[:<:]]cudaMalloc[[:>:]]/hipMalloc/g; s/[[:<:]]cudaFree[[:>:]]/hipFree/g' \
      -e 's/[[:<:]]cudaMemcpy[[:>:]]/hipMemcpy/g; s/cudaMemcpyHostToDevice/hipMemcpyHostToDevice/g; s/cudaMemcpyDeviceToHost/hipMemcpyDeviceToHost/g' \
      -e 's/[[:<:]]cudaMemset[[:>:]]/hipMemset/g' \
      -e 's/[[:<:]]cudaDeviceSynchronize[[:>:]]/hipDeviceSynchronize/g' \
      -e 's/[[:<:]]cudaGetLastError[[:>:]]/hipGetLastError/g' \
      -e 's/[[:<:]]cudaStream_t[[:>:]]/hipStream_t/g; s/[[:<:]]cudaStreamSynchronize[[:>:]]/hipStreamSynchronize/g' \
      -e 's/[[:<:]]cudaGetDeviceCount[[:>:]]/hipGetDeviceCount/g' \
      -e 's/[[:<:]]cudaSetDeviceFlags[[:>:]]/hipSetDeviceFlags/g' \
      -e 's/cudaDeviceScheduleBlockingSync/hipDeviceScheduleBlockingSync/g' \
      -e 's/#include <cuda_runtime.h>/#include <hip\/hip_runtime.h>/' "$src" > "$dst"
  sed "${SED_I[@]}" -E '/#include [<"](cute|cutlass)\//d' "$dst"
  sed "${SED_I[@]}" 's/__CUDA_ARCH__/P40_SDOT4/g; s/__HIP_ARCH__/P40_SDOT4/g; s/__dp4a(/p40_dp4a(/g' "$dst"
  sed "${SED_I[@]}" -E 's/defined\(P40_SDOT4\) && P40_SDOT4 >= [0-9]+/defined(P40_SDOT4)/' "$dst"
}

for f in gemm/rng_fill_sm61.cu gemm/noise_gemm_sm61.cu gemm/noising_sm61.cu \
         gemm/pearl_gemm_only_sm61.cu gemm/pearl_blake3_sm61.cu blake3/blake3.cu; do
  convert "csrc/$f" "$BUILD/$f"
done
convert csrc/blake3/blake3.cuh $BUILD/blake3/blake3.cuh
cp csrc/blake3/blake3_constants.hpp $BUILD/blake3/
cp csrc/amd/hip_compat.h $BUILD/amd/
cp csrc/amd/noise_gen_hip.cu $BUILD/amd/
cp csrc/capi/p40_hip_capi.cu $BUILD/capi/

# check-only: strip the launch config <<<...>>> (kernels stay as plain calls)
find $BUILD -name '*.cu' -o -name '*.cuh' | while read -r f; do
  sed "${SED_I[@]}" -E 's/<<<[^>]*>>>//' "$f"
done

FAIL=0
for f in $BUILD/gemm/*.cu $BUILD/blake3/blake3.cu $BUILD/amd/noise_gen_hip.cu \
         $BUILD/capi/p40_hip_capi.cu; do
  if clang++ -std=c++20 -fsyntax-only -x c++ -D__HIP__ -I "$STUB" -I "$BUILD" \
       -include $BUILD/amd/hip_compat.h "$f" 2> "$BUILD/err.txt"; then
    echo "PASS  $f"
  else
    echo "FAIL  $f"
    head -30 "$BUILD/err.txt"
    FAIL=1
  fi
done
exit $FAIL
