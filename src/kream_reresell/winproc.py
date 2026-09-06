"""Windows 프로세스 다루기 (browser.py 가 크롬을 띄우고 정리할 때 쓴다).

- process_image(pid): 살아 있는 프로세스의 실행 파일 이름 (죽었으면 None). pid 만으로는 재사용됐을 수 있어 이름까지 본다.
- find_profile_chromes(profile): 그 프로필(--user-data-dir)로 떠 있는 크롬 본체 프로세스 목록 (PowerShell 로 명령줄을 읽는다, 0.5초쯤).
- kill_with_this_process(pid): 자식 프로세스를 Job Object 에 넣어 이 python 이 어떻게 죽든(창 닫기, 작업 관리자) 같이 죽게 한다.
- kill_tree(pid): taskkill /T /F.

Windows 가 아니면 전부 아무것도 하지 않는다 (None / 빈 목록 / False).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PYTHON_IMAGES = ("python.exe", "pythonw.exe")
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.windll.kernel32
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]
else:
    _kernel32 = None

_jobs: dict[int, int] = {}   # 자식 pid → Job 핸들 (핸들이 닫히면 그 안의 프로세스가 죽으므로 살아 있는 동안 들고 있는다)


def process_image(pid: int) -> str | None:
    """pid 가 살아 있으면 실행 파일 이름(소문자, 예: 'pythonw.exe'), 죽었거나 없으면 None."""
    if _kernel32 is None or pid <= 0:
        return None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name.lower()
    finally:
        _kernel32.CloseHandle(handle)


def kill_with_this_process(pid: int) -> bool:
    """pid 를 Job Object 에 넣어, 이 python 프로세스가 끝나면(정상 종료·창 닫기·작업 관리자 강제 종료 모두) 같이 죽게 한다.

    크롬 본체가 죽으면 renderer 등 자식들도 스스로 끝난다. 실패하면(정책상 Job 을 못 만드는 환경) False.
    """
    if _kernel32 is None:
        return False
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _kernel32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))
    handle = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid) if ok else None
    if handle:
        ok = _kernel32.AssignProcessToJobObject(job, handle)
        _kernel32.CloseHandle(handle)
    else:
        ok = False
    if not ok:
        log.warning("크롬 프로세스 %d 를 Job Object 에 넣지 못함 (오류 %d) - 이 프로그램이 강제 종료되면 크롬이 남을 수 있음",
                    pid, _kernel32.GetLastError())
        _kernel32.CloseHandle(job)
        return False
    _jobs[pid] = job
    return True


def release(pid: int) -> None:
    """kill_with_this_process 로 넣은 Job 핸들을 닫는다 (아직 살아 있으면 그 자리에서 죽는다)."""
    job = _jobs.pop(pid, None)
    if job and _kernel32 is not None:
        _kernel32.CloseHandle(job)


def kill_tree(pid: int) -> None:
    if sys.platform != "win32":
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=15, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@dataclass(frozen=True)
class ChromeProcess:
    pid: int
    parent_pid: int
    port: int | None      # --remote-debugging-port


_PS_LIST_CHROME = (
    "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
    "Where-Object { $_.CommandLine -like '*--user-data-dir=*' -and $_.CommandLine -notlike '*--type=*' } | "
    "ForEach-Object { [pscustomobject]@{pid=$_.ProcessId; ppid=$_.ParentProcessId; cmd=$_.CommandLine} } | "
    "ConvertTo-Json -Compress"
)
_PORT_RE = re.compile(r"--remote-debugging-port=(\d+)")
_DATA_DIR_RE = re.compile(r'--user-data-dir=(?:"([^"]*)"|(\S+))')


def find_profile_chromes(profile: Path) -> list[ChromeProcess]:
    """profile 을 --user-data-dir 로 쓰는 크롬 본체(--type= 없는) 프로세스들. 조회에 실패하면 빈 목록."""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_LIST_CHROME],
                             capture_output=True, text=True, timeout=30, check=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows = json.loads(out.stdout) if out.stdout.strip() else []
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.warning("크롬 프로세스 목록을 읽지 못함 (%s)", e)
        return []
    if isinstance(rows, dict):
        rows = [rows]
    want = str(profile.resolve()).lower()
    found: list[ChromeProcess] = []
    for row in rows:
        cmd = str(row.get("cmd") or "")
        m = _DATA_DIR_RE.search(cmd)
        data_dir = (m.group(1) or m.group(2) or "") if m else ""
        try:
            if str(Path(data_dir).resolve()).lower() != want:
                continue
        except OSError:
            continue
        port_m = _PORT_RE.search(cmd)
        found.append(ChromeProcess(pid=int(row["pid"]), parent_pid=int(row.get("ppid") or 0),
                                   port=int(port_m.group(1)) if port_m else None))
    return found
