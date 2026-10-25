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

Invoke-WebRequest "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main/tools/RpfTool/Program.cs" -UseBasicParsing -OutFile (Join-Path $RpfDir "Program.cs")

$TxtReport = Join-Path $ReportDir "step4_report.txt"
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

Add-Line "=== STEP 4: RSXXTEA DECRYPTION + FULL RPF NG REPACK ROUND-TRIP ==="
Add-Line ("Timestamp: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line ""

$PyExe = "C:\Users\anton\AppData\Local\Python\bin\python.exe"
$AwcTool = Join-Path $ToolsDir "gta5_awc_tool.py"

# 1. Rebuild RpfTool with RSXXTEA AWC decryption + NG LUT caching
Push-Location $RpfDir
$bOut = & dotnet build -c Release --nologo -v q 2>&1
Add-Line ("Build output: " + ($bOut -join "; "))
Pop-Location
$RpfExe = Join-Path $RpfDir "bin\Release\net8.0\RpfTool.exe"

# 2. Re-extract & decrypt the 3 English AWC files and unpack to clean WAVs
Add-Line "=== 1. EXTRACTING & DECRYPTING ENGLISH AWC -> WAV ==="
$Pairs = @(
    @{ Rpf = "x64\audio\sfx\PROLOGUE.rpf";               Awc = "pro_mcs_1_mastered_only.awc" },
    @{ Rpf = "x64\audio\sfx\CUTSCENE_MASTERED_ONLY.rpf"; Awc = "ah_1_mcs_1_mastered_only.awc" },
    @{ Rpf = "x64\audio\sfx\SS_DE.rpf";                  Awc = "engph.awc" }
)

foreach ($p in $Pairs) {
    $rpfFull = Join-Path $GameDir $p.Rpf
    $outAwc  = Join-Path $EngDir $p.Awc
    $extOut  = & $RpfExe extract $GameDir $rpfFull $p.Awc $outAwc 2>&1
    Add-Line ($extOut -join "`n")

    $unpDir = Join-Path $EngDir ($p.Awc.Replace(".awc", "_unpacked"))
    $unpOut = & $PyExe $AwcTool unpack $outAwc $unpDir 2>&1
    Add-Line ("Unpacked WAVs for {0}: {1}" -f $p.Awc, ($unpOut -join "; "))
}
Add-Line ""

# 3. Test Repacking WAV -> AWC -> RPF (with native Rockstar NG encryption!) on a copy of PROLOGUE.rpf
Add-Line "=== 2. TESTING FULL ROUND-TRIP: WAV -> AWC -> PROLOGUE.RPF (NATIVE NG ENCRYPTION) ==="
$OrigPrologueRpf = Join-Path $GameDir "x64\audio\sfx\PROLOGUE.rpf"
$TestPrologueRpf = Join-Path $SampleDir "test_PROLOGUE.rpf"
Copy-Item -LiteralPath $OrigPrologueRpf -Destination $TestPrologueRpf -Force

$EngProAwc    = Join-Path $EngDir "pro_mcs_1_mastered_only.awc"
$EngProWavDir = Join-Path $EngDir "pro_mcs_1_mastered_only_unpacked"
$RepackedProAwc = Join-Path $SampleDir "repacked_pro_mcs_1_mastered_only.awc"

$repOut = & $PyExe $AwcTool repack $EngProAwc $EngProWavDir $RepackedProAwc 2>&1
Add-Line ("AWC Repack result: " + ($repOut -join "; "))

$injOut = & $RpfExe inject $GameDir $TestPrologueRpf "" $RepackedProAwc 2>&1
Add-Line ("RPF Inject result: " + ($injOut -join "`n"))

# Verify extracting back from test_PROLOGUE.rpf
$VerifyAwc = Join-Path $SampleDir "verify_from_rpf.awc"
$verOut = & $RpfExe extract $GameDir $TestPrologueRpf "repacked_pro_mcs_1_mastered_only.awc" $VerifyAwc 2>&1
Add-Line ("RPF Verify Extract result: " + ($verOut -join "`n"))

# Remove temporary 44MB test RPF after verification
Remove-Item -LiteralPath $TestPrologueRpf -Force -ErrorAction SilentlyContinue

# Upload 1 English WAV sample (0x0769AE27.wav) and 1 Russian MP3 sample (0x0769AE27.mp3) to GitHub
$EngSampleWav = Join-Path $EngDir "engph_unpacked\0x0769AE27.wav"
$RuSampleMp3  = Join-Path $SampleDir "ru_engph_unpacked\0x0769AE27.mp3"
if (Test-Path $EngSampleWav) { Send-ToGitHub -LocalFile $EngSampleWav -RepoPath "samples/en_0x0769AE27.wav" }
if (Test-Path $RuSampleMp3)  { Send-ToGitHub -LocalFile $RuSampleMp3  -RepoPath "samples/ru_0x0769AE27.mp3" }

Add-Line ""
Add-Line "=== END OF STEP 4 REPORT ==="

[System.IO.File]::WriteAllLines($TxtReport, $Lines, [System.Text.Encoding]::UTF8)
Send-ToGitHub -LocalFile $TxtReport -RepoPath "reports/step4_report.txt"
