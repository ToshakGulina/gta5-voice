param(
    [string]$NerualDir = "D:\Users\anton\Downloads\NeuroDub_GTAV_FREE_dub",
    [string]$GameDir   = "D:\Games\Grand Theft Auto V.Premium Edition.EGS-Rip\GTAV"
)

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = "Continue"

$WorkRoot   = "D:\gta5-voice"
$ToolsDir   = Join-Path $WorkRoot "tools"
$RpfDir     = Join-Path $ToolsDir "RpfTool"
$ReportDir  = Join-Path $WorkRoot "reports"
$SampleDir  = Join-Path $WorkRoot "samples"
$EngDir     = Join-Path $SampleDir "english"
$RpfTestDir = Join-Path $SampleDir "rpf_test"
$EnhDir     = Join-Path $SampleDir "enhanced_ru"
$VenvDir    = Join-Path $WorkRoot ".venv"
$PipCache   = Join-Path $WorkRoot "pip_cache"
$SeedVcDir  = Join-Path $ToolsDir "seed_vc_core"

New-Item -ItemType Directory -Force -Path $RpfTestDir, $EnhDir, $PipCache, $SeedVcDir | Out-Null
$env:PIP_CACHE_DIR = $PipCache
$env:HF_HOME       = Join-Path $WorkRoot "checkpoints"
$env:TORCH_HOME    = Join-Path $WorkRoot "checkpoints"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"

# Download updated files from GitHub
Invoke-WebRequest "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main/tools/RpfTool/Program.cs" -UseBasicParsing -OutFile (Join-Path $RpfDir "Program.cs")
Invoke-WebRequest "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main/tools/seed_vc_core.zip" -UseBasicParsing -OutFile (Join-Path $ToolsDir "seed_vc_core.zip")
Invoke-WebRequest "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main/tools/gta5_emotion_vc.py" -UseBasicParsing -OutFile (Join-Path $ToolsDir "gta5_emotion_vc.py")

Expand-Archive -LiteralPath (Join-Path $ToolsDir "seed_vc_core.zip") -DestinationPath $SeedVcDir -Force

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

Add-Line "=== STEP 6: NATIVE RPF NG INJECTION + RTX 5070 Ti EMOTION VC TEST ==="
Add-Line ("Timestamp: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line ""

# 1. Rebuild RpfTool and test RPF NG injection with "."
Push-Location $RpfDir
$bOut = & dotnet build -c Release --nologo -v q 2>&1
Add-Line ("RpfTool build: " + ($bOut -join "; "))
Pop-Location
$RpfExe = Join-Path $RpfDir "bin\Release\net8.0\RpfTool.exe"

Add-Line "=== 1. TESTING RPF NG INJECTION WITH '.' ROOT ARGUMENT ==="
$OrigPrologueRpf = Join-Path $GameDir "x64\audio\sfx\PROLOGUE.rpf"
$TestPrologueRpf = Join-Path $RpfTestDir "PROLOGUE.rpf"
Copy-Item -LiteralPath $OrigPrologueRpf -Destination $TestPrologueRpf -Force

$RepackedProAwc = Join-Path $SampleDir "repacked_awc\pro_mcs_1_mastered_only.awc"
$injOut = & $RpfExe inject $GameDir $TestPrologueRpf "." $RepackedProAwc 2>&1
Add-Line ("RPF Inject output:`n" + ($injOut -join "`n"))
Remove-Item -LiteralPath $TestPrologueRpf -Force -ErrorAction SilentlyContinue
Add-Line ""

# 2. Setup Python venv on D:\gta5-voice\.venv with PyTorch cu128 (RTX 5070 Ti sm_120)
Add-Line "=== 2. SETTING UP PYTORCH CU128 VENV ON D:\gta5-voice\.venv ==="
$SysPy = "C:\Users\anton\AppData\Local\Python\bin\python.exe"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    Write-Host "Creating virtual environment in $VenvDir ..." -ForegroundColor Cyan
    & $SysPy -m venv $VenvDir
}

Write-Host "Installing PyTorch cu128 (for RTX 5070 Ti) and Seed-VC dependencies (1-2 mins)..." -ForegroundColor Cyan
& $VenvPy -m pip install --upgrade pip -q
$pipTorch = & $VenvPy -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128 -q 2>&1
Add-Line ("pip torch cu128: " + ($pipTorch -join "; "))

$pipDeps = & $VenvPy -m pip install numpy scipy soundfile pyyaml munch einops huggingface_hub transformers hydra-core omegaconf -q 2>&1
Add-Line ("pip deps: " + ($pipDeps -join "; "))

$gpuTest = & $VenvPy -c "import torch; print('Torch:', torch.__version__, 'CUDA:', torch.cuda.is_available(), 'Device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')" 2>&1
Add-Line ("GPU Check: " + ($gpuTest -join "; "))
Add-Line ""

# 3. Run Emotion & Timbre Transfer on 2 real sample files!
Add-Line "=== 3. RUNNING EMOTION & TIMBRE TRANSFER ON SAMPLE FILES ==="
$VcScript = Join-Path $ToolsDir "gta5_emotion_vc.py"

$RuEngph = Join-Path $SampleDir "ru_engph_unpacked\0x0769AE27.mp3"
$EnEngph = Join-Path $EngDir "engph_unpacked\0x0769AE27.wav"
$OutEngph = Join-Path $EnhDir "engph_0x0769AE27.wav"

Write-Host "Converting sample 1/2: engph (0x0769AE27)..." -ForegroundColor Cyan
$vcOut1 = & $VenvPy $VcScript --seed-vc-dir $SeedVcDir --ru $RuEngph --en $EnEngph --out $OutEngph 2>&1
Add-Line ("VC Sample 1 (engph):`n" + ($vcOut1 -join "`n"))

$RuPro = Join-Path $SampleDir "ru_pro_mcs_1_mastered_only_unpacked\0x00E9E7B7.mp3"
$EnPro = Join-Path $EngDir "pro_mcs_1_mastered_only_unpacked\0x00E9E7B7.wav"
$OutPro = Join-Path $EnhDir "pro_mcs_1_0x00E9E7B7.wav"

Write-Host "Converting sample 2/2: pro_mcs_1_mastered_only (0x00E9E7B7)..." -ForegroundColor Cyan
$vcOut2 = & $VenvPy $VcScript --seed-vc-dir $SeedVcDir --ru $RuPro --en $EnPro --out $OutPro 2>&1
Add-Line ("VC Sample 2 (pro_mcs_1):`n" + ($vcOut2 -join "`n"))

if (Test-Path $OutEngph) { Send-ToGitHub -LocalFile $OutEngph -RepoPath "samples/enhanced_0x0769AE27.wav" }
if (Test-Path $OutPro)   { Send-ToGitHub -LocalFile $OutPro   -RepoPath "samples/enhanced_pro_mcs_1.wav" }

Add-Line ""
Add-Line "=== END OF STEP 6 REPORT ==="

[System.IO.File]::WriteAllLines($TxtReport, $Lines, [System.Text.Encoding]::UTF8)
Send-ToGitHub -LocalFile $TxtReport -RepoPath "reports/step6_report.txt"
