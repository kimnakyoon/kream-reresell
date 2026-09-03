@echo off
title KREAM 자동입찰 업데이트
cd /d "%~dp0"
echo ============================================
echo  KREAM 자동입찰 업데이트
echo  최신 코드를 받아오고 패키지와 바로가기를 맞춥니다.
echo ============================================
echo.
where git >nul 2>nul
if errorlevel 1 goto nogit
if not exist ".git" goto norepo
git pull --ff-only
if errorlevel 1 goto fail_pull
if not exist ".venv\Scripts\python.exe" python -m venv .venv
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\make_shortcut.ps1"
echo.
echo 업데이트 완료.
pause
exit /b 0

:nogit
echo [오류] Git 이 설치되어 있지 않습니다. https://git-scm.com/download/win 에서 설치하세요.
pause
exit /b 1

:norepo
echo [안내] 이 폴더는 git 으로 받은 폴더가 아닙니다.
echo        압축파일로 설치했다면, 새 압축파일의 src 와 scripts 폴더만 덮어쓰면 됩니다.
pause
exit /b 1

:fail_pull
echo [오류] 코드를 받아오지 못했습니다. 인터넷 연결 또는 로컬 수정 사항을 확인하세요.
pause
exit /b 1
