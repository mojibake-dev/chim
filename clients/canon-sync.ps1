<#
canon-sync.ps1 -- keep a TES3MP canon folder byte-identical to the server, downloading only what
changed. Re-run any time; it is safe, idempotent, and self-verifying (every file is checked against
the server manifest's SHA-256). No extra tooling: built-in curl.exe + Get-FileHash only.

  Install : unzip the canon, then run this script FROM that folder -> it syncs its own folder
  Update  : run it again after a re-roll                           -> only changed/new files, strays removed

It syncs the folder it lives in, so no path is needed. Your invite has the manifest SHA:
  powershell -ExecutionPolicy Bypass -File canon-sync.ps1 -ManifestSha <sha>
(Different cwd? It still targets its own folder via $PSScriptRoot. Override with -Canon only if you must.)
#>
[CmdletBinding()]
param(
  [string]  $Base        = "https://canon.gaussing.tv/049b7997d13572010d849e8b",  # the vvardenfell canon endpoint
  [string]  $Canon       = "",   # default: the folder THIS script lives in (it ships inside the canon dir)
  [string]  $ManifestSha = "",
  [string]  $Cfg         = "",   # the ACTIVE openmw.cfg to sync the load order into (default: this OS's OpenMW config path)
  [string]  $SettingsCfg = "",   # the ACTIVE settings.cfg for canon gameplay settings (default: this OS's path)
  [string[]]$Ignore      = @("openmw.cfg","INSTALL.md","tes3mp-client-default.cfg","modlock.json","requiredDataFiles.json","canon-sync.ps1","canon-sync.sh")
)
$ErrorActionPreference = "Stop"
function Sha256([string]$p){ (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLower() }

# no -Canon given -> sync the folder THIS script lives in (self-locate; the script ships inside the
# canon dir, so its own directory IS the canon). $PSScriptRoot is set when run via -File.
if (-not $Canon) { $Canon = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path } }
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
#    Progress ticks every ~10% of the scan so a full 82k-file hash pass never looks hung, and every
#    actual download names its file (so a small update shows exactly what it pulled).
$ok=0; $dl=0; $fail=@(); $seen=0; $nextPct=10; $total=$want.Count
Write-Host ("verifying {0} local files against the manifest (full hash scan)..." -f $total)
foreach ($path in $want.Keys) {
  $seen++
  if ($total -gt 0 -and ($seen * 100 / $total) -ge $nextPct) {
    Write-Host ("  scanned {0}% ({1}/{2}); {3} fetched so far" -f [int]$nextPct, $seen, $total, $dl)
    while ($total -gt 0 -and ($seen * 100 / $total) -ge $nextPct) { $nextPct += 10 }
  }
  $local = Join-Path $Canon ($path -replace '/','\')
  if ((Test-Path -LiteralPath $local) -and ((Sha256 $local) -eq $want[$path])) { $ok++; continue }
  New-Item -ItemType Directory -Force (Split-Path -Parent $local) | Out-Null
  $enc = ($path.Split('/') | ForEach-Object { [Uri]::EscapeDataString($_) }) -join '/'
  Write-Host "  down: $path"
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

# 5. enforce the canon's gameplay settings in the ACTIVE settings.cfg (a section-aware INI, unlike
#    openmw.cfg): keep each [Game] key at the canon value, add if absent, leave the friend's own
#    graphics prefs untouched. Currently: stop NPCs shoving the player around.
if (-not $SettingsCfg) { $SettingsCfg = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'My Games\OpenMW\settings.cfg' }
$wantGame = [ordered]@{ 'NPCs avoid collisions' = 'true'; 'Standing NPCs give way to moving ones' = 'true' }
New-Item -ItemType Directory -Force (Split-Path -Parent $SettingsCfg) | Out-Null
if (Test-Path -LiteralPath $SettingsCfg) {
  $sold = [System.IO.File]::ReadAllText($SettingsCfg)
  $sout = New-Object System.Collections.Generic.List[string]; $inGame = $false; $sdone = @{}
  foreach ($line in ($sold -split "`r`n|`n|`r", -1)) {
    if ($line -match '^\[Game\]') { $inGame = $true; $sout.Add($line); continue }
    if ($line -match '^\[') {
      if ($inGame) { foreach ($k in $wantGame.Keys) { if (-not $sdone.ContainsKey($k)) { $sout.Add("$k = $($wantGame[$k])"); $sdone[$k]=$true } } }
      $inGame = $false; $sout.Add($line); continue
    }
    if ($inGame -and $line -match '=') {
      $k = ($line -split '=',2)[0].Trim()
      if ($wantGame.Contains($k)) { $sout.Add("$k = $($wantGame[$k])"); $sdone[$k]=$true; continue }
    }
    $sout.Add($line)
  }
  if ($inGame) { foreach ($k in $wantGame.Keys) { if (-not $sdone.ContainsKey($k)) { $sout.Add("$k = $($wantGame[$k])"); $sdone[$k]=$true } } }
  if ($sdone.Count -lt $wantGame.Count) { $sout.Add('[Game]'); foreach ($k in $wantGame.Keys) { if (-not $sdone.ContainsKey($k)) { $sout.Add("$k = $($wantGame[$k])") } } }
  $snew = (($sout -join "`n").TrimEnd("`n")) + "`n"
  if ($snew -eq $sold) { Write-Host "gameplay settings already applied." }
  else {
    $st = Get-Date -Format 'yyyyMMdd-HHmmss'
    Copy-Item -LiteralPath $SettingsCfg -Destination "$SettingsCfg.$st.bak" -Force
    [System.IO.File]::WriteAllText($SettingsCfg, $snew, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "gameplay settings applied -> $SettingsCfg (NPC collision avoidance; backup .$st.bak)" -ForegroundColor Green
  }
} else {
  [System.IO.File]::WriteAllText($SettingsCfg, "[Game]`nNPCs avoid collisions = true`nStanding NPCs give way to moving ones = true`n", (New-Object System.Text.UTF8Encoding($false)))
  Write-Host "created settings.cfg with canon gameplay settings -> $SettingsCfg" -ForegroundColor Yellow
}

Remove-Item $tmp -Force -EA SilentlyContinue
Write-Host ("sync: {0} up-to-date, {1} downloaded, {2} removed" -f $ok,$dl,$del)
if ($fail.Count) { Write-Host ("FAILED ({0}): {1}" -f $fail.Count, ($fail -join ', ')) -ForegroundColor Red; exit 1 }
Write-Host "canon in sync." -ForegroundColor Green

# 6. self-update: refresh both canon-sync.* from the server so a script change propagates on its own
#    after the first manual hand-out. Applies on the next run; backfills a missing script; quiet when
#    unchanged. (PowerShell reads the whole script at start, so replacing the file now is safe.)
foreach ($s in @('canon-sync.ps1','canon-sync.sh')) {
  $dst = Join-Path $Canon $s; $dl = Join-Path $env:TEMP "$s.dl"
  curl.exe -fsSL --retry 3 --retry-delay 2 -o $dl "$Base/$s" 2>$null
  if ($LASTEXITCODE -eq 0 -and (Test-Path $dl)) {
    $prev = if (Test-Path -LiteralPath $dst) { [System.IO.File]::ReadAllText($dst) } else { "" }
    if ([System.IO.File]::ReadAllText($dl) -ne $prev) {
      Copy-Item -LiteralPath $dl -Destination $dst -Force
      Write-Host "self-update: $s refreshed (re-run to use the new version)" -ForegroundColor Cyan
    }
    Remove-Item $dl -Force -EA SilentlyContinue
  }
}
