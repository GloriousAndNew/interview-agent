$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
# Match only Python commands launched from this project's virtual environment.
# Include the Windows venv launcher and its interpreter child, which share argv.
$taskPythonPrefix = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
    $_.CommandLine -and
    $_.CommandLine.IndexOf($taskPythonPrefix, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
    $_.CommandLine -match '(run\.py|worker\.py|-m\s+streamlit\s+run)'
})
foreach ($taskProcess in $taskProcesses) {
    Stop-Process -Id $taskProcess.ProcessId -ErrorAction SilentlyContinue
}
Write-Output 'Interview Agent stopped. Saved tasks will resume after restart.'
