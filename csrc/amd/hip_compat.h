// hip_compat.h -- HIP/ROCm compatibility layer for the p40 Pascal kernel set.
//
// Included (via -include) into every TU of the HIP build. Provides:
//   1. p40_dp4a: __dp4a replacement -- sdot4 on gfx11+ (RDNA3), exact
//      byte-wise emulation elsewhere. Integer math, so both paths agree
//      bit-for-bit with the CUDA kernel.
//   2. A minimal cute:: shim (fixed-size register tensors: Tensor,
//      make_tensor, make_tensor_like, Int, copy) covering everything the
//      Pascal kernels and blake3.cuh actually use -- no CUTLASS needed.
//   3. CUTLASS_DEVICE / CUTLASS_PRAGMA_UNROLL macros used by blake3.cuh.
#pragma once

#include <cstdint>

#if !defined(__HIP__)
#error "hip_compat.h is only for HIP builds (compile with hipcc)"
#endif

#include <hip/hip_runtime.h>

// ---------------------------------------------------------------- macros
#define CUTLASS_DEVICE __device__ __forceinline__
#define CUTLASS_PRAGMA_UNROLL _Pragma("unroll")

// ---------------------------------------------------------------- dp4a
#if defined(__HIP_DEVICE_COMPILE__)
#if defined(__gfx1100__) || defined(__gfx1101__) || defined(__gfx1102__) || \
    defined(__gfx1103__) || defined(__gfx1150__) || defined(__gfx1151__) || \
    defined(__gfx1152__)
#define P40_SDOT4 1
#endif
__device__ __forceinline__ int p40_dp4a(int a, int b, int c) {
#if defined(P40_SDOT4)
  return __builtin_amdgcn_sdot4(a, b, c, false);
#else
  int r = c;
#pragma unroll
  for (int i = 0; i < 4; ++i)
    r += (int)(int8_t)((a >> (i * 8)) & 0xFF) * (int)(int8_t)((b >> (i * 8)) & 0xFF);
  return r;
#endif
}
#else
// host pass: never called, but must parse
#define p40_dp4a(a, b, c) ((a) + (b) + (c))
#endif

// ------------------------------------------------- minimal cute:: shim
// The kernels use cute only as a fixed-size register-array abstraction:
//   Tensor block = make_tensor<uint32_t>(Int<16>{});
//   Tensor rState = make_tensor_like<uint32_t>(block);
//   copy(block, rBlock);
// blake3.cuh's compress copies an 8-word chaining value into a 16-word state,
// so copy() writes min(src, dst) words.
namespace cute {

template <int N>
struct Int {
  static constexpr int value = N;
};

template <class T, int N>
struct Tensor {
  T v[N];
  __host__ __device__ __forceinline__ T& operator()(int i) { return v[i]; }
  __host__ __device__ __forceinline__ T operator()(int i) const { return v[i]; }
};

template <class T, int N>
__host__ __device__ __forceinline__ Tensor<T, N> make_tensor(Int<N>) {
  return Tensor<T, N>{};
}

template <class T, class U, int N>
__host__ __device__ __forceinline__ Tensor<T, N> make_tensor_like(const Tensor<U, N>&) {
  return Tensor<T, N>{};
}

template <class T, int N, class U, int M>
__host__ __device__ __forceinline__ void copy(const Tensor<U, N>& src, Tensor<T, M>& dst) {
  constexpr int CNT = (N < M) ? N : M;
#pragma unroll
  for (int i = 0; i < CNT; ++i) dst(i) = src(i);
}

}  // namespace cute
