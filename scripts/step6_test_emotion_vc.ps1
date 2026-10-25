$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$WorkDir = "D:\gta5-voice"
$SamplesDir = "$WorkDir\samples"
$ToolsDir = "$WorkDir\tools"
$ReportsDir = "$WorkDir\reports"
$ReportFile = "$ReportsDir\step6_report.txt"
$GameDir = "D:\Games\Grand Theft Auto V.Premium Edition.EGS-Rip\GTAV"
$NerualDir = "D:\Users\anton\Downloads\NeuroDub_GTAV_FREE_dub\NeuroDub_GTAV_FREE_dub_story"

New-Item -ItemType Directory -Force -Path $SamplesDir, $ToolsDir, $ReportsDir | Out-Null

$ReportLines = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$text) {
    Write-Host $text
    $ReportLines.Add($text)
}

Add-Line "=== STEP 6: TRUE CONTOUR FIX + 3-CHANNEL CUTSCENES (PRO_MCS_2 & PRO_MCS_3_PT1) ==="
Add-Line "Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Add-Line ""

# 1. Download updated tools
$RepoRaw = "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main"
foreach ($f in @("tools/gta5_awc_tool.py", "tools/gta5_emotion_vc.py")) {
    $dest = "$WorkDir\$($f -replace '/', '\')"
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Invoke-WebRequest -Uri "$RepoRaw/$f" -OutFile $dest -UseBasicParsing
}

$RpfExe = "$ToolsDir\RpfTool\bin\Release\net8.0\RpfTool.exe"
$ProRpf = "$GameDir\x64\audio\sfx\PROLOGUE.rpf"
$RuProDir = "$NerualDir\update\x64\dlcpacks\patchdayng\dlc.rpf\x64\audio\sfx\prologue.rpf"

# 2. Extract and unpack pro_mcs_2_mastered_only.awc and pro_mcs_3_pt1_mastered_only.awc
foreach ($awcName in @("pro_mcs_2_mastered_only.awc", "pro_mcs_3_pt1_mastered_only.awc")) {
    $base = [System.IO.Path]::GetFileNameWithoutExtension($awcName)
    $enAwc = "$SamplesDir\english\$awcName"
    $enUnp = "$SamplesDir\english\${base}_unpacked"
    $ruAwc = "$RuProDir\$awcName"
    $ruUnp = "$SamplesDir\ru_${base}_unpacked"

    if (-not (Test-Path $enAwc)) {
        $extOut = (& $RpfExe extract $GameDir $ProRpf $awcName $enAwc 2>&1 | Out-String).Trim()
        Add-Line "Extract EN ${awcName}: $extOut"
    }
    if (Test-Path $enUnp) { Remove-Item -Recurse -Force $enUnp }
    $unpEn = (& python "$ToolsDir\gta5_awc_tool.py" unpack $enAwc $enUnp 2>&1 | Out-String).Trim()
    Add-Line "Unpack EN ${awcName}: $unpEn"

    if (Test-Path $ruUnp) { Remove-Item -Recurse -Force $ruUnp }
    $unpRu = (& python "$ToolsDir\gta5_awc_tool.py" unpack $ruAwc $ruUnp 2>&1 | Out-String).Trim()
    Add-Line "Unpack RU ${awcName}: $unpRu"
}

if (-not (Test-Path "$SamplesDir\ru_engph_unpacked\0x1227C3A3.mp3")) {
    $u1 = (& python "$ToolsDir\gta5_awc_tool.py" unpack "$NerualDir\update\x64\dlcpacks\patchdayng\dlc.rpf\x64\audio\sfx\ss_de.rpf\engph.awc" "$SamplesDir\ru_engph_unpacked" 2>&1 | Out-String).Trim()
    Add-Line "Unpack ru_engph: $u1"
}

Add-Line ""
Add-Line "=== 1. RUNNING GPU EMOTION ENHANCEMENT BATCH ==="

$Env:TQDM_DISABLE = "1"
$Env:HF_HUB_DISABLE_PROGRESS_BARS = "1"

$BatchList = @(
    @{
        name = "engph_0x1227C3A3"
        ru   = "$SamplesDir\ru_engph_unpacked\0x1227C3A3.mp3"
        en   = "$SamplesDir\english\engph_unpacked\0x1227C3A3.wav"
        out  = "$SamplesDir\enhanced_0x1227C3A3.wav"
    },
    @{
        name = "pro_mcs_2_cutscene"
        ru   = "$SamplesDir\ru_pro_mcs_2_mastered_only_unpacked"
        en   = "$SamplesDir\english\pro_mcs_2_mastered_only_unpacked"
        out  = "$SamplesDir\enhanced_pro_mcs_2.wav"
    },
    @{
        name = "pro_mcs_3_pt1_cutscene"
        ru   = "$SamplesDir\ru_pro_mcs_3_pt1_mastered_only_unpacked"
        en   = "$SamplesDir\english\pro_mcs_3_pt1_mastered_only_unpacked"
        out  = "$SamplesDir\enhanced_pro_mcs_3_pt1.wav"
    }
)

$BatchJsonPath = "$SamplesDir\vc_batch.json"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($BatchJsonPath, ($BatchList | ConvertTo-Json -Depth 4), $Utf8NoBom)

$Sw = [System.Diagnostics.Stopwatch]::StartNew()
$VcOut = (& python "$ToolsDir\gta5_emotion_vc.py" --batch-json $BatchJsonPath --steps 25 --cfg 0.75 --emotion-scale 1.75 2>&1 | Out-String).Trim()
$Sw.Stop()

Add-Line "GPU Batch elapsed: $([Math]::Round($Sw.Elapsed.TotalSeconds, 2)) sec"
Add-Line $VcOut

foreach ($item in $BatchList) {
    $outFile = $item.out
    if (Test-Path $outFile) {
        $sz = (Get-Item $outFile).Length
        Add-Line "SUCCESS [$($item.name)]: $outFile ($sz bytes)"
    } else {
        Add-Line "ERROR [$($item.name)]: Missing $outFile"
    }
}

Add-Line ""
Add-Line "=== END OF STEP 6 REPORT ==="

[System.IO.File]::WriteAllLines($ReportFile, $ReportLines, [System.Text.Encoding]::UTF8)

# Upload report and enhanced samples to GitHub
$TokenFile = "$WorkDir\.token"
if (Test-Path $TokenFile) {
    $Token = (Get-Content $TokenFile -Raw).Trim()
    $Headers = @{
        "Authorization" = "Bearer $Token"
        "Accept"        = "application/vnd.github+json"
        "User-Agent"    = "GTA5-Voice-Agent"
    }

    function Upload-GitHubFile($LocalPath, $RepoPath) {
        if (-not (Test-Path $LocalPath)) { return }
        $Bytes = [System.IO.File]::ReadAllBytes($LocalPath)
        $B64 = [Convert]::ToBase64String($Bytes)
        $Uri = "https://api.github.com/repos/ToshakGulina/gta5-voice/contents/$RepoPath"
        $Sha = $null
        try {
            $Existing = Invoke-RestMethod -Uri $Uri -Headers $Headers -Method Get -ErrorAction SilentlyContinue
            if ($Existing -and $Existing.sha) { $Sha = $Existing.sha }
        } catch {}
        $BodyObj = @{
            message = "Update $RepoPath (true F0 contour + 3-chan cutscenes)"
            content = $B64
        }
        if ($Sha) { $BodyObj["sha"] = $Sha }
        $BodyJson = $BodyObj | ConvertTo-Json -Depth 4 -Compress
        try {
            Invoke-RestMethod -Uri $Uri -Headers $Headers -Method Put -Body $BodyJson -ContentType "application/json; charset=utf-8" | Out-Null
            Write-Host "Uploaded to GitHub: $RepoPath"
        } catch {
            Write-Host "GitHub upload error for ${RepoPath}: $($_.Exception.Message)"
        }
    }

    Upload-GitHubFile $ReportFile "reports/step6_report.txt"
    Upload-GitHubFile "$SamplesDir\english\engph_unpacked\0x1227C3A3.wav" "samples/en_0x1227C3A3.wav"
    Upload-GitHubFile "$SamplesDir\enhanced_0x1227C3A3.wav" "samples/enhanced_0x1227C3A3.wav"
    Upload-GitHubFile "$SamplesDir\enhanced_pro_mcs_2.wav" "samples/enhanced_pro_mcs_2.wav"
    Upload-GitHubFile "$SamplesDir\enhanced_pro_mcs_3_pt1.wav" "samples/enhanced_pro_mcs_3_pt1.wav"
}
