"""Torch-free CUDA backend: stdlib ctypes wrapper over p40cuda.dll.

Provides device-memory management and the Pearl kernels (noise_gen, noise_A/B,
transpose_i8, pearl_pow_split) with no torch / cuda-python / cupy dependency.
"""
from __future__ import annotations

import ctypes
import os
import sys

import numpy as np

# Universal backend selection: a production bundle may ship BOTH the CUDA
# backend (p40cuda_nv.dll, sm_61..89 + PTX) and the HIP backend for AMD
# (p40cuda_amd.dll, all gfx code objects in one fatbin). The first library
# that loads AND sees a live device wins -- rigs auto-detect their GPU class
# at startup, no flags anywhere. Single-backend builds keep the plain name.
_NAMES = (["p40cuda.dll", "p40cuda_nv.dll", "p40cuda_amd.dll"]
          if sys.platform == "win32"
          else ["libp40cuda.so", "libp40cuda_nv.so", "libp40cuda_amd.so"])
if os.environ.get("P40_LIB"):
    _NAMES = [os.environ["P40_LIB"]]

_HERE = os.path.dirname(os.path.abspath(__file__))
_SEARCH = [_HERE, os.path.dirname(_HERE), getattr(sys, "_MEIPASS", _HERE)]

# Make the runtime DLLs (cudart64_*.dll / amdhip64.dll) findable next to the
# backends, then via the CUDA toolkit env if present.
if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
    for _d in _SEARCH:
        if os.path.isdir(_d):
            try:
                os.add_dll_directory(_d)
            except OSError:
                pass
    for _env in ("CUDA_PATH", "CUDA_PATH_V12_8", "CUDA_PATH_V12_4"):
        _v = os.environ.get(_env)
        if _v and os.path.isdir(os.path.join(_v, "bin")):
            try:
                os.add_dll_directory(os.path.join(_v, "bin"))
            except OSError:
                pass

_lib = _path = None
for _name in _NAMES:
    for _d in _SEARCH:
        _p = os.path.join(_d, _name)
        if not os.path.exists(_p):
            continue
        try:
            _cand = ctypes.CDLL(_p)
        except OSError:
            continue
        _cand.p40_device_count.restype = ctypes.c_int
        if _cand.p40_device_count() > 0:
            _lib, _path = _cand, _p
            break
    if _lib is not None:
        break
if _lib is None:
    raise RuntimeError("no p40 backend with a visible GPU: expected one of "
                       + ", ".join(_NAMES) + " next to the miner "
                       "(NVIDIA needs p40cuda[_nv].dll, AMD needs p40cuda_amd.dll)")

# Backend tag: "cuda" (NVIDIA p40cuda) or "hip" (AMD build of the same ABI).
# The HIP backend has no on-GPU tensor_hash/commitments (no CUTLASS), so the
# miner derives the noise seeds on the host right after setup_job -- see
# HOST_COMMITMENTS usage in miner_capi.mine_job.
try:
    _lib.p40_backend.argtypes = []
    _lib.p40_backend.restype = ctypes.c_char_p
    BACKEND = _lib.p40_backend().decode()
except AttributeError:
    BACKEND = "cuda"
HOST_COMMITMENTS = (BACKEND == "hip")

_VP = ctypes.c_void_p
_I = ctypes.c_int
_Z = ctypes.c_size_t

_lib.p40_malloc.argtypes = [ctypes.POINTER(_VP), _Z]
_lib.p40_free.argtypes = [_VP]
_lib.p40_memcpy_htod.argtypes = [_VP, _VP, _Z]
_lib.p40_memcpy_dtoh.argtypes = [_VP, _VP, _Z]
_lib.p40_memset.argtypes = [_VP, _I, _Z]
_lib.p40_sync.argtypes = []
_lib.p40_transpose_i8.argtypes = [_VP, _VP, _I, _I, _I, _I]
_lib.p40_noise_gen.argtypes = [_VP, _VP, _VP, _VP, _VP, _VP, _I, _I, _I, _I]
_lib.p40_noise_apply_A.argtypes = [_VP, _VP, _VP, _VP, _VP, _VP, _I, _I, _I]
_lib.p40_noise_apply_B.argtypes = [_VP, _VP, _VP, _VP, _VP, _VP, _I, _I, _I]
_lib.p40_noise_gemm.argtypes = [_VP, _VP, _VP, _VP, _I, _I, _I]
_lib.p40_pearl_pow_split.argtypes = [_VP, _VP, _I, _I, _I, _I, _VP, _VP, _VP, _VP, _VP, _VP, _I]
_lib.p40_setup_job.argtypes = [_VP, _VP, _VP, _VP, _VP, _VP, _I, _I, _I, _I, ctypes.c_ulonglong]
_lib.p40_init.argtypes = []
_lib.p40_init.restype = _I
_lib.p40_device_count.argtypes = []
_lib.p40_device_count.restype = _I

# Block-sync init: must run before this process creates a CUDA context, so that
# sync() sleeps the CPU thread instead of spin-waiting. Critical for multi-GPU
# rigs (one process per card) so the workers don't each burn a core. A non-fatal
# error just leaves the default spin behavior, so the return is ignored.
_lib.p40_init()


def device_count() -> int:
    """Number of CUDA devices visible to this process (0 if none / no driver)."""
    return int(_lib.p40_device_count())


def _chk(rc, what):
    if rc != 0:
        raise RuntimeError(f"{what} failed (cuda error {rc})")


class DBuf:
    """A device allocation. `ptr` is a c_void_p usable as a kernel argument."""

    def __init__(self, nbytes: int):
        self.nbytes = int(nbytes)
        self.ptr = _VP()
        _chk(_lib.p40_malloc(ctypes.byref(self.ptr), self.nbytes), "malloc")

    def offset(self, byte_off: int) -> _VP:
        return _VP(self.ptr.value + int(byte_off))

    def from_host(self, arr: np.ndarray):
        a = np.ascontiguousarray(arr)
        _chk(_lib.p40_memcpy_htod(self.ptr, a.ctypes.data_as(_VP), a.nbytes), "htod")

    def to_host(self, arr: np.ndarray):
        _chk(_lib.p40_memcpy_dtoh(arr.ctypes.data_as(_VP), self.ptr, arr.nbytes), "dtoh")

    def memset(self, v: int):
        _chk(_lib.p40_memset(self.ptr, v, self.nbytes), "memset")

    def free(self):
        if self.ptr:
            _lib.p40_free(self.ptr)
            self.ptr = _VP()


def sync():
    _chk(_lib.p40_sync(), "sync")


def transpose_i8(src, dst, rows, cols, src_ld, col_off):
    _chk(_lib.p40_transpose_i8(_as(src), _as(dst), rows, cols, src_ld, col_off), "transpose")


def setup_job(A, B, Bt, key, nsA, nsB, M, N, K, R, seed):
    """GPU job setup: fill A,B; commit; write noise seeds nsA,nsB (device)."""
    _chk(_lib.p40_setup_job(_as(A), _as(B), _as(Bt), _as(key), _as(nsA), _as(nsB),
                            M, N, K, R, seed), "setup_job")


def noise_gemm(X, Y, Z, out, M, K, R):
    """Fast noise-apply: out = clamp(Z + X @ Y^T over R)."""
    _chk(_lib.p40_noise_gemm(_as(X), _as(Y), _as(Z), _as(out), M, K, R), "noise_gemm")


def noise_gen(EAL, EAR, EBL, EBR, key_A, key_B, m, n, k, R):
    _chk(_lib.p40_noise_gen(_as(EAL), _as(EAR), _as(EBL), _as(EBR), _as(key_A),
                            _as(key_B), m, n, k, R), "noise_gen")


def noise_apply_A(A, EAL, EAR_t, EBL_t, ApEA, AxEBL, M, K, R):
    _chk(_lib.p40_noise_apply_A(_as(A), _as(EAL), _as(EAR_t), _as(EBL_t),
                                _as(ApEA), _as(AxEBL), M, K, R), "noise_A")


def noise_apply_B(B, EBR, EAR, EBL, BpEB, EARxBpEB, N, K, R):
    _chk(_lib.p40_noise_apply_B(_as(B), _as(EBR), _as(EAR), _as(EBL),
                                _as(BpEB), _as(EARxBpEB), N, K, R), "noise_B")


def pearl_pow_split(A, Bt, m, n, k, R, key, target, transcript, digests, found, coord, variant):
    """transcript is a caller-owned reusable buffer (>= (m/16)*(n/16)*16 uint32).
    digests may be 0/None to skip the per-tile digest write (mining only needs
    found/coord)."""
    _chk(_lib.p40_pearl_pow_split(_as(A), _as(Bt), m, n, k, R, _as(key), _as(target),
                                  _as(transcript), _as(digests) or _VP(0), _as(found),
                                  _as(coord), variant),
         "pearl_pow_split")


def _as(x):
    """Accept a DBuf, a c_void_p (e.g. from DBuf.offset), or a raw int address."""
    if isinstance(x, DBuf):
        return x.ptr
    return x
