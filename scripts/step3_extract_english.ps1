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

New-Item -ItemType Directory -Force -Path $RpfDir | Out-Null
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
New-Item -ItemType Directory -Force -Path $EngDir | Out-Null

# Download updated gta5_awc_tool.py and RpfTool sources
$AwcTool = Join-Path $ToolsDir "gta5_awc_tool.py"
Invoke-WebRequest "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main/tools/gta5_awc_tool.py" -UseBasicParsing -OutFile $AwcTool
Invoke-WebRequest "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main/tools/RpfTool/RpfTool.csproj" -UseBasicParsing -OutFile (Join-Path $RpfDir "RpfTool.csproj")
Invoke-WebRequest "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main/tools/RpfTool/Program.cs" -UseBasicParsing -OutFile (Join-Path $RpfDir "Program.cs")

$TxtReport = Join-Path $ReportDir "step3_report.txt"
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

Add-Line "=== STEP 3: BUILD RPFTOOL & EXTRACT ORIGINAL ENGLISH AWC FROM LEGACY ==="
Add-Line ("Timestamp: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line ""

$PyExe = "C:\Users\anton\AppData\Local\Python\bin\python.exe"

# 1. Build RpfTool (.NET 8 + CodeWalker.Core)
Add-Line "=== 1. BUILDING RpfTool (.NET 8 + CodeWalker.Core) ==="
Push-Location $RpfDir
$buildOut = & dotnet build -c Release 2>&1
Add-Line ($buildOut -join "`n")
Pop-Location
$RpfExe = Join-Path $RpfDir "bin\Release\net8.0\RpfTool.exe"
Add-Line ("RpfTool.exe exists: " + (Test-Path $RpfExe))
Add-Line ""

# 2. Extract matching English AWC files from GTA V Legacy RPF archives
Add-Line "=== 2. EXTRACTING ENGLISH AWC FILES FROM GTA V LEGACY ==="
$Pairs = @(
    @{ Rpf = "x64\audio\sfx\PROLOGUE.rpf";               Awc = "pro_mcs_1_mastered_only.awc" },
    @{ Rpf = "x64\audio\sfx\CUTSCENE_MASTERED_ONLY.rpf"; Awc = "ah_1_mcs_1_mastered_only.awc" },
    @{ Rpf = "x64\audio\sfx\SS_DE.rpf";                  Awc = "engph.awc" }
)

foreach ($p in $Pairs) {
    $rpfFull = Join-Path $GameDir $p.Rpf
    $outAwc  = Join-Path $EngDir $p.Awc
    Add-Line ("Extracting {0} from {1}..." -f $p.Awc, $rpfFull)
    $extOut = & $RpfExe extract $GameDir $rpfFull $p.Awc $outAwc 2>&1
    Add-Line ($extOut -join "`n")

    if (Test-Path $outAwc) {
        $infoOut = & $PyExe $AwcTool info $outAwc 2>&1
        Add-Line ("English AWC Info for " + $p.Awc + ":")
        Add-Line ($infoOut -join "`n")

        $unpDir = Join-Path $EngDir ($p.Awc.Replace(".awc", "_unpacked"))
        $unpOut = & $PyExe $AwcTool unpack $outAwc $unpDir 2>&1
        Add-Line ("English Unpack Result: " + ($unpOut -join "; "))

        if ((Get-Item $outAwc).Length -lt 600000) {
            Send-ToGitHub -LocalFile $outAwc -RepoPath ("samples/english_" + $p.Awc)
        }
    }
}

Add-Line ""
Add-Line "=== 3. UNPACKING NERUAL RUSSIAN AWC WITH UPDATED MP3 SUPPORT ==="
$StoryRoot = Join-Path $NerualDir "NeuroDub_GTAV_FREE_dub_story"
foreach ($p in $Pairs) {
    $ruAwc = Join-Path (Join-Path $StoryRoot $p.Rpf) $p.Awc
    if (Test-Path $ruAwc) {
        $ruUnpDir = Join-Path $SampleDir ("ru_" + $p.Awc.Replace(".awc", "_unpacked"))
        $ruUnpOut = & $PyExe $AwcTool unpack $ruAwc $ruUnpDir 2>&1
        Add-Line ("Russian Unpack ({0}): {1}" -f $p.Awc, ($ruUnpOut -join "; "))
    }
}

Add-Line ""
Add-Line "=== END OF STEP 3 REPORT ==="

[System.IO.File]::WriteAllLines($TxtReport, $Lines, [System.Text.Encoding]::UTF8)
Send-ToGitHub -LocalFile $TxtReport -RepoPath "reports/step3_report.txt"
