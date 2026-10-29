#!/usr/bin/env python3
"""Bit-exact verification of the HIP (AMD) backend against the pure-python
reference. Run on the AMD rig after packaging/build_hip.sh:

    python tools/test_hip_engine.py

Needs only numpy + blake3 (no torch). Checks:
  1. backend tag / device visibility
  2. setup_job fill range/determinism + B->Bt transpose
  3. noise_gen (all four matrices) vs the reference miner_base math
  4. noise_gemm vs an exact numpy int8 GEMM
  5. transpose_i8 vs numpy
  6. pearl_pow_split: with zero operands the transcript is zero, so each tile
     digest must equal keyed-BLAKE3 of 64 zero bytes (known-answer), and the
     found-flag logic must trip on a max target / stay clear on a zero target.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import numpy as np
import blake3

import cuda_capi as cc

M, N, K, R = 64, 64, 256, 32
fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        fails.append(name)


def host_buf(arr):
    b = cc.DBuf(arr.nbytes)
    b.from_host(arr)
    return b


# ------------------------------------------------------------------ 1
check("backend is hip", cc.BACKEND == "hip", f"got {cc.BACKEND}")
check("device visible", cc.device_count() >= 1, f"count={cc.device_count()}")

# --------------------------------------------------- reference noise (miner_base)
def random_hash(index, seed, key, prepend_index):
    msg = np.zeros(8, np.int32)
    msg[prepend_index] = 1 + index
    return blake3.blake3(msg.tobytes() + seed, key=key).digest()


def uniform_ref(seed, key, rows, rank):
    draws = (rows * rank + 31) // 32
    rb = b"".join(random_hash(i, seed, key, 0) for i in range(draws))
    rt = np.frombuffer(rb, np.uint8)[: rows * rank]
    return ((rt & 63).astype(np.int32) - 32).astype(np.int8).reshape(rows, rank)


def mulhi_u32(a, u):
    return (int(a) * int(u)) >> 32


def perm_ref(seed, key, rows, cols, rank, assign_cols):
    mat = np.zeros((rows, cols), np.int8)
    required = cols if assign_cols else rows
    draws = (required * 4 + 31) // 32
    for i in range(draws):
        words = np.frombuffer(random_hash(i, seed, key, 1), np.uint32)
        for kk in range(8):
            ai = i * 8 + kk
            if ai >= required:
                break
            u = int(words[kk])
            first = u & (rank - 1)
            second = first ^ (1 + mulhi_u32(rank - 1, u))
            if assign_cols:
                mat[first, ai], mat[second, ai] = 1, -1
            else:
                mat[ai, first], mat[ai, second] = 1, -1
    return mat


# ------------------------------------------------------------------ 2
dA = cc.DBuf(M * K); dB = cc.DBuf(K * N); dBt = cc.DBuf(N * K)
dkey = cc.DBuf(32); dnsA = cc.DBuf(32); dnsB = cc.DBuf(32)
key = os.urandom(32)
dkey.from_host(np.frombuffer(key, np.uint8).copy())

cc.setup_job(dA, dB, dBt, dkey, dnsA, dnsB, M, N, K, R, 12345)
cc.sync()
A = np.empty((M, K), np.int8); dA.to_host(A)
B = np.empty((K, N), np.int8); dB.to_host(B)
Bt = np.empty((N, K), np.int8); dBt.to_host(Bt)
check("fill range [-64,63]", int(A.min()) >= -64 and int(A.max()) <= 63,
      f"min={A.min()} max={A.max()}")
check("B->Bt transpose", np.array_equal(Bt, np.ascontiguousarray(B.T)))

cc.setup_job(dA, dB, dBt, dkey, dnsA, dnsB, M, N, K, R, 12345)
cc.sync()
A2 = np.empty((M, K), np.int8); dA.to_host(A2)
check("fill deterministic (same seed)", np.array_equal(A, A2))
ns = np.empty(32, np.uint8); dnsA.to_host(ns)
check("hip setup_job leaves seeds to host", not ns.any())

# ------------------------------------------------------------------ 3
key_A, key_B = os.urandom(32), os.urandom(32)
dEAL = cc.DBuf(M * R); dEAR = cc.DBuf(R * K); dEBL = cc.DBuf(K * R); dEBR = cc.DBuf(N * R)
dkA = host_buf(np.frombuffer(key_A, np.uint8).copy())
dkB = host_buf(np.frombuffer(key_B, np.uint8).copy())
cc.noise_gen(dEAL, dEAR, dEBL, dEBR, dkA, dkB, M, N, K, R)
cc.sync()
seedA, seedB = b"A_tensor" + b"\x00" * 24, b"B_tensor" + b"\x00" * 24
EAL = np.empty((M, R), np.int8); dEAL.to_host(EAL)
EAR = np.empty((R, K), np.int8); dEAR.to_host(EAR)
EBL = np.empty((K, R), np.int8); dEBL.to_host(EBL)
EBR = np.empty((N, R), np.int8); dEBR.to_host(EBR)
check("EAL bit-exact", np.array_equal(EAL, uniform_ref(seedA, key_A, M, R)))
check("EAR bit-exact", np.array_equal(EAR, perm_ref(seedA, key_A, R, K, R, True)))
check("EBL bit-exact", np.array_equal(EBL, perm_ref(seedB, key_B, K, R, R, False)))
check("EBR bit-exact", np.array_equal(EBR, uniform_ref(seedB, key_B, N, R)))

# ------------------------------------------------------------------ 4
rng = np.random.default_rng(7)
X = rng.integers(-32, 32, (M, R), dtype=np.int8)
Y = rng.integers(-1, 2, (K, R), dtype=np.int8)   # sparse-ish keeps sums in range
Z = rng.integers(-64, 64, (M, K), dtype=np.int8)
dX, dY, dZ, dOut = host_buf(X), host_buf(Y), host_buf(Z), cc.DBuf(M * K)
cc.noise_gemm(dX, dY, dZ, dOut, M, K, R)
cc.sync()
out = np.empty((M, K), np.int8); dOut.to_host(out)
ref = np.clip(Z.astype(np.int32) + X.astype(np.int32) @ Y.T.astype(np.int32), -128, 127)
check("noise_gemm bit-exact", np.array_equal(out.astype(np.int32), ref))

# ------------------------------------------------------------------ 5
src = rng.integers(-64, 64, (R, K), dtype=np.int8)
dsrc, ddst = host_buf(src), cc.DBuf(R * K)
cc.transpose_i8(dsrc, ddst, R, K, K, 0)
cc.sync()
dst = np.empty((K, R), np.int8); ddst.to_host(dst)
check("transpose_i8 bit-exact", np.array_equal(dst, np.ascontiguousarray(src.T)))

# ------------------------------------------------------------------ 6
# NOTE: launch_pearl_gemm_only only dispatches for R in {64,128,256}; the
# mandated mining shape is R=256, here R=64 keeps the test tiny.
R2 = 64
A0 = np.zeros((M, K), np.int8); Bt0 = np.zeros((N, K), np.int8)
dA0, dBt0 = host_buf(A0), host_buf(Bt0)
dpk = host_buf(np.frombuffer(key_A, np.uint8).copy())
tgt_max = host_buf(np.frombuffer(bytes([0xFF] * 32), np.uint8).copy())
tgt_zero = host_buf(np.zeros(32, np.uint8).copy())
ntiles = (M // 16) * (N // 16)
dtb = cc.DBuf(ntiles * 16 * 4)
ddg = cc.DBuf(ntiles * 32)
dfound = cc.DBuf(4); dcoord = cc.DBuf(8)

dfound.memset(0)
cc.pearl_pow_split(dA0, dBt0, M, N, K, R2, dpk, tgt_max, dtb, ddg, dfound, dcoord, 0)
cc.sync()
found = np.empty(1, np.int32); dfound.to_host(found)
dg = np.empty((ntiles, 32), np.uint8); ddg.to_host(dg)
kexp = blake3.blake3(b"\x00" * 64, key=key_A).digest()
check("zero-transcript digest == keyed blake3(64 zero bytes)",
      all((dg[i] == np.frombuffer(kexp, np.uint8)).all() for i in range(ntiles)))
check("found trips on max target", int(found[0]) == 1, f"found={found[0]}")

dfound.memset(0)
cc.pearl_pow_split(dA0, dBt0, M, N, K, R2, dpk, tgt_zero, dtb, ddg, dfound, dcoord, 0)
cc.sync()
dfound.to_host(found)
check("found clear on zero target", int(found[0]) == 0, f"found={found[0]}")

print()
print(("ALL PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(0 if not fails else 1)
