// HIP noise generation for the Pearl PoW -- bit-exact with the reference
// miner_base / pearl_miner.generate_noise (the authoritative spec):
//
//   EAL[M,R], EBR[N,R] : dense int8 in [-32, 32) from keyed-BLAKE3 streams.
//       Element e (row-major) <- byte e of the concatenated digests
//       blake3_keyed(msg{int32[8]: msg[0]=1+draw} || seed), draw = e/32,
//       value = (byte & 63) - 32.
//   EAR[R,K] (K-major), EBL[K,R] (R-major) : sparse +-1 permutation factors.
//       For position p (0..K-1): u = word (p%8) of digest
//       blake3_keyed(msg{int32[8]: msg[1]=1+(p/8)} || seed);
//       first = u & (R-1); second = first ^ (1 + mulhi(R-1, u));
//       EAR[first,p]=+1, EAR[second,p]=-1 / EBL[p,first]=+1, EBL[p,second]=-1.
//
//   Seeds: b"A_tensor"+24 zeros for (EAL, EAR) keyed by key_A (= commitment_A);
//          b"B_tensor"+24 zeros for (EBL, EBR) keyed by key_B (= commitment_B).
//
// This replaces the CuTe-based upstream noise_generation kernel so the HIP
// build has no CUTLASS dependency. One keyed-BLAKE3 single-block compress per
// 32-byte digest (upstream does the same).

#include <hip/hip_runtime.h>
#include <cstdint>

#include "blake3/blake3.cuh"

namespace {

using cute::Int;
using cute::Tensor;
using cute::make_tensor;

__device__ __forceinline__ uint32_t mulhi_u32(uint32_t a, uint32_t b) {
  return (uint32_t)(((uint64_t)a * b) >> 32);
}

// One keyed-BLAKE3 compress of the 64-byte block {msg int32[8], seed[32B]};
// out = first 8 state words (the 32-byte digest).
__device__ __forceinline__ void keyed_digest32(
    const uint32_t* __restrict__ key, uint32_t s0, uint32_t s1,
    uint32_t msg_word, int prepend_index, uint32_t out[8]) {
  Tensor block = make_tensor<uint32_t>(Int<16>{});
  Tensor cv = make_tensor<uint32_t>(Int<8>{});
#pragma unroll
  for (int i = 0; i < 16; ++i) block(i) = 0u;
  block(prepend_index) = msg_word;
  block(8) = s0;
  block(9) = s1;  // words 10..15 stay 0 (24 zero bytes of the seed)
#pragma unroll
  for (int i = 0; i < 8; ++i) cv(i) = key[i];
  blake3::compress_msg_block_u32(block, cv,
                                 blake3::COMPRESS_PARAMS_SINGLE_BLOCK_KEYED);
#pragma unroll
  for (int i = 0; i < 8; ++i) out[i] = cv(i);
}

// Dense uniform int8: 32 elements per digest (rng_range 128 -> mask 63, zp 32).
__global__ void uniform_i8_kernel(int8_t* __restrict__ out, int64_t numel,
                                  const uint32_t* __restrict__ key,
                                  uint32_t s0, uint32_t s1,
                                  int mask, int zero_point) {
  const int64_t draw = (int64_t)blockIdx.x * blockDim.x + threadIdx.x;
  const int64_t base = draw * 32;
  if (base >= numel) return;
  uint32_t d[8];
  keyed_digest32(key, s0, s1, (uint32_t)(1u + draw), 0, d);
  const uint8_t* bytes = reinterpret_cast<const uint8_t*>(d);
  for (int j = 0; j < 32; ++j) {
    const int64_t e = base + j;
    if (e < numel) out[e] = (int8_t)((int)(bytes[j] & mask) - zero_point);
  }
}

// Sparse +-1 permutation factors. k_major: out is [R,K] (position = column);
// otherwise out is [K,R] (position = row). `other` is the non-position dim.
__global__ void perm_i8_kernel(int8_t* __restrict__ out, int positions,
                               int other, int k_major,
                               const uint32_t* __restrict__ key,
                               uint32_t s0, uint32_t s1, int rank) {
  const int pos = blockIdx.x * blockDim.x + threadIdx.x;
  if (pos >= positions) return;
  uint32_t d[8];
  keyed_digest32(key, s0, s1, (uint32_t)(1u + (pos >> 3)), 1, d);
  const uint32_t u = d[pos & 7];
  const uint32_t first = u & (uint32_t)(rank - 1);
  const uint32_t second = first ^ (1u + mulhi_u32((uint32_t)(rank - 1), u));
  if (k_major) {  // out[r, pos] at r*other + pos
    out[(size_t)first * other + pos] = 1;
    out[(size_t)second * other + pos] = -1;
  } else {        // out[pos, r] at pos*other + r
    out[(size_t)pos * other + first] = 1;
    out[(size_t)pos * other + second] = -1;
  }
}

}  // namespace

extern "C" void launch_noise_gen_hip(int8_t* EAL, int8_t* EAR, int8_t* EBL,
                                     int8_t* EBR, const uint8_t* key_A,
                                     const uint8_t* key_B, int m, int n, int k,
                                     int R, hipStream_t stream) {
  // "A_tensor"/"B_tensor" + 24 zero bytes, as little-endian u32 words.
  const uint32_t SEED_A0 = 0x65745F41u, SEED_A1 = 0x726F736Eu;
  const uint32_t SEED_B0 = 0x65745F42u, SEED_B1 = 0x726F736Eu;
  const int MASK = 63, ZP = 32;  // rng_range 128
  const uint32_t* kA = reinterpret_cast<const uint32_t*>(key_A);
  const uint32_t* kB = reinterpret_cast<const uint32_t*>(key_B);

  hipMemsetAsync(EAR, 0, (size_t)R * k, stream);
  hipMemsetAsync(EBL, 0, (size_t)k * R, stream);

  const int64_t numelA = (int64_t)m * R;
  int draws = (int)((numelA + 31) / 32);
  uniform_i8_kernel<<<(draws + 255) / 256, 256, 0, stream>>>(
      EAL, numelA, kA, SEED_A0, SEED_A1, MASK, ZP);

  const int64_t numelB = (int64_t)n * R;
  draws = (int)((numelB + 31) / 32);
  uniform_i8_kernel<<<(draws + 255) / 256, 256, 0, stream>>>(
      EBR, numelB, kB, SEED_B0, SEED_B1, MASK, ZP);

  perm_i8_kernel<<<(k + 255) / 256, 256, 0, stream>>>(
      EAR, k, k, 1, kA, SEED_A0, SEED_A1, R);
  perm_i8_kernel<<<(k + 255) / 256, 256, 0, stream>>>(
      EBL, k, R, 0, kB, SEED_B0, SEED_B1, R);
}
