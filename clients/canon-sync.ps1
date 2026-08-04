<#
canon-sync.ps1 -- keep a TES3MP canon folder byte-identical to the server, downloading only what
changed. Re-run any time; it is safe, idempotent, and self-verifying (every file is checked against
the server manifest's SHA-256). No extra tooling: built-in curl.exe + Get-FileHash only.

  Cold install : point -Canon at an empty folder, run  -> downloads the whole canon
  Update       : after a re-roll, run again             -> only changed/new files, strays removed

Your invite has the -Base token and the manifest SHA. Either edit the defaults below or pass them:
  powershell -ExecutionPolicy Bypass -File canon-sync.ps1 -Base https://... -Canon C:\MW-Canon -ManifestSha <sha>
#>
[CmdletBinding()]
param(
  [string]  $Base        = "https://canon.gaussing.tv/049b7997d13572010d849e8b",  # the vvardenfell canon endpoint
  [string]  $Canon       = "$env:USERPROFILE\MW-Canon",
  [string]  $ManifestSha = "",
  [string]  $Cfg         = "",   # the ACTIVE openmw.cfg to sync the load order into (default: this OS's OpenMW config path)
  [string[]]$Ignore      = @("openmw.cfg","INSTALL.md","tes3mp-client-default.cfg","modlock.json","requiredDataFiles.json","canon-sync.ps1","canon-sync.sh")
)
$ErrorActionPreference = "Stop"
function Sha256([string]$p){ (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLower() }

$Canon = (New-Item -ItemType Directory -Force $Canon).FullName.TrimEnd('\')
$tmp   = Join-Path $env:TEMP ("modlock.{0}.json" -f $PID)

# 1. fetch + (optionally) verify the manifest -- the one trust check a friend makes.
curl.exe -fsSL --retry 5 --retry-delay 2 -o $tmp "$Base/modlock.json"
if ($LASTEXITCODE -ne 0) { throw "cannot fetch manifest: $Base/modlock.json" }
if ($ManifestSha) {
  $got = Sha256 $tmp
  if ($got -ne $ManifestSha.ToLower()) { throw "manifest SHA-256 mismatch: $got != $ManifestSha" }
  Write-Host "manifest verified ($ManifestSha)"
}
$want = @{}   # path (PowerShell hashtables are case-insensitive -> robust to case drift) -> sha256
foreach ($f in (Get-Content -Raw $tmp | ConvertFrom-Json).files) { $want[$f.path] = $f.sha256.ToLower() }
$ig = @{}; foreach ($i in $Ignore) { $ig[$i] = $true }
Write-Host ("manifest: {0} files -> {1}" -f $want.Count, $Canon)

# 2. download only what's missing or changed (each verified against the manifest before it lands).
$ok=0; $dl=0; $fail=@()
foreach ($path in $want.Keys) {
  $local = Join-Path $Canon ($path -replace '/','\')
  if ((Test-Path -LiteralPath $local) -and ((Sha256 $local) -eq $want[$path])) { $ok++; continue }
  New-Item -ItemType Directory -Force (Split-Path -Parent $local) | Out-Null
  $enc = ($path.Split('/') | ForEach-Object { [Uri]::EscapeDataString($_) }) -join '/'
  curl.exe -fsSL --retry 5 --retry-delay 2 -o "$local.part" "$Base/tree/$enc"
  if ($LASTEXITCODE -ne 0)                     { $fail += $path;          Remove-Item "$local.part" -Force -EA SilentlyContinue; continue }
  if ((Sha256 "$local.part") -ne $want[$path]) { $fail += "$path (hash)"; Remove-Item "$local.part" -Force -EA SilentlyContinue; continue }
  Move-Item -LiteralPath "$local.part" -Destination $local -Force
  $dl++
}

# 3. remove strays -- local files not in the manifest (keeps the canon byte-identical), except the
#    per-friend configs/docs in -Ignore.
$del=0
Get-ChildItem -LiteralPath $Canon -Recurse -File | ForEach-Object {
  $rel = ($_.FullName.Substring($Canon.Length).TrimStart('\')) -replace '\\','/'
  if ($want.ContainsKey($rel))               { return }
  if ($ig.ContainsKey(($rel -split '/')[-1])) { return }
  Remove-Item -LiteralPath $_.FullName -Force; $del++
}

# 4. sync the LOAD ORDER into the friend's ACTIVE openmw.cfg -- the axes TES3MP enforces
#    (content / fallback-archive / groundcover). Rewrite ONLY those three axes from the manifest
#    and keep every other line (their data= dirs, fallback= weather/lighting, [General], comments)
#    verbatim. openmw.cfg stays on -Ignore for the file sync above; here we write it at its real
#    OpenMW config-dir path, a different location from the canon tree, so there is no conflict.
$mods = Get-Content -Raw $tmp | ConvertFrom-Json
$axes = [ordered]@{
  'content'          = @($mods.content   | ForEach-Object { $_.name })
  'fallback-archive' = @($mods.archives)
  'groundcover'      = @($mods.groundcover)
}
if (-not $Cfg) {   # locate the ACTIVE openmw.cfg for this OS (override with -Cfg)
  $Cfg = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'My Games\OpenMW\openmw.cfg'
}
New-Item -ItemType Directory -Force (Split-Path -Parent $Cfg) | Out-Null
# cold client: no cfg yet -> seed from the canon's shipped openmw.cfg (per-OS data= lines commented;
# uncomment your pair once, per INSTALL.md), then merge the axes into it.
if (-not (Test-Path -LiteralPath $Cfg)) {
  $seed = Join-Path $Canon 'openmw.cfg'
  if (Test-Path -LiteralPath $seed) { Copy-Item -LiteralPath $seed -Destination $Cfg -Force }
  else { curl.exe -fsSL --retry 5 --retry-delay 2 -o $Cfg "$Base/openmw.cfg" | Out-Null }
  Write-Host "seeded a fresh openmw.cfg at $Cfg (uncomment your OS's data= lines -- see INSTALL.md)" -ForegroundColor Yellow
}
# single-pass axis rewrite: replace each axis block at its FIRST line's position, drop later lines
# of that axis, append any axis absent originally (canonical order), keep all else verbatim.
$old = [System.IO.File]::ReadAllText($Cfg)
$out = New-Object System.Collections.Generic.List[string]
$done = @{}
foreach ($line in ($old -split "`r`n|`n|`r", -1)) {
  $key = if ($line -match '=') { ($line -split '=',2)[0].Trim().ToLower() } else { $null }
  if ($key -and $axes.Contains($key)) {
    if (-not $done.ContainsKey($key)) { foreach ($v in $axes[$key]) { $out.Add("$key=$v") }; $done[$key] = $true }
    continue   # drop the old axis line (names are quote-free -> emit bare above)
  }
  $out.Add($line)
}
foreach ($key in $axes.Keys) { if (-not $done.ContainsKey($key)) { foreach ($v in $axes[$key]) { $out.Add("$key=$v") } } }
$new = (($out -join "`n").TrimEnd("`n")) + "`n"   # OpenMW cfgs are LF, single trailing newline
if ($new -eq $old) {
  Write-Host ("load order already in sync ({0} plugins)." -f $axes['content'].Count)
} else {
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  Copy-Item -LiteralPath $Cfg -Destination "$Cfg.$stamp.bak" -Force
  [System.IO.File]::WriteAllText($Cfg, $new, (New-Object System.Text.UTF8Encoding($false)))
  Write-Host ("load order synced -> {0} ({1} content, {2} archives, {3} groundcover; backup .{4}.bak)" -f `
    $Cfg, $axes['content'].Count, $axes['fallback-archive'].Count, $axes['groundcover'].Count, $stamp) -ForegroundColor Green
}

Remove-Item $tmp -Force -EA SilentlyContinue
Write-Host ("sync: {0} up-to-date, {1} downloaded, {2} removed" -f $ok,$dl,$del)
if ($fail.Count) { Write-Host ("FAILED ({0}): {1}" -f $fail.Count, ($fail -join ', ')) -ForegroundColor Red; exit 1 }
Write-Host "canon in sync." -ForegroundColor Green
