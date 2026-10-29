#pragma once
// Offline syntax-check stub of the HIP runtime surface used by the p40 kernel
// set. NOT a real runtime: exists only so `clang++ -fsyntax-only` can validate
// the converted sources (and the hand-written HIP files) on any OS, without
// ROCm or a GPU. Semantics are irrelevant here -- only syntax and types.
#include <cstddef>
#include <cstdint>

typedef void* hipStream_t;
typedef int hipError_t;
static const hipError_t hipSuccess = 0;
enum hipMemcpyKind { hipMemcpyHostToHost = 0, hipMemcpyHostToDevice = 1,
                     hipMemcpyDeviceToHost = 2, hipMemcpyDeviceToDevice = 3 };
#define hipDeviceScheduleBlockingSync 0x04

struct dim3 {
  unsigned x = 0, y = 0, z = 0;
  dim3() {}
  dim3(unsigned a) : x(a) {}
  dim3(unsigned a, unsigned b) : x(a), y(b) {}
  dim3(unsigned a, unsigned b, unsigned c) : x(a), y(b), z(c) {}
};

#define __global__
#define __device__
#define __host__
#define __forceinline__ inline
#define __shared__
#define __constant__
#define __launch_bounds__(...)
#define __align__(...)

// thread/block indices (globals -- kernels are plain functions in this stub)
static dim3 blockIdx, threadIdx, blockDim, gridDim;

struct uint2 { unsigned x, y; };
struct uint4 { unsigned x, y, z, w; };
struct int2 { int x, y; };
static inline uint2 make_uint2(unsigned a, unsigned b) { return {a, b}; }
static inline uint4 make_uint4(unsigned a, unsigned b, unsigned c, unsigned d) {
  return {a, b, c, d};
}

extern "C" {
hipError_t hipMalloc(void**, size_t);
hipError_t hipFree(void*);
hipError_t hipMemcpy(void*, const void*, size_t, hipMemcpyKind);
hipError_t hipMemset(void*, int, size_t);
hipError_t hipMemsetAsync(void*, int, size_t, hipStream_t);
hipError_t hipDeviceSynchronize(void);
hipError_t hipGetLastError(void);
hipError_t hipGetDeviceCount(int*);
hipError_t hipSetDeviceFlags(unsigned int);
hipError_t hipStreamSynchronize(hipStream_t);
}

static inline void __syncthreads() {}
template <class T>
static inline T __shfl_xor_sync(unsigned, T v, int) { return v; }
static inline int atomicCAS(int* a, int cmp, int) { return cmp; }
static inline unsigned atomicCAS(unsigned* a, unsigned cmp, unsigned) { return cmp; }
