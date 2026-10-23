param([switch]$SkipTests)
$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Create .venv and install project dependencies first.' }
if (-not $SkipTests) {
    & $taskPython -m pytest -q (Join-Path $taskRoot 'tests')
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; build stopped.' }
}
& $taskPython -m PyInstaller --noconfirm --distpath (Join-Path $taskRoot 'dist') --workpath (Join-Path $taskRoot 'build') (Join-Path $taskRoot 'interview-agent.spec')
if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }
& $taskPython -m PyInstaller --noconfirm --distpath (Join-Path $taskRoot 'dist') --workpath (Join-Path $taskRoot 'build') (Join-Path $taskRoot 'updater.spec')
if ($LASTEXITCODE -ne 0) { throw 'Updater build failed.' }
Copy-Item -LiteralPath (Join-Path $taskRoot 'docs/windows-exe.md') -Destination (Join-Path $taskRoot 'dist/interview-agent/使用说明.md')
& $taskPython (Join-Path $taskRoot 'tools/package_windows.py')
if ($LASTEXITCODE -ne 0) { throw 'Archive packaging failed.' }
Write-Output 'Build ready: dist/interview-agent/interview-agent.exe and dist/interview-agent-v*-windows-x64.zip. Keep the complete folder together.'
