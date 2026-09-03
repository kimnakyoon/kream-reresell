# 바탕화면에 "KREAM 자동입찰" 바로가기를 만든다 (이 폴더 위치 기준).
param(
    [string]$Name = "KREAM 자동입찰"
)
$proj = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$desk = [Environment]::GetFolderPath('Desktop')
$pythonw = Join-Path $proj ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { Write-Error "가상환경이 없습니다: $pythonw (설치.bat 을 먼저 실행)"; exit 1 }
$lnk = Join-Path $desk "$Name.lnk"
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $pythonw
$s.Arguments = "`"$proj\scripts\gui.pyw`""
$s.WorkingDirectory = $proj
$s.IconLocation = "$pythonw,0"
$s.Description = "KREAM 자동입찰 (창고보관 구매입찰)"
$s.Save()
Write-Output "바로가기 생성: $lnk"
