// HIP C ABI over the AMD port of the p40 Pearl kernels (RDNA2/RDNA3).
//
// Exposes the SAME p40_* symbols as csrc/capi/p40_capi.cu (the CUDA build), so
// python/cuda_capi.py drives it unchanged; it even ships under the same file
// name (libp40cuda.so) in AMD bundles. Differences:
//   * p40_backend() returns "hip" -> the Python side switches to host-side
//     commitments (pearl_host), because the CuTe tensor_hash pipeline is not
//     ported. p40_setup_job therefore only fills A/B and transposes B->Bt;
//     nsA/nsB are filled by the caller afterwards.
//   * pearl_pow_split / noise_gemm always take the DP4A-style scalar path
//     (sdot4 on gfx11+); the CUTLASS tensor-core kernels are CUDA-only for now.
//
// Every function returns a hipError_t as int (0 == hipSuccess).
// Compiles to a standalone shared library linking only the HIP runtime.

#include <hip/hip_runtime.h>
#include <cstdint>
#include <cstddef>

// kernel launchers (defined in the hipified/ported translation units)
extern "C" void launch_fill_rand_i8(int8_t* out, int64_t numel, uint64_t seed, hipStream_t stream);
extern "C" void launch_transpose_i8(const int8_t* src, int8_t* dst, int rows, int cols, hipStream_t stream);
extern "C" void launch_noise_gen_hip(int8_t*, int8_t*, int8_t*, int8_t*, const uint8_t*,
                                     const uint8_t*, int, int, int, int, hipStream_t);
extern "C" void launch_noise_A(const int8_t*, const int8_t*, const int8_t*, const int8_t*,
                               int8_t*, int32_t*, int, int, int, hipStream_t);
extern "C" void launch_noise_B(const int8_t*, const int8_t*, const int8_t*, const int8_t*,
                               int8_t*, int32_t*, int, int, int, hipStream_t);
extern "C" void launch_noise_gemm(const int8_t* X, const int8_t* Y, const int8_t* Z,
                                  int8_t* out, int M, int K, int R, hipStream_t);
extern "C" void launch_pearl_gemm_only(const int8_t*, const int8_t*, int, int, int, int,
                                       uint32_t*, int, hipStream_t);
extern "C" void launch_pearl_blake3(const uint32_t*, int, int, const uint32_t*,
                                    const uint32_t*, uint8_t*, int*, int*, hipStream_t);

#ifdef _WIN32
#define P40_API extern "C" __declspec(dllexport)
#else
#define P40_API extern "C" __attribute__((visibility("default")))
#endif

P40_API const char* p40_backend(void) { return "hip"; }

// =========================== init / device ================================
P40_API int p40_init(void) {
  return (int)hipSetDeviceFlags(hipDeviceScheduleBlockingSync);
}
P40_API int p40_device_count(void) {
  int n = 0;
  if (hipGetDeviceCount(&n) != hipSuccess) return 0;
  return n;
}

// =========================== memory helpers ===============================
P40_API int p40_malloc(void** p, size_t n) { return (int)hipMalloc(p, n); }
P40_API int p40_free(void* p) { return (int)hipFree(p); }
P40_API int p40_memcpy_htod(void* d, const void* h, size_t n) {
  return (int)hipMemcpy(d, h, n, hipMemcpyHostToDevice);
}
P40_API int p40_memcpy_dtoh(void* h, const void* d, size_t n) {
  return (int)hipMemcpy(h, d, n, hipMemcpyDeviceToHost);
}
P40_API int p40_memset(void* d, int v, size_t n) { return (int)hipMemset(d, v, n); }
P40_API int p40_sync(void) { return (int)hipDeviceSynchronize(); }

// =========================== transpose =====================================
__global__ void transpose_i8_kernel(const int8_t* __restrict__ src,
                                    int8_t* __restrict__ dst, int rows, int cols,
                                    int src_ld, int col_off) {
  const long total = (long)rows * cols;
  for (long idx = (long)blockIdx.x * blockDim.x + threadIdx.x; idx < total;
       idx += (long)gridDim.x * blockDim.x) {
    const int r = (int)(idx / cols);
    const int c = (int)(idx % cols);
    dst[(size_t)c * rows + r] = src[(size_t)r * src_ld + col_off + c];
  }
}

P40_API int p40_transpose_i8(const void* src, void* dst, int rows, int cols,
                             int src_ld, int col_off) {
  const long total = (long)rows * cols;
  int tpb = 256;
  long blocks = (total + tpb - 1) / tpb;
  if (blocks > 65535) blocks = 65535;
  transpose_i8_kernel<<<(unsigned)blocks, tpb>>>((const int8_t*)src,
                                                 (int8_t*)dst, rows, cols,
                                                 src_ld, col_off);
  return (int)hipGetLastError();
}

// =========================== kernels =======================================
// HIP variant: fill A[M,K] and B[K,N], transpose B->Bt[N,K]. NO commitments --
// the CuTe tensor_hash pipeline is CUDA-only; with backend "hip" the Python
// miner derives nsA/nsB on the host (pearl_host, the validated Rust MerkleTree)
// and uploads them into the caller's nsA/nsB buffers right after this call.
P40_API int p40_setup_job(void* A, void* B, void* Bt, const void* key,
                          void* nsA, void* nsB, int M, int N, int K, int R,
                          unsigned long long seed) {
  (void)key; (void)nsA; (void)nsB;  // commitments are host-side on this backend
  hipStream_t s = 0;
  launch_fill_rand_i8((int8_t*)A, (int64_t)M * K, seed, s);
  launch_fill_rand_i8((int8_t*)B, (int64_t)K * N, seed + 1, s);
  launch_transpose_i8((const int8_t*)B, (int8_t*)Bt, K, N, s);  // B[K,N] -> Bt[N,K]
  return (int)hipGetLastError();
}

P40_API int p40_noise_gen(void* EAL, void* EAR, void* EBL, void* EBR,
                          const void* key_A, const void* key_B,
                          int m, int n, int k, int R) {
  launch_noise_gen_hip((int8_t*)EAL, (int8_t*)EAR, (int8_t*)EBL, (int8_t*)EBR,
                       (const uint8_t*)key_A, (const uint8_t*)key_B,
                       m, n, k, R, 0);
  return (int)hipGetLastError();
}

P40_API int p40_noise_apply_A(const void* A, const void* EAL, const void* EAR,
                              const void* EBL, void* ApEA, void* AxEBL,
                              int M, int K, int R) {
  launch_noise_A((const int8_t*)A, (const int8_t*)EAL, (const int8_t*)EAR,
                 (const int8_t*)EBL, (int8_t*)ApEA, (int32_t*)AxEBL, M, K, R, 0);
  return (int)hipGetLastError();
}

P40_API int p40_noise_apply_B(const void* B, const void* EBR, const void* EAR,
                              const void* EBL, void* BpEB, void* EARxBpEB,
                              int N, int K, int R) {
  launch_noise_B((const int8_t*)B, (const int8_t*)EBR, (const int8_t*)EAR,
                 (const int8_t*)EBL, (int8_t*)BpEB, (int32_t*)EARxBpEB, N, K, R, 0);
  return (int)hipGetLastError();
}

// out[M,K] = clamp(Z + X @ Y^T over R) -- DP4A/sdot4 path only (no CuTe IMMA).
P40_API int p40_noise_gemm(const void* X, const void* Y, const void* Z, void* out,
                           int M, int K, int R) {
  launch_noise_gemm((const int8_t*)X, (const int8_t*)Y, (const int8_t*)Z,
                    (int8_t*)out, M, K, R, 0);
  return (int)hipGetLastError();
}

// Two-step search: GEMM-only -> transcript -> BLAKE3. Same contract as the
// CUDA p40_pearl_pow_split, always on the scalar kernel path.
P40_API int p40_pearl_pow_split(const void* A, const void* Bt, int m, int n,
                                int k, int R, const void* key, const void* target,
                                void* transcript, void* digests, void* found,
                                void* coord, int variant) {
  const int num_tiles = (m / 16) * (n / 16);
  uint32_t* tb = (uint32_t*)transcript;
  launch_pearl_gemm_only((const int8_t*)A, (const int8_t*)Bt, m, n, k, R, tb,
                         variant, 0);
  launch_pearl_blake3(tb, num_tiles, n, (const uint32_t*)key,
                      (const uint32_t*)target, (uint8_t*)digests, (int*)found,
                      (int*)coord, 0);
  return (int)hipGetLastError();
}
