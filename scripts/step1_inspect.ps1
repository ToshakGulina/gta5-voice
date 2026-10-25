param(
    [string]$NerualDir = "D:\Users\anton\Downloads\NeuroDub_GTAV_FREE_dub",
    [string]$GameDir   = "D:\Games\Grand Theft Auto V.Premium Edition.EGS-Rip\GTAV"
)

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ReportDir = Join-Path $RepoRoot "reports"
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

$TxtReport = Join-Path $ReportDir "step1_report.txt"
$Lines = New-Object System.Collections.Generic.List[string]

function Add-Line([string]$s) {
    Write-Host $s
    $Lines.Add($s)
}

Add-Line "=== GTA 5 VOICE UPGRADE: STEP 1 INSPECTION ==="
Add-Line ("Timestamp: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line ""

# 1. Disk Free Space
Add-Line "=== 1. DISK SPACE ==="
Get-PSDrive -PSProvider FileSystem | ForEach-Object {
    $freeGB = [math]::Round($_.Free / 1GB, 2)
    $usedGB = [math]::Round($_.Used / 1GB, 2)
    Add-Line ("Drive {0}: Free = {1} GB, Used = {2} GB" -f $_.Name, $freeGB, $usedGB)
}
Add-Line ""

# 2. GPU & System Tools
Add-Line "=== 2. GPU & INSTALLED TOOLS ==="
$smi = Get-Command "nvidia-smi" -ErrorAction SilentlyContinue
if ($smi) {
    $gpuOut = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1
    Add-Line ("GPU: " + ($gpuOut -join "; "))
} else {
    Add-Line "GPU: nvidia-smi not found in PATH"
}

foreach ($tool in @("git", "python", "py", "dotnet", "ffmpeg", "7z", "innounp")) {
    $cmd = Get-Command $tool -ErrorAction SilentlyContinue
    if ($cmd) {
        Add-Line ("Tool [{0}]: FOUND at {1}" -f $tool, $cmd.Source)
    } else {
        Add-Line ("Tool [{0}]: not in PATH" -f $tool)
    }
}

# Also check standard 7-Zip / InnoSetup locations
foreach ($p in @(
    "C:\Program Files\7-Zip\7z.exe",
    "C:\Program Files (x86)\7-Zip\7z.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
)) {
    if (Test-Path $p) {
        Add-Line ("Extra path FOUND: " + $p)
    }
}
Add-Line ""

# 3. Inspect Nerual Dreming Folder
Add-Line "=== 3. NERUAL DREMING ARCHIVE FOLDER ==="
Add-Line ("Target Path: " + $NerualDir)
if (Test-Path $NerualDir) {
    $nFiles = Get-ChildItem -LiteralPath $NerualDir -Recurse -Force -ErrorAction SilentlyContinue
    Add-Line ("Total items found: " + $nFiles.Count)
    foreach ($f in ($nFiles | Select-Object -First 200)) {
        if ($f.PSIsContainer) {
            Add-Line ("[DIR]  " + $f.FullName)
        } else {
            $mb = [math]::Round($f.Length / 1MB, 2)
            Add-Line ("[FILE] {0} ({1} MB / {2} bytes)" -f $f.FullName, $mb, $f.Length)
            if ($f.Extension -eq ".exe") {
                try {
                    $ver = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($f.FullName)
                    Add-Line ("       ExeInfo: Product='{0}', Desc='{1}', Version='{2}'" -f $ver.ProductName, $ver.FileDescription, $ver.FileVersion)
                    # Check if Inno Setup signature exists in first 4MB
                    $fs = [System.IO.File]::OpenRead($f.FullName)
                    $bufLen = [Math]::Min($fs.Length, 4194304)
                    $buf = New-Object byte[] $bufLen
                    [void]$fs.Read($buf, 0, $bufLen)
                    $fs.Close()
                    $ascii = [System.Text.Encoding]::ASCII.GetString($buf)
                    if ($ascii -match "Inno Setup Setup Data \(([0-9\.a-zA-Z\s]+)\)") {
                        Add-Line ("       InnoSetup Signature: " + $Matches[0])
                    }
                } catch {
                    Add-Line ("       ExeInfo read error: " + $_.Exception.Message)
                }
            }
        }
    }
    if ($nFiles.Count -gt 200) {
        Add-Line ("... (truncated, total items: {0})" -f $nFiles.Count)
    }
} else {
    Add-Line "ERROR: NerualDir path does NOT exist! Checking parent folder..."
    $parent = Split-Path -Parent $NerualDir
    if (Test-Path $parent) {
        Get-ChildItem -LiteralPath $parent | Where-Object { $_.Name -match "Neuro|Nerual|GTA|dub|vo" } | ForEach-Object {
            Add-Line ("Candidate in Downloads: {0} ({1})" -f $_.FullName, $_.Length)
        }
    }
}
Add-Line ""

# 4. Inspect GTA V Legacy Folder
Add-Line "=== 4. GTA V LEGACY GAME FOLDER ==="
Add-Line ("Target Path: " + $GameDir)
if (Test-Path $GameDir) {
    $gtaExe = Join-Path $GameDir "GTA5.exe"
    if (Test-Path $gtaExe) {
        $ver = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($gtaExe)
        Add-Line ("GTA5.exe Version: " + $ver.FileVersion + " / ProductVersion: " + $ver.ProductVersion)
    } else {
        Add-Line "WARNING: GTA5.exe not found directly in GameDir"
    }

    $modsDir = Join-Path $GameDir "mods"
    Add-Line ("mods folder exists: " + (Test-Path $modsDir))
    Add-Line ("OpenIV.asi exists:  " + (Test-Path (Join-Path $GameDir "OpenIV.asi")))
    Add-Line ("dinput8.dll exists: " + (Test-Path (Join-Path $GameDir "dinput8.dll")))

    $sfxDir = Join-Path $GameDir "x64\audio\sfx"
    if (Test-Path $sfxDir) {
        $rpfFiles = Get-ChildItem -LiteralPath $sfxDir -Filter "*.rpf"
        Add-Line ("Audio SFX RPF count in x64\audio\sfx: " + $rpfFiles.Count)
        foreach ($rpf in $rpfFiles) {
            $mb = [math]::Round($rpf.Length / 1MB, 2)
            Add-Line ("  [SFX RPF] {0} ({1} MB)" -f $rpf.Name, $mb)
        }
    } else {
        Add-Line "WARNING: x64\audio\sfx not found!"
    }
} else {
    Add-Line "ERROR: GameDir path does NOT exist!"
}

Add-Line ""
Add-Line "=== END OF STEP 1 REPORT ==="

[System.IO.File]::WriteAllLines($TxtReport, $Lines, [System.Text.Encoding]::UTF8)
Write-Host ""
Write-Host "Report saved to: $TxtReport"

# Commit and push automatically if inside git repo
Push-Location $RepoRoot
try {
    git add reports/step1_report.txt
    git commit -m "Add Step 1 inspection report"
    git push
    Write-Host "Successfully pushed report to GitHub!"
} catch {
    Write-Host ("Git push note: " + $_.Exception.Message)
}
Pop-Location
