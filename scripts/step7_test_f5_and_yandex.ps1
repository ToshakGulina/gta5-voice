$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$WorkRoot  = "D:\gta5-voice"
$ToolsDir  = Join-Path $WorkRoot "tools"
$SampleDir = Join-Path $WorkRoot "samples"
$ReportDir = Join-Path $WorkRoot "reports"
$CkptDir   = Join-Path $WorkRoot "checkpoints"
$VenvPy    = Join-Path $WorkRoot ".venv\Scripts\python.exe"
$TokenFile = Join-Path $WorkRoot ".token"
$RpfTool   = Join-Path $ToolsDir "RpfTool\bin\Release\net8.0\RpfTool.exe"

New-Item -ItemType Directory -Force -Path $ToolsDir, $SampleDir, $ReportDir, $CkptDir | Out-Null

$ReportFile = Join-Path $ReportDir "step7_report.txt"
$Lines = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$text) {
    Write-Host $text
    $Lines.Add($text)
}

Add-Line "=================================================================="
Add-Line "GTA 5 Voice - Step 7: F5-TTS_RUSSIAN (Zero-Shot) + Yandex Lively Voices"
Add-Line "Date: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Add-Line "=================================================================="

# 1. Download updated tools and Yandex Lively Voices samples from GitHub
$RawBase = "https://raw.githubusercontent.com/ToshakGulina/gta5-voice/main"
$Downloads = @(
    @{ Url = "$RawBase/tools/f5_tts_core.zip";                  Out = (Join-Path $ToolsDir "f5_tts_core.zip") },
    @{ Url = "$RawBase/tools/gta5_f5_tts.py";                   Out = (Join-Path $ToolsDir "gta5_f5_tts.py") },
    @{ Url = "$RawBase/tools/gta5_awc_tool.py";                 Out = (Join-Path $ToolsDir "gta5_awc_tool.py") },
    @{ Url = "$RawBase/samples/yandex_lively_0x1227C3A3.wav";   Out = (Join-Path $SampleDir "yandex_lively_0x1227C3A3.wav") },
    @{ Url = "$RawBase/samples/yandex_lively_0x0769AE27.wav";   Out = (Join-Path $SampleDir "yandex_lively_0x0769AE27.wav") },
    @{ Url = "$RawBase/samples/yandex_lively_full.mp3";         Out = (Join-Path $SampleDir "yandex_lively_full.mp3") }
)

foreach ($d in $Downloads) {
    Invoke-WebRequest -Uri $d.Url -OutFile $d.Out -UseBasicParsing
    $sz = (Get-Item $d.Out).Length
    Add-Line "[Download] $(Split-Path $d.Out -Leaf) ($sz bytes)"
}

# 2. Build batch manifest for F5-TTS_RUSSIAN + Yandex Cutscenes Reel
$EnSingleDir = Join-Path $SampleDir "english\engph_unpacked"
$RuSingleDir = Join-Path $SampleDir "nerual\engph_unpacked"

$EnMcs1 = Join-Path $SampleDir "english\pro_mcs_1_mastered_only_unpacked\0x049B266C.wav"
$RuMcs1 = Join-Path $SampleDir "nerual\pro_mcs_1_mastered_only_unpacked\0x049B266C.mp3"

$EnMcs2 = Join-Path $SampleDir "english\pro_mcs_2_mastered_only_unpacked\0x110566DA.wav"
$RuMcs2 = Join-Path $SampleDir "nerual\pro_mcs_2_mastered_only_unpacked\0x110566DA.mp3"

$EnMcs3 = Join-Path $SampleDir "english\pro_mcs_3_pt1_mastered_only_unpacked\0x00E9E7B7.wav"
$RuMcs3 = Join-Path $SampleDir "nerual\pro_mcs_3_pt1_mastered_only_unpacked\0x00E9E7B7.mp3"

$BatchJsonPath = Join-Path $SampleDir "f5_batch_manifest.json"
$BatchReportJson = Join-Path $ReportDir "f5_batch_results.json"
$YandexReelWav = Join-Path $SampleDir "yandex_cutscenes_reel.wav"
$YandexReelMap = Join-Path $SampleDir "yandex_cutscenes_map.json"

$BatchObj = @{
    cache_dir  = $CkptDir
    out_report = $BatchReportJson
    jobs = @(
        @{
            name     = "0x1227C3A3"
            type     = "single"
            en_wav   = (Join-Path $EnSingleDir "0x1227C3A3.wav")
            ru_audio = (Join-Path $RuSingleDir "0x1227C3A3.mp3")
            out_wav  = (Join-Path $SampleDir "f5_0x1227C3A3.wav")
        },
        @{
            name     = "0x0769AE27"
            type     = "single"
            en_wav   = (Join-Path $EnSingleDir "0x0769AE27.wav")
            ru_audio = (Join-Path $RuSingleDir "0x0769AE27.mp3")
            out_wav  = (Join-Path $SampleDir "f5_0x0769AE27.wav")
        },
        @{
            name     = "pro_mcs_1"
            type     = "cutscene"
            en_wav   = $EnMcs1
            ru_audio = $RuMcs1
            out_wav  = (Join-Path $SampleDir "f5_pro_mcs_1.wav")
        },
        @{
            name     = "pro_mcs_2"
            type     = "cutscene"
            en_wav   = $EnMcs2
            ru_audio = $RuMcs2
            out_wav  = (Join-Path $SampleDir "f5_pro_mcs_2.wav")
        },
        @{
            name     = "pro_mcs_3_pt1"
            type     = "cutscene"
            en_wav   = $EnMcs3
            ru_audio = $RuMcs3
            out_wav  = (Join-Path $SampleDir "f5_pro_mcs_3_pt1.wav")
        }
    )
    yandex_reel = @{
        out_wav = $YandexReelWav
        out_map = $YandexReelMap
        items   = @(
            @{ name = "pro_mcs_1";     wav_path = $EnMcs1 },
            @{ name = "pro_mcs_2";     wav_path = $EnMcs2 },
            @{ name = "pro_mcs_3_pt1"; wav_path = $EnMcs3 }
        )
    }
}

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($BatchJsonPath, ($BatchObj | ConvertTo-Json -Depth 6), $Utf8NoBom)

# 3. Run F5-TTS_RUSSIAN batch synthesis + Yandex Cutscenes Reel builder
Add-Line ""
Add-Line "--- Running Misha24-10/F5-TTS_RUSSIAN (F5TTS_v1_Base_v2) on RTX 5070 Ti ---"
$env:TQDM_DISABLE = "1"
$env:HF_HUB_DISABLE_PROGRESS_BARS = "1"
$F5Script = Join-Path $ToolsDir "gta5_f5_tts.py"

$F5Start = Get-Date
$F5Out = & $VenvPy $F5Script batch $BatchJsonPath --cache-dir $CkptDir 2>&1
$F5Exit = $LASTEXITCODE
$F5Elapsed = [Math]::Round(((Get-Date) - $F5Start).TotalSeconds, 1)

foreach ($line in $F5Out) {
    Add-Line "  $line"
}
Add-Line "[F5-TTS] ExitCode=$F5Exit, Elapsed=${F5Elapsed}s"

# 4. Repack Prologue cutscenes with F5-TTS audio and inject into rpf_test\PROLOGUE.rpf
$AwcTool = Join-Path $ToolsDir "gta5_awc_tool.py"
$TestRpf = Join-Path $SampleDir "rpf_test\PROLOGUE.rpf"

$CutsceneRepacks = @(
    @{
        Name    = "pro_mcs_1_mastered_only.awc"
        OrigAwc = (Join-Path $SampleDir "english\pro_mcs_1_mastered_only.awc")
        UnpDir  = (Join-Path $SampleDir "english\pro_mcs_1_mastered_only_unpacked")
        F5Wav   = (Join-Path $SampleDir "f5_pro_mcs_1.wav")
        Targets = @("0x049B266C.wav", "0x026FE200.wav")
        OutAwc  = (Join-Path $SampleDir "repacked_f5_pro_mcs_1_mastered_only.awc")
    },
    @{
        Name    = "pro_mcs_2_mastered_only.awc"
        OrigAwc = (Join-Path $SampleDir "english\pro_mcs_2_mastered_only.awc")
        UnpDir  = (Join-Path $SampleDir "english\pro_mcs_2_mastered_only_unpacked")
        F5Wav   = (Join-Path $SampleDir "f5_pro_mcs_2.wav")
        Targets = @("0x110566DA.wav")
        OutAwc  = (Join-Path $SampleDir "repacked_f5_pro_mcs_2_mastered_only.awc")
    },
    @{
        Name    = "pro_mcs_3_pt1_mastered_only.awc"
        OrigAwc = (Join-Path $SampleDir "english\pro_mcs_3_pt1_mastered_only.awc")
        UnpDir  = (Join-Path $SampleDir "english\pro_mcs_3_pt1_mastered_only_unpacked")
        F5Wav   = (Join-Path $SampleDir "f5_pro_mcs_3_pt1.wav")
        Targets = @("0x00E9E7B7.wav")
        OutAwc  = (Join-Path $SampleDir "repacked_f5_pro_mcs_3_pt1_mastered_only.awc")
    }
)

Add-Line ""
Add-Line "--- Repacking .awc & In-Place RPF Injection ---"
foreach ($c in $CutsceneRepacks) {
    if (Test-Path $c.F5Wav) {
        $RepDir = Join-Path $SampleDir ("f5_repack_" + [System.IO.Path]::GetFileNameWithoutExtension($c.Name))
        New-Item -ItemType Directory -Force -Path $RepDir | Out-Null
        Copy-Item -Path (Join-Path $c.UnpDir "*") -Destination $RepDir -Force
        foreach ($t in $c.Targets) {
            Copy-Item -Path $c.F5Wav -Destination (Join-Path $RepDir $t) -Force
        }
        $RepOut = & $VenvPy $AwcTool repack $c.OrigAwc $RepDir $c.OutAwc 2>&1
        foreach ($l in $RepOut) { Add-Line "  $l" }
        if ((Test-Path $TestRpf) -and (Test-Path $c.OutAwc)) {
            $InjOut = & $RpfTool inject $TestRpf "." $c.OutAwc $c.Name 2>&1
            foreach ($l in $InjOut) { Add-Line "  $l" }
        }
    }
}

# Save report locally
[System.IO.File]::WriteAllText($ReportFile, ($Lines -join "`r`n"), $Utf8NoBom)

# 5. Upload report + F5 samples + Yandex Cutscenes Reel to GitHub
if (Test-Path $TokenFile) {
    $Token = (Get-Content $TokenFile -Raw).Trim()
    $Headers = @{
        Authorization = "Bearer $Token"
        Accept        = "application/vnd.github+json"
        "User-Agent"  = "gta5-voice-step7"
    }

    function Upload-GitHubFile([string]$LocalPath, [string]$RemotePath, [string]$Msg) {
        if (-not (Test-Path $LocalPath)) { return }
        $Uri = "https://api.github.com/repos/ToshakGulina/gta5-voice/contents/$RemotePath"
        $Sha = $null
        try {
            $Existing = Invoke-RestMethod -Uri $Uri -Headers $Headers -Method Get -ErrorAction Stop
            $Sha = $Existing.sha
        } catch {}
        $Bytes = [System.IO.File]::ReadAllBytes($LocalPath)
        $B64   = [Convert]::ToBase64String($Bytes)
        $Body  = @{ message = $Msg; content = $B64 }
        if ($Sha) { $Body.sha = $Sha }
        $JsonBody = $Body | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri $Uri -Headers $Headers -Method Put -Body $JsonBody -ContentType "application/json" | Out-Null
        Write-Host "[GitHub] Uploaded $RemotePath ($($Bytes.Length) bytes)"
    }

    Upload-GitHubFile $ReportFile "reports/step7_report.txt" "Add Step 7 F5-TTS_RUSSIAN report"
    Upload-GitHubFile $BatchReportJson "reports/f5_batch_results.json" "Add Step 7 F5-TTS transcripts JSON"
    Upload-GitHubFile (Join-Path $SampleDir "f5_0x1227C3A3.wav") "samples/f5_0x1227C3A3.wav" "Add F5-TTS 0x1227C3A3 sample"
    Upload-GitHubFile (Join-Path $SampleDir "f5_0x0769AE27.wav") "samples/f5_0x0769AE27.wav" "Add F5-TTS 0x0769AE27 sample"
    Upload-GitHubFile (Join-Path $SampleDir "f5_pro_mcs_1.wav") "samples/f5_pro_mcs_1.wav" "Add F5-TTS pro_mcs_1 cutscene"
    Upload-GitHubFile (Join-Path $SampleDir "f5_pro_mcs_2.wav") "samples/f5_pro_mcs_2.wav" "Add F5-TTS pro_mcs_2 cutscene"
    Upload-GitHubFile (Join-Path $SampleDir "f5_pro_mcs_3_pt1.wav") "samples/f5_pro_mcs_3_pt1.wav" "Add F5-TTS pro_mcs_3_pt1 cutscene"
    Upload-GitHubFile $YandexReelMap "samples/yandex_cutscenes_map.json" "Add Yandex cutscenes timecode map"
    Upload-GitHubFile $YandexReelWav "samples/yandex_cutscenes_reel.wav" "Add Yandex cutscenes English reel WAV"
}

Write-Host ""
Write-Host "DONE! Step 7 finished. Check D:\gta5-voice\samples\ for yandex_lively_*.wav and f5_*.wav!"
