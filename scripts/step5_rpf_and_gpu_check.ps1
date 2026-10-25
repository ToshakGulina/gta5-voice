param(
    [string]$NerualDir = "D:\Users\anton\Downloads\NeuroDub_GTAV_FREE_dub",
    [string]$GameDir   = "D:\Games\Grand Theft Auto V.Premium Edition.EGS-Rip\GTAV"
)

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = "Continue"

$WorkRoot  = "D:\gta5-voice"
$ToolsDir  = Join-Path $WorkRoot "tools"
$RpfDir    = Join-Path $ToolsDir "RpfTool"
$ReportDir = Join-Path $WorkRoot "reports"
$SampleDir = Join-Path $WorkRoot "samples"
$EngDir    = Join-Path $SampleDir "english"
$RpfTestDir = Join-Path $SampleDir "rpf_test"
$RepackDir  = Join-Path $SampleDir "repacked_awc"

New-Item -ItemType Directory -Force -Path $RpfTestDir, $RepackDir | Out-Null

$TxtReport = Join-Path $ReportDir "step5_report.txt"
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

Add-Line "=== STEP 5: RPF NG ROUND-TRIP (EXACT FILENAME) + RTX 5070 Ti PYTORCH CHECK ==="
Add-Line ("Timestamp: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line ""

$PyExe   = "C:\Users\anton\AppData\Local\Python\bin\python.exe"
$AwcTool = Join-Path $ToolsDir "gta5_awc_tool.py"
$RpfExe  = Join-Path $RpfDir "bin\Release\net8.0\RpfTool.exe"

# 1. Copy PROLOGUE.rpf keeping its EXACT filename (required by Rockstar NG key derivation!)
Add-Line "=== 1. FULL ROUND-TRIP WITH EXACT RPF FILENAME (rpf_test\PROLOGUE.rpf) ==="
$OrigPrologueRpf = Join-Path $GameDir "x64\audio\sfx\PROLOGUE.rpf"
$TestPrologueRpf = Join-Path $RpfTestDir "PROLOGUE.rpf"
Copy-Item -LiteralPath $OrigPrologueRpf -Destination $TestPrologueRpf -Force

$EngProAwc      = Join-Path $EngDir "pro_mcs_1_mastered_only.awc"
$EngProWavDir   = Join-Path $EngDir "pro_mcs_1_mastered_only_unpacked"
$RepackedProAwc = Join-Path $RepackDir "pro_mcs_1_mastered_only.awc"

$repOut = & $PyExe $AwcTool repack $EngProAwc $EngProWavDir $RepackedProAwc 2>&1
Add-Line ("AWC Repack result: " + ($repOut -join "; "))

$injOut = & $RpfExe inject $GameDir $TestPrologueRpf "" $RepackedProAwc 2>&1
Add-Line ("RPF Inject result: " + ($injOut -join "`n"))

# Re-open PROLOGUE.rpf from scratch, extract injected AWC, and unpack back to WAV
$VerifyAwc    = Join-Path $RpfTestDir "verify_pro_mcs_1_mastered_only.awc"
$VerifyWavDir = Join-Path $RpfTestDir "verify_wav"
$verOut = & $RpfExe extract $GameDir $TestPrologueRpf "pro_mcs_1_mastered_only.awc" $VerifyAwc 2>&1
Add-Line ("RPF Verify Extract result: " + ($verOut -join "`n"))

if (Test-Path $VerifyAwc) {
    $vInfo = & $PyExe $AwcTool info $VerifyAwc 2>&1
    Add-Line ("Verified AWC Info after RPF round-trip:`n" + ($vInfo -join "`n"))
    $vUnp  = & $PyExe $AwcTool unpack $VerifyAwc $VerifyWavDir 2>&1
    Add-Line ("Verified WAV Unpack after RPF round-trip: " + ($vUnp -join "; "))
}

# Clean up 44MB test RPF
Remove-Item -LiteralPath $TestPrologueRpf -Force -ErrorAction SilentlyContinue
Add-Line ""

# 2. Check Python environment & RTX 5070 Ti (sm_120) CUDA availability
Add-Line "=== 2. PYTHON & RTX 5070 Ti (BLACKWELL sm_120) CHECK ==="
$pyCheck = & $PyExe -c "import sys; print('Python:', sys.version); 
try:
    import torch
    print('Torch:', torch.__version__, 'CUDA available:', torch.cuda.is_available())
    if torch.cuda.is_available():
        print('Device:', torch.cuda.get_device_name(0), 'Arch list:', torch.cuda.get_arch_list())
        x = torch.randn(1024, 1024, device='cuda') @ torch.randn(1024, 1024, device='cuda')
        print('CUDA MatMul test: PASSED', x.shape)
except Exception as e:
    print('Torch check:', e)
" 2>&1
Add-Line ($pyCheck -join "`n")

# 3. Check HuggingFace Hub connectivity for Plachta/Seed-VC and Misha24-10/F5-TTS_RUSSIAN
Add-Line ""
Add-Line "=== 3. HUGGING FACE MODEL ACCESS CHECK FROM USER PC ==="
$hfUrls = @(
    "https://huggingface.co/api/models/Plachta/Seed-VC",
    "https://huggingface.co/api/models/Misha24-10/F5-TTS_RUSSIAN",
    "https://huggingface.co/api/models/Systran/faster-whisper-large-v3-turbo"
)
foreach ($u in $hfUrls) {
    try {
        $r = Invoke-WebRequest -Uri $u -Method Head -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        Add-Line ("HF OK [{0}]: {1}" -f $r.StatusCode, $u)
    } catch {
        Add-Line ("HF ERROR: {0} -> {1}" -f $u, $_.Exception.Message)
    }
}

Add-Line ""
Add-Line "=== END OF STEP 5 REPORT ==="

[System.IO.File]::WriteAllLines($TxtReport, $Lines, [System.Text.Encoding]::UTF8)
Send-ToGitHub -LocalFile $TxtReport -RepoPath "reports/step5_report.txt"
