param([int]$Port = 8501, [switch]$Headless)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    & py -3.13 -m venv (Join-Path $taskRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.13 is required. Install it or create .venv with Python >= 3.11.' }
    & $taskPython -m pip install -r (Join-Path $taskRoot 'requirements.lock.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
    & $taskPython -m pip install --no-deps -e "$taskRoot[dev]"
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
$taskArguments = @((Join-Path $taskRoot 'run.py'), '--port', "$Port")
if ($Headless) { $taskArguments += '--headless' }
& $taskPython @taskArguments
