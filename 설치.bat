@echo off
title KREAM 자동입찰 설치
cd /d "%~dp0"
echo ============================================
echo  KREAM 자동입찰 설치
echo  1. 파이썬 가상환경 만들기   2. 패키지 설치
echo  3. .env 준비                4. 바탕화면 바로가기 만들기
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 goto oldpython

set CHROME_OK=0
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set CHROME_OK=1
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set CHROME_OK=1
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set CHROME_OK=1
if "%CHROME_OK%"=="0" echo [주의] 크롬이 보이지 않습니다. 이 프로그램은 설치된 크롬으로 동작하므로 크롬을 먼저 설치하세요: https://www.google.com/chrome/
if "%CHROME_OK%"=="0" echo.

echo [1/4] 가상환경 만드는 중...
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto fail_venv

echo [2/4] 패키지 설치 중 - 1~2분 걸립니다...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail_pip

echo [3/4] .env 준비...
if exist ".env" goto env_ok
copy /y ".env.example" ".env" >nul
echo        .env 를 새로 만들었습니다. 메모장으로 열어 KREAM_ID / KREAM_PW 를 채워주세요.
goto env_done
:env_ok
echo        기존 .env 를 그대로 씁니다.
:env_done

echo [4/4] 바탕화면 바로가기 만드는 중...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\make_shortcut.ps1"

echo.
echo ============================================
echo  설치 완료. 바탕화면의 "KREAM 자동입찰" 을 더블클릭하세요.
echo  첫 실행 때 크롬이 뜨고 .env 의 계정으로 자동 로그인합니다.
echo ============================================
pause
exit /b 0

:nopython
echo [오류] Python 이 설치되어 있지 않습니다.
echo        https://www.python.org/downloads/ 에서 3.10 이상을 설치하세요.
echo        설치 화면 맨 아래 "Add python.exe to PATH" 를 반드시 체크한 뒤 이 파일을 다시 실행하세요.
pause
exit /b 1

:oldpython
echo [오류] Python 3.10 이상이 필요합니다. 현재 버전:
python --version
pause
exit /b 1

:fail_venv
echo [오류] 가상환경 생성에 실패했습니다.
pause
exit /b 1

:fail_pip
echo [오류] 패키지 설치에 실패했습니다. 인터넷 연결을 확인하세요.
pause
exit /b 1
