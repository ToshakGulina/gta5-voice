param(
    [string]$NerualDir = "D:\Users\anton\Downloads\NeuroDub_GTAV_FREE_dub",
    [string]$GameDir   = "D:\Games\Grand Theft Auto V.Premium Edition.EGS-Rip\GTAV"
)

chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding  = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:DOTNET_CLI_UI_LANGUAGE = "en"

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = "Continue"

$WorkRoot   = "D:\gta5-voice"
$ToolsDir   = Join-Path $WorkRoot "tools"
$RpfExe     = Join-Path $ToolsDir "RpfTool\bin\Release\net8.0\RpfTool.exe"
$ReportDir  = Join-Path $WorkRoot "reports"
$SampleDir  = Join-Path $WorkRoot "samples"
$EngDir     = Join-Path $SampleDir "english"
$EnhDir     = Join-Path $SampleDir "enhanced_ru"
$VenvDir    = Join-Path $WorkRoot ".venv"
$PipCache   = Join-Path $WorkRoot "pip_cache"
$SeedVcDir  = Join-Path $ToolsDir "seed_vc_core"
$StoryRoot  = Join-Path $NerualDir "NeuroDub_GTAV_FREE_dub_story"

New-Item -ItemType Directory -Force -Path $EnhDir, $PipCache, $SeedVcDir | Out-Null
$env:PIP_CACHE_DIR = $PipCache
$env:HF_HOME       = Join-Path $WorkRoot "checkpoints"
$env:TORCH_HOME    = Join-Path $WorkRoot "checkpoints"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
$env:HF_HUB_DISABLE_PROGRESS_BARS = "1"
$env:TQDM_DISABLE = "1"

Invoke-WebRequest "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main/tools/gta5_awc_tool.py" -UseBasicParsing -OutFile (Join-Path $ToolsDir "gta5_awc_tool.py")
Invoke-WebRequest "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main/tools/gta5_emotion_vc.py" -UseBasicParsing -OutFile (Join-Path $ToolsDir "gta5_emotion_vc.py")

$TxtReport = Join-Path $ReportDir "step6_report.txt"
$Lines = New-Object System.Collections.Generic.List[string]

function Add-Line([string]$s) {
    Write-Host $s
    $Lines.Add($s)
}

function Send-ToGitHub([string]$LocalFile, [string]$RepoPath) {
    $TokenFile = Join-Path $WorkRoot ".token"
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
    $BodyHash = @{ message = "Upload $RepoPath from PC"; content = $B64 }
    if ($Sha) { $BodyHash["sha"] = $Sha }
    $JsonBody = $BodyHash | ConvertTo-Json -Depth 4
    $Resp = Invoke-RestMethod -Method Put -Uri $Uri -Headers $Headers -Body $JsonBody -ContentType "application/json; charset=utf-8" -ErrorAction Stop
    Write-Host "SUCCESS: Uploaded $RepoPath to GitHub!" -ForegroundColor Green
}

Add-Line "=== STEP 6 (MACRO/MICRO INTONATION + DRAMATIC PROLOGUE SCENES) ==="
Add-Line ("Timestamp: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line ""

$VenvPy  = Join-Path $VenvDir "Scripts\python.exe"
$AwcTool = Join-Path $ToolsDir "gta5_awc_tool.py"
$VcScript = Join-Path $ToolsDir "gta5_emotion_vc.py"

# 1. Extract & unpack dramatic Prologue scenes (pro_mcs_2 = vault hostage, pro_mcs_3_pt1 = Brad/Michael shot)
Add-Line "=== 1. EXTRACTING & UNPACKING DRAMATIC PROLOGUE CUTSCENES ==="
$PrologueRpf = Join-Path $GameDir "x64\audio\sfx\PROLOGUE.rpf"
foreach ($awcName in @("pro_mcs_2_mastered_only.awc", "pro_mcs_3_pt1_mastered_only.awc")) {
    $enAwc = Join-Path $EngDir $awcName
    $enUnp = Join-Path $EngDir ($awcName.Replace(".awc", "_unpacked"))
    $extOut = & $RpfExe extract $GameDir $PrologueRpf $awcName $enAwc 2>&1
    Add-Line ("Extract EN [$awcName]: " + ($extOut -join "; "))
    $uEnOut = & $VenvPy $AwcTool unpack $enAwc $enUnp 2>&1
    Add-Line ("Unpack EN [$awcName]: " + ($uEnOut -join "; "))

    $ruAwc = Join-Path $StoryRoot ("x64\audio\sfx\PROLOGUE.rpf\" + $awcName)
    $ruUnp = Join-Path $SampleDir ("ru_" + $awcName.Replace(".awc", "_unpacked"))
    $uRuOut = & $VenvPy $AwcTool unpack $ruAwc $ruUnp 2>&1
    Add-Line ("Unpack RU [$awcName]: " + ($uRuOut -join "; "))
}
Add-Line ""

$Pro2Mp3 = Get-ChildItem -LiteralPath (Join-Path $SampleDir "ru_pro_mcs_2_mastered_only_unpacked") -Filter "*.mp3" | Select-Object -First 1
$Pro3Mp3 = Get-ChildItem -LiteralPath (Join-Path $SampleDir "ru_pro_mcs_3_pt1_mastered_only_unpacked") -Filter "*.mp3" | Select-Object -First 1

$BatchJsonPath = Join-Path $SampleDir "vc_batch.json"
$BatchItems = @(
    @{
        ru  = (Join-Path $SampleDir "ru_engph_unpacked\0x1227C3A3.mp3")
        en  = (Join-Path $EngDir "engph_unpacked\0x1227C3A3.wav")
        out = (Join-Path $EnhDir "engph_0x1227C3A3.wav")
    },
    @{
        ru  = $Pro2Mp3.FullName
        en  = (Join-Path $EngDir ("pro_mcs_2_mastered_only_unpacked\" + $Pro2Mp3.BaseName + ".wav"))
        out = (Join-Path $EnhDir "pro_mcs_2_enhanced.wav")
    },
    @{
        ru  = $Pro3Mp3.FullName
        en  = (Join-Path $EngDir ("pro_mcs_3_pt1_mastered_only_unpacked\" + $Pro3Mp3.BaseName + ".wav"))
        out = (Join-Path $EnhDir "pro_mcs_3_pt1_enhanced.wav")
    }
)
$BatchJson = $BatchItems | ConvertTo-Json -Depth 4
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($BatchJsonPath, $BatchJson, $Utf8NoBom)

Add-Line "=== 2. RUNNING GPU BATCH EMOTION VC ==="
$vcOut = & $VenvPy $VcScript --seed-vc-dir $SeedVcDir --batch-json $BatchJsonPath 2>&1
Add-Line ("VC Batch Output:`n" + ($vcOut -join "`n"))

$Out1 = Join-Path $EnhDir "engph_0x1227C3A3.wav"
$Out2 = Join-Path $EnhDir "pro_mcs_2_enhanced.wav"
$Out3 = Join-Path $EnhDir "pro_mcs_3_pt1_enhanced.wav"

if (Test-Path $Out1) { Send-ToGitHub -LocalFile $Out1 -RepoPath "samples/enhanced_0x1227C3A3.wav" }
if (Test-Path $Out2) { Send-ToGitHub -LocalFile $Out2 -RepoPath "samples/enhanced_pro_mcs_2.wav" }
if (Test-Path $Out3) { Send-ToGitHub -LocalFile $Out3 -RepoPath "samples/enhanced_pro_mcs_3_pt1.wav" }

Add-Line ""
Add-Line "=== END OF STEP 6 REPORT ==="

[System.IO.File]::WriteAllLines($TxtReport, $Lines, $Utf8NoBom)
Send-ToGitHub -LocalFile $TxtReport -RepoPath "reports/step6_report.txt"
