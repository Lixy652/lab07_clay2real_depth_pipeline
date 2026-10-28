# 一键复现：按实验指南的参数矩阵跑完整组（Windows PowerShell）
#
#   .\scripts\run_all.ps1                 # 跑 runA~runE 五组
#   .\scripts\run_all.ps1 -Only runA      # 只跑指定组
#   .\scripts\run_all.ps1 -SkipScene      # 不重建白模 .blend
#
# 依赖：Blender 4.5.x（环境变量 BLENDER_EXE 或 --blender 指定）、
#       models/ 下两个权重、vendor/sdcpp-ext/sd-cli.exe
param(
    [string[]]$Only = @("runA", "runB", "runC", "runD", "runE"),
    [switch]$SkipScene
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Py = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$Blender = if ($env:BLENDER_EXE) { $env:BLENDER_EXE } else { "blender" }

# 参数矩阵：A 基准 / B 高分辨率 / C 多步数 / D 低 ControlNet 强度 / E 高 CFG（错误示范）
$Runs = @(
    @{ name = "runA"; res = 512; steps = 4; cfg = 1.2; cn = 0.9; note = "基准：512^2 / 4 步" },
    @{ name = "runB"; res = 768; steps = 4; cfg = 1.2; cn = 0.9; note = "高分辨率：768^2 / 4 步" },
    @{ name = "runC"; res = 512; steps = 8; cfg = 1.2; cn = 0.9; note = "多步数：512^2 / 8 步" },
    @{ name = "runD"; res = 512; steps = 4; cfg = 1.2; cn = 0.5; note = "低控制强度：512^2 / 4 步 / CN 0.5" },
    @{ name = "runE"; res = 512; steps = 4; cfg = 2.5; cn = 0.9; note = "高 CFG 失败组：512^2 / 4 步 / CFG 2.5" }
)

if (-not $SkipScene) {
    Write-Host "== 重建白模场景 ==" -ForegroundColor Cyan
    & $Blender -b --factory-startup -P blender/make_demo_scene.py -- --out assets/demo_white_model.blend --res 768
}

foreach ($r in $Runs) {
    if ($Only -notcontains $r.name) { continue }
    Write-Host ("== {0}: {1} ==" -f $r.name, $r.note) -ForegroundColor Cyan
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    & $Py scripts/run_pipeline.py --name $r.name --res $r.res --steps $r.steps `
        --cfg $r.cfg --control-strength $r.cn --seed 42 --blender $Blender
    $sw.Stop()
    Write-Host ("   wall {0:N1}s" -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green
}

Write-Host "== 汇总结果表 ==" -ForegroundColor Cyan
& $Py scripts/analyze_runs.py
