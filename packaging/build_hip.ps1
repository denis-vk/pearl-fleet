# build_hip.ps1 -- build p40cuda.dll (HIP backend for AMD RDNA2/3) ON WINDOWS.
#
# Prereqs: AMD HIP SDK for Windows (https://rocm.docs.amd.com - HIP SDK,
# default path "C:\Program Files\AMD\ROCm\<ver>") + MSVC 2022 (vcvars64).
# No CUDA, no CUTLASS needed. AMD has no PTX forward-compat: pass your card's
# exact arch via -Archs (see https://rocm.docs.amd.com/projects/install-on-windows)
#
# Usage (from the repo root, plain or admin PowerShell):
#   powershell -ExecutionPolicy Bypass -File packaging\build_hip.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build_hip.ps1 -Archs gfx1102
param(
    [string]$HipPath = "",
    # Full universal arch list: CDNA2 (gfx90a), RDNA1 (gfx1010/1012), RDNA2
    # (gfx1030-1032 desktop, 1035/1036 mobile+iGPU), RDNA3 (gfx1100-1103,
    # incl. 780M), RDNA3.5 APUs (gfx1150/1151), RDNA4 (gfx1200/1201). One
    # fatbin carries ALL code objects; the runtime picks its own -- nothing to
    # detect at build time. Older HIP SDKs reject gfx120x/gfx90a: the build
    # auto-retries without them.
    [string[]]$Archs = @("gfx90a", "gfx1010", "gfx1012", "gfx1030", "gfx1031",
                         "gfx1032", "gfx1035", "gfx1036", "gfx1100", "gfx1101",
                         "gfx1102", "gfx1103", "gfx1150", "gfx1151",
                         "gfx1200", "gfx1201"),
    [string]$OutName = "p40cuda.dll",   # universal bundle: p40cuda_amd.dll
    [string]$VcVars = "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not $HipPath) {
    $cand = Get-ChildItem "C:\Program Files\AMD\ROCm" -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | Select-Object -First 1
    if ($cand) { $HipPath = $cand.FullName }
    if (-not $HipPath) { throw "HIP SDK not found; pass -HipPath <path to ROCm dir>" }
}
$hipcc = Join-Path $HipPath "bin\hipcc.exe"
if (-not (Test-Path $hipcc)) { throw "hipcc.exe not found at $hipcc" }
if (-not (Test-Path $VcVars)) { throw "vcvars64.bat not found at $VcVars (edit -VcVars)" }

# ---- 1. convert the CUDA sources (deterministic token table, mirrors build_hip.sh)
$Build = "build_hip"
New-Item -ItemType Directory -Force -Path "$Build\gemm", "$Build\blake3", "$Build\capi" | Out-Null

function Convert-P40Src($src, $dst) {
    $t = Get-Content $src -Raw
    foreach ($pair in @(
        @('\bcudaMalloc\b', 'hipMalloc'), @('\bcudaFree\b', 'hipFree'),
        @('\bcudaMemcpy\b', 'hipMemcpy'), @('cudaMemcpyHostToDevice', 'hipMemcpyHostToDevice'),
        @('cudaMemcpyDeviceToHost', 'hipMemcpyDeviceToHost'),
        @('\bcudaMemset\b', 'hipMemset'), @('\bcudaDeviceSynchronize\b', 'hipDeviceSynchronize'),
        @('\bcudaGetLastError\b', 'hipGetLastError'), @('\bcudaStream_t\b', 'hipStream_t'),
        @('\bcudaStreamSynchronize\b', 'hipStreamSynchronize'),
        @('\bcudaGetDeviceCount\b', 'hipGetDeviceCount'),
        @('\bcudaSetDeviceFlags\b', 'hipSetDeviceFlags'),
        @('cudaDeviceScheduleBlockingSync', 'hipDeviceScheduleBlockingSync'),
        @('#include <cuda_runtime\.h>', '#include <hip/hip_runtime.h>')
    )) { $t = $t -replace $pair[0], $pair[1] }
    # drop CUTLASS/CuTe includes (hip_compat.h provides the shim), normalize the
    # arch guards through P40_SDOT4 (the ">= 610" tail must go or gfx11 falls
    # back to emulation), and route every __dp4a through p40_dp4a.
    $t = ($t -split "`r?`n" | Where-Object { $_ -notmatch '#include [<"](cute|cutlass)/' }) -join "`n"
    $t = $t.Replace('__CUDA_ARCH__', 'P40_SDOT4').Replace('__HIP_ARCH__', 'P40_SDOT4')
    $t = $t.Replace('__dp4a(', 'p40_dp4a(')
    $t = $t -replace 'defined\(P40_SDOT4\) && P40_SDOT4 >= \d+', 'defined(P40_SDOT4)'
    [IO.File]::WriteAllText((Join-Path (Get-Location) $dst), $t)
}

foreach ($f in @("gemm\rng_fill_sm61.cu", "gemm\noise_gemm_sm61.cu", "gemm\noising_sm61.cu",
                 "gemm\pearl_gemm_only_sm61.cu", "gemm\pearl_blake3_sm61.cu", "blake3\blake3.cu",
                 "blake3\blake3.cuh")) {
    Convert-P40Src ("csrc\" + $f) ($Build + "\" + $f)
}
Copy-Item "csrc\blake3\blake3_constants.hpp" "$Build\blake3\blake3_constants.hpp" -Force

# ---- 2. compile with hipcc (needs MSVC in the environment for the host side)
$srcs = "csrc\capi\p40_hip_capi.cu csrc\amd\noise_gen_hip.cu " +
        "$Build\gemm\rng_fill_sm61.cu $Build\gemm\noise_gemm_sm61.cu $Build\gemm\noising_sm61.cu " +
        "$Build\gemm\pearl_gemm_only_sm61.cu $Build\gemm\pearl_blake3_sm61.cu $Build\blake3\blake3.cu"

function Invoke-Hipcc([string[]]$tryArchs) {
    $archFlags = ($tryArchs | ForEach-Object { "--offload-arch=$_" }) -join " "
    $cmd = "`"$hipcc`" -shared -o $OutName $srcs " +
           "-I $Build -I csrc -include csrc\amd\hip_compat.h " +
           "-std=c++20 -O3 -DNDEBUG -DP40_NO_TORCH $archFlags"
    cmd /c "call `"$VcVars`" >nul && $cmd"
    return $LASTEXITCODE -eq 0
}

# Older HIP SDKs don't know gfx120x (RDNA4) / gfx90a (CDNA2): drop and retry.
$attempt = $Archs
$built = $false
foreach ($drop in @(@(), @("gfx1200", "gfx1201"), @("gfx1200", "gfx1201", "gfx90a"))) {
    if (-not $built) {
        $try = @($attempt | Where-Object { $drop -notcontains $_ })
        Write-Host "hipcc archs: $($try -join ' ')"
        $built = Invoke-Hipcc $try
        $attempt = $try
    }
}
if (-not $built) { throw "hipcc build failed (see output above)" }

# ---- 3. ship the HIP runtime next to the dll (rigs need only the AMD driver)
Copy-Item (Join-Path $HipPath "bin\amdhip64.dll") . -Force
Write-Host "== built $OutName (HIP backend, universal arch list) + amdhip64.dll"
Write-Host "   quick check on a rig: python tools\test_hip_engine.py"
