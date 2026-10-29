# inventory_av.ps1 -- WHAT antiviruses are actually on the fleet, remotely,
# one command, no travel. Lists every AV product registered in Windows
# Security Center on each machine, then prints a per-product summary so you can
# decide: push an exclusion via that product's console, or uninstall the junk.
#
# Usage (admin PowerShell with network access to the fleet):
#   powershell -ExecutionPolicy Bypass -File agent\inventory_av.ps1 -Hosts hosts.txt
#   powershell ... -Hosts hosts.txt -OutCsv av-report.csv
#
# hosts.txt: one machine name or IP per line (# comments allowed).
#
# Next steps by what you find:
#   Kaspersky  -> Kaspersky Security Center: push exclusion to the whole network
#                 (one policy), or locally: avp.com via Invoke-Command
#   Defender   -> agent\deploy_exclusion.ps1 (already written)
#   ESET       -> ESET PROTECT console policy, or remote uninstall below
#   junk/other -> remote uninstall (admin's call on your own fleet):
#                 Get-Package/'msiexec /x {GUID} /qn' via Invoke-Command,
#                 Windows falls back to Defender automatically
param(
    [Parameter(Mandatory=$true)][string]$Hosts,
    [string]$OutCsv = "",
    [int]$Throttle = 50
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Hosts)) { throw "hosts file not found: $Hosts" }
$targets = Get-Content $Hosts | Where-Object { $_ -and -not $_.TrimStart().StartsWith("#") } |
           ForEach-Object { $_.Trim() }
Write-Host "querying AV products on $($targets.Count) machines ..."

$rows = @()
$targets | ForEach-Object -ThrottleLimit $Throttle -Parallel {
    $machine = $_
    try {
        $prods = Invoke-Command -ComputerName $machine -ErrorAction Stop -ScriptBlock {
            Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct |
                Select-Object displayName, productState, pathToSignedProductExe
        }
        foreach ($p in $prods) {
            # productState bit 0x1000 = enabled, 0x10 = up to date
            $enabled = ($p.productState -band 0x1000) -ne 0
            [PSCustomObject]@{
                Machine = $machine
                Product = $p.displayName
                Enabled  = $enabled
                State    = ('0x{0:X}' -f $p.productState)
                Exe      = $p.pathToSignedProductExe
                Error    = ""
            }
        }
    } catch {
        [PSCustomObject]@{
            Machine = $machine; Product = ""; Enabled = $false
            State = ""; Exe = ""; Error = $_.Exception.Message
        }
    }
} | ForEach-Object { $rows += $_ }

# per-product summary -- the decision table
""
"=== WHAT IS INSTALLED (per product) ==="
$rows | Where-Object { $_.Product } | Group-Object Product |
    Sort-Object Count -Descending |
    ForEach-Object { "{0,5} machines : {1}" -f $_.Count, $_.Name }

$errs = @($rows | Where-Object { $_.Error })
if ($errs.Count) {
    ""
    "=== UNREACHABLE/FAILED ($($errs.Count)) ==="
    $errs | ForEach-Object { "  $($_.Machine): $($_.Error)" }
}

if ($OutCsv) { $rows | Export-Csv -NoTypeInformation -Encoding UTF8 $OutCsv; Write-Host "csv -> $OutCsv" }
