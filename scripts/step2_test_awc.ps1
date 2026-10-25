param(
    [string]$NerualDir = "D:\Users\anton\Downloads\NeuroDub_GTAV_FREE_dub",
    [string]$GameDir   = "D:\Games\Grand Theft Auto V.Premium Edition.EGS-Rip\GTAV"
)

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = "Continue"

$WorkRoot = "D:\gta5-voice"
$ToolsDir = Join-Path $WorkRoot "tools"
$ReportDir = Join-Path $WorkRoot "reports"
$SampleDir = Join-Path $WorkRoot "samples"
New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
New-Item -ItemType Directory -Force -Path $SampleDir | Out-Null

# Download latest gta5_awc_tool.py
$AwcTool = Join-Path $ToolsDir "gta5_awc_tool.py"
Invoke-WebRequest "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main/tools/gta5_awc_tool.py" -UseBasicParsing -OutFile $AwcTool

$TxtReport = Join-Path $ReportDir "step2_report.txt"
$Lines = New-Object System.Collections.Generic.List[string]

function Add-Line([string]$s) {
    Write-Host $s
    $Lines.Add($s)
}

function Send-ToGitHub([string]$LocalFile, [string]$RepoPath) {
    $TokenFile = Join-Path $WorkRoot ".token"
    if (!(Test-Path $TokenFile)) {
        Write-Host "ERROR: Token file not found at $TokenFile" -ForegroundColor Red
        return
    }
    $Token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
    $Uri = "https://api.github.com/repos/ToshakGulina/gta5-voice/contents/$RepoPath"
    $Headers = @{
        "Authorization" = "Bearer $Token"
        "Accept"        = "application/vnd.github+json"
        "User-Agent"    = "GTA5-Voice-Inspector"
    }

    $Sha = $null
    try {
        $Existing = Invoke-RestMethod -Method Get -Uri $Uri -Headers $Headers -ErrorAction Stop
        $Sha = $Existing.sha
    } catch {}

    $Bytes = [System.IO.File]::ReadAllBytes($LocalFile)
    $B64 = [Convert]::ToBase64String($Bytes)
    $BodyHash = @{
        message = "Upload $RepoPath from PC"
        content = $B64
    }
    if ($Sha) {
        $BodyHash["sha"] = $Sha
    }
    $JsonBody = $BodyHash | ConvertTo-Json -Depth 4
    $Resp = Invoke-RestMethod -Method Put -Uri $Uri -Headers $Headers -Body $JsonBody -ContentType "application/json; charset=utf-8" -ErrorAction Stop
    Write-Host "SUCCESS: Uploaded $RepoPath to GitHub!" -ForegroundColor Green
}

Add-Line "=== STEP 2: AWC UNPACK/REPACK & INSTALLER STRUCTURE TEST ==="
Add-Line ("Timestamp: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line ""

$PyExe = "C:\Users\anton\AppData\Local\Python\bin\python.exe"
if (!(Test-Path $PyExe)) {
    $PyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($PyCmd) { $PyExe = $PyCmd.Source }
}
Add-Line ("Using Python: " + $PyExe)
$DotnetOut = & dotnet --list-sdks 2>&1
Add-Line ("Dotnet SDKs: " + ($DotnetOut -join "; "))
$DotnetRuntimes = & dotnet --list-runtimes 2>&1
Add-Line ("Dotnet Runtimes: " + ($DotnetRuntimes -join "; "))
Add-Line ""

# 1. Breakdown of folders in NeuroDub_GTAV_FREE_dub_story
Add-Line "=== 1. FOLDER BREAKDOWN IN NeuroDub_GTAV_FREE_dub_story ==="
$StoryRoot = Join-Path $NerualDir "NeuroDub_GTAV_FREE_dub_story"
if (Test-Path $StoryRoot) {
    $SubDirs = Get-ChildItem -LiteralPath $StoryRoot -Recurse -Directory
    foreach ($d in $SubDirs) {
        $awcFiles = @(Get-ChildItem -LiteralPath $d.FullName -File -ErrorAction SilentlyContinue)
        if ($awcFiles.Count -gt 0) {
            $totalMB = [math]::Round((($awcFiles | Measure-Object -Property Length -Sum).Sum) / 1MB, 2)
            $sampleNames = ($awcFiles | Select-Object -First 5 | ForEach-Object { "{0} ({1} KB)" -f $_.Name, [math]::Round($_.Length/1KB, 1) }) -join ", "
            Add-Line ("Folder: {0} -> {1} files ({2} MB). Examples: {3}" -f $d.FullName.Substring($StoryRoot.Length), $awcFiles.Count, $totalMB, $sampleNames)
        }
    }
}
Add-Line ""

# 2. Scan NeuroDub_GTAV_FREE_dub.exe for tool names (.exe, .dll, .ps1, CodeWalker, rpf)
Add-Line "=== 2. SCANNING NeuroDub_GTAV_FREE_dub.exe FOR EMBEDDED TOOLS ==="
$SetupExe = Join-Path $NerualDir "NeuroDub_GTAV_FREE_dub.exe"
if (Test-Path $SetupExe) {
    $bytes = [System.IO.File]::ReadAllBytes($SetupExe)
    $ascii = [System.Text.Encoding]::ASCII.GetString($bytes)
    $uni   = [System.Text.Encoding]::Unicode.GetString($bytes)

    $matchesAscii = [regex]::Matches($ascii, "[a-zA-Z0-9_\-\.\\\/]{3,40}\.(exe|dll|ps1|bat|dat|rpf|awc)") | ForEach-Object { $_.Value } | Select-Object -Unique
    Add-Line ("ASCII filenames in Setup.exe: " + (($matchesAscii | Select-Object -First 60) -join ", "))

    $matchesUni = [regex]::Matches($uni, "[a-zA-Z0-9_\-\.\\\/]{3,40}\.(exe|dll|ps1|bat|dat|rpf|awc)") | ForEach-Object { $_.Value } | Select-Object -Unique
    Add-Line ("UTF16 filenames in Setup.exe: " + (($matchesUni | Select-Object -First 60) -join ", "))
}
Add-Line ""

# 3. Test gta5_awc_tool.py on 1 Cutscene AWC and 1 Single/Mission AWC from Nerual
Add-Line "=== 3. TESTING gta5_awc_tool.py ON REAL NERUAL AWC FILES ==="
$TestCandidates = @()
$PrologueDir = Join-Path $StoryRoot "x64\audio\sfx\PROLOGUE.rpf"
if (Test-Path $PrologueDir) {
    $pFile = Get-ChildItem -LiteralPath $PrologueDir -Filter "*.awc" | Sort-Object Length | Select-Object -First 1
    if ($pFile) { $TestCandidates += $pFile }
}
$CutDir = Join-Path $StoryRoot "x64\audio\sfx\CUTSCENE_MASTERED_ONLY.rpf"
if (Test-Path $CutDir) {
    $cFile = Get-ChildItem -LiteralPath $CutDir -Filter "*.awc" | Where-Object { $_.Length -gt 50000 -and $_.Length -lt 400000 } | Select-Object -First 1
    if ($cFile) { $TestCandidates += $cFile }
}
$SsDir = Join-Path $StoryRoot "x64\audio\sfx\SS_DE.rpf"
if (Test-Path $SsDir) {
    $sFile = Get-ChildItem -LiteralPath $SsDir -Filter "*.awc" | Where-Object { $_.Length -gt 20000 -and $_.Length -lt 400000 } | Select-Object -First 1
    if ($sFile) { $TestCandidates += $sFile }
}

foreach ($tf in $TestCandidates) {
    Add-Line ("--- Testing file: {0} ({1} bytes) ---" -f $tf.FullName, $tf.Length)
    $infoOut = & $PyExe $AwcTool info $tf.FullName 2>&1
    Add-Line ($infoOut -join "`n")

    $unpackOutDir = Join-Path $SampleDir ($tf.BaseName + "_unpacked")
    $unpackOut = & $PyExe $AwcTool unpack $tf.FullName $unpackOutDir 2>&1
    Add-Line ("Unpack result: " + ($unpackOut -join "; "))

    $repackFile = Join-Path $SampleDir ($tf.BaseName + "_repacked.awc")
    $repackOut = & $PyExe $AwcTool repack $tf.FullName $unpackOutDir $repackFile 2>&1
    Add-Line ("Repack result: " + ($repackOut -join "; "))

    # Upload small sample AWC (< 500 KB) to GitHub so assistant can inspect it directly
    if ($tf.Length -lt 500000) {
        try {
            Send-ToGitHub -LocalFile $tf.FullName -RepoPath ("samples/" + $tf.Name)
        } catch {
            Add-Line ("Upload sample note: " + $_.Exception.Message)
        }
    }
}

Add-Line ""
Add-Line "=== END OF STEP 2 REPORT ==="

[System.IO.File]::WriteAllLines($TxtReport, $Lines, [System.Text.Encoding]::UTF8)
Send-ToGitHub -LocalFile $TxtReport -RepoPath "reports/step2_report.txt"
