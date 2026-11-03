# SmartCut Windows 桌面构建脚本
# 用法（PowerShell）：
#   conda activate video_edit
#   ./packaging/build_windows.ps1

$ErrorActionPreference = "Stop"

Write-Host "==> 安装桌面/转写/视觉/打包依赖"
python -m pip install -e ".[desktop,transcribe,vision,packaging]"

# GPU 构建前先安装 cu126 torch 与 demucs：
# python -m pip install torch==2.6.0+cu126 --index-url https://download.pytorch.org/whl/cu126
# python -m pip install -e ".[separation]"

Write-Host "==> 准备内嵌 ffmpeg 与 whisper 模型"
python scripts/prepare_vendor.py --source modelscope

Write-Host "==> 准备 Demucs htdemucs 权重"
python scripts/prepare_demucs_models.py

Write-Host "==> PyInstaller one-folder 构建主程序"
python -m PyInstaller packaging/smartcut.spec --noconfirm --clean

Write-Host "==> PyInstaller 构建伴生分离 worker"
python -m PyInstaller packaging/smartcut-separator.spec --noconfirm --clean
$separatorDest = "dist\SmartCut\separator"
if (Test-Path $separatorDest) {
    Remove-Item $separatorDest -Recurse -Force
}
robocopy "dist\SmartCut-Separator" $separatorDest /E /NFL /NDL /NJH /NJS /NP /R:2 /W:2 | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "复制 SmartCut-Separator 失败，robocopy 退出码 $LASTEXITCODE"
}

$smokeData = Join-Path $env:TEMP "smartcut-smoke"
Write-Host "==> 运行打包后的 smoke test"
& "dist\SmartCut\SmartCut.exe" --smoke-test --data-dir $smokeData
if ($LASTEXITCODE -ne 0) {
    throw "SmartCut.exe smoke test 失败，退出码 $LASTEXITCODE"
}

Write-Host "==> 验证内嵌 ffmpeg / whisper 模型"
& "dist\SmartCut\SmartCut.exe" --self-check --data-dir $smokeData
if ($LASTEXITCODE -ne 0) {
    Write-Host "selfcheck.json:"
    Get-Content (Join-Path $smokeData "selfcheck.json") -ErrorAction SilentlyContinue
    throw "SmartCut.exe self-check 失败，退出码 $LASTEXITCODE"
}

Write-Host "构建完成: dist\SmartCut\SmartCut.exe"
