param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$NoBrowser,
    [string]$Python = $env:WXBOT_PYTHON
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'

if ([string]::IsNullOrWhiteSpace($Python)) {
    $panelVenvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $panelVenvPython) {
        $Python = $panelVenvPython
    } else {
        $panelPythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $panelPythonCommand) { throw '请安装 64 位 Python，或通过 -Python 指定解释器。' }
        $Python = $panelPythonCommand.Source
    }
}
$panelArguments = @('-X', 'utf8', (Join-Path $PSScriptRoot 'web.py'), '--port', $Port)
if ($NoBrowser) { $panelArguments += '--no-browser' }
& $Python @panelArguments
exit $LASTEXITCODE
