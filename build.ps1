$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE"
    }
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$venvDir = Join-Path $PSScriptRoot ".venv-package-$stamp"
$buildDir = Join-Path $PSScriptRoot "build-package-$stamp"
$distDir = Join-Path $PSScriptRoot "dist-package-$stamp"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$fallbackPythonExe = Join-Path $PSScriptRoot ".venv-build\Scripts\python.exe"

try {
    Invoke-CheckedCommand { python -m venv $venvDir }
}
catch {
    if (-not (Test-Path $fallbackPythonExe)) {
        throw
    }
    Write-Warning "创建干净虚拟环境失败，改用现有 .venv-build 执行打包：$($_.Exception.Message)"
    $pythonExe = $fallbackPythonExe
}

if ($pythonExe -ne $fallbackPythonExe) {
    Invoke-CheckedCommand { & $pythonExe -m pip install --upgrade pip }
    Invoke-CheckedCommand { & $pythonExe -m pip install -r requirements.txt }
}
Invoke-CheckedCommand { & $pythonExe -m PyInstaller .\AutoOverlay.spec --clean --noconfirm --workpath $buildDir --distpath $distDir }

Write-Host ""
Write-Host "Build complete: $distDir\AutoOverlay\AutoOverlay.exe"
