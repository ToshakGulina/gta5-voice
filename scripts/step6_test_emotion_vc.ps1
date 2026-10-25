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

# Download updated files from GitHub
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

Add-Line "=== STEP 6 (HIGH-EXPRESSIVENESS BATCH): RTX 5070 Ti EMOTION VC TEST ==="
Add-Line ("Timestamp: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line ""

$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
$AwcTool = Join-Path $ToolsDir "gta5_awc_tool.py"
$VcScript = Join-Path $ToolsDir "gta5_emotion_vc.py"

# Unpack all 3 Russian sample AWCs from $StoryRoot with Xing VBR headers and log output
Add-Line "=== 1. UNPACKING RUSSIAN AWC SAMPLES (WITH XING VBR HEADERS) ==="
$RuAwcList = @(
    @{ Src = (Join-Path $StoryRoot "x64\audio\sfx\SS_DE.rpf\engph.awc");                        Dst = (Join-Path $SampleDir "ru_engph_unpacked") },
    @{ Src = (Join-Path $StoryRoot "x64\audio\sfx\PROLOGUE.rpf\pro_mcs_1_mastered_only.awc");   Dst = (Join-Path $SampleDir "ru_pro_mcs_1_mastered_only_unpacked") },
    @{ Src = (Join-Path $StoryRoot "x64\audio\sfx\CUTSCENE_MASTERED_ONLY.rpf\ah_1_mcs_1_mastered_only.awc"); Dst = (Join-Path $SampleDir "ru_ah_1_mcs_1_mastered_only_unpacked") }
)
foreach ($item in $RuAwcList) {
    $uOut = & $VenvPy $AwcTool unpack $item.Src $item.Dst 2>&1
    Add-Line ("Unpack [" + (Split-Path $item.Src -Leaf) + "]: " + ($uOut -join "; "))
}
Add-Line ""

Add-Line "=== 2. RUNNING GPU BATCH EMOTION VC ON 4 SAMPLES ==="
$BatchJsonPath = Join-Path $SampleDir "vc_batch.json"
$BatchItems = @(
    @{
        ru  = (Join-Path $SampleDir "ru_engph_unpacked\0x1227C3A3.mp3")
        en  = (Join-Path $EngDir "engph_unpacked\0x1227C3A3.wav")
        out = (Join-Path $EnhDir "engph_0x1227C3A3.wav")
    },
    @{
        ru  = (Join-Path $SampleDir "ru_engph_unpacked\0x0769AE27.mp3")
        en  = (Join-Path $EngDir "engph_unpacked\0x0769AE27.wav")
        out = (Join-Path $EnhDir "engph_0x0769AE27.wav")
    },
    @{
        ru  = (Join-Path $SampleDir "ru_pro_mcs_1_mastered_only_unpacked\0x00E9E7B7.mp3")
        en  = (Join-Path $EngDir "pro_mcs_1_mastered_only_unpacked\0x00E9E7B7.wav")
        out = (Join-Path $EnhDir "pro_mcs_1_0x00E9E7B7.wav")
    },
    @{
        ru  = (Join-Path $SampleDir "ru_ah_1_mcs_1_mastered_only_unpacked\0x182B26EC.mp3")
        en  = (Join-Path $EngDir "ah_1_mcs_1_mastered_only_unpacked\0x182B26EC.wav")
        out = (Join-Path $EnhDir "ah_1_mcs_1_0x182B26EC.wav")
    }
)
$BatchJson = $BatchItems | ConvertTo-Json -Depth 4
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($BatchJsonPath, $BatchJson, $Utf8NoBom)

Write-Host "Running single-load GPU batch on 4 sample files (engph x2, pro_mcs_1, ah_1_mcs_1 35s cutscene)..." -ForegroundColor Cyan
$vcOut = & $VenvPy $VcScript --seed-vc-dir $SeedVcDir --batch-json $BatchJsonPath 2>&1
Add-Line ("VC Batch Output:`n" + ($vcOut -join "`n"))

$Out1 = Join-Path $EnhDir "engph_0x1227C3A3.wav"
$Out2 = Join-Path $EnhDir "engph_0x0769AE27.wav"
$Out3 = Join-Path $EnhDir "pro_mcs_1_0x00E9E7B7.wav"
$Out4 = Join-Path $EnhDir "ah_1_mcs_1_0x182B26EC.wav"

if (Test-Path $Out1) { Send-ToGitHub -LocalFile $Out1 -RepoPath "samples/enhanced_0x1227C3A3.wav" }
if (Test-Path $Out2) { Send-ToGitHub -LocalFile $Out2 -RepoPath "samples/enhanced_0x0769AE27.wav" }
if (Test-Path $Out3) { Send-ToGitHub -LocalFile $Out3 -RepoPath "samples/enhanced_pro_mcs_1.wav" }
if (Test-Path $Out4) { Send-ToGitHub -LocalFile $Out4 -RepoPath "samples/enhanced_ah_1_mcs_1.wav" }

Add-Line ""
Add-Line "=== END OF STEP 6 REPORT ==="

[System.IO.File]::WriteAllLines($TxtReport, $Lines, $Utf8NoBom)
Send-ToGitHub -LocalFile $TxtReport -RepoPath "reports/step6_report.txt"
