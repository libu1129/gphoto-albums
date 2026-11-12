"""윈도우 전용 도우미: 한 번만 실행, 절전 막기, 네트워크 드라이브를 UNC로, 메시지 상자, 창 아이콘."""
import ctypes
import os
import sys
from ctypes import wintypes

_WIN = sys.platform == "win32"
_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if _WIN else None
_mutex = None


def single_instance(name: str) -> bool:
    """같은 이름의 뮤텍스가 이미 있으면 False(다른 창이 떠 있음)."""
    global _mutex
    if not _WIN:
        return True
    _k32.CreateMutexW.restype = wintypes.HANDLE
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _mutex = _k32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def keep_awake(on: bool) -> None:
    """올리는 동안 절전에 들지 않게 한다. 부른 스레드가 끝나거나 다시 부르면 풀린다."""
    if not _WIN:
        return
    es_continuous, es_system_required = 0x80000000, 0x00000001
    flags = es_continuous | (es_system_required if on else 0)
    _k32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    _k32.SetThreadExecutionState.restype = wintypes.DWORD
    _k32.SetThreadExecutionState(flags)


def to_unc(path: str) -> str:
    """네트워크 드라이브 문자(Z:\\…)면 \\\\서버\\공유\\… 로 바꾼다. 드라이브 문자는 로그온마다 달라질 수 있어서다."""
    if not _WIN or len(path) < 2 or path[1] != ":":
        return path
    drive = path[:2]
    if _k32.GetDriveTypeW(drive + "\\") != 4:  # DRIVE_REMOTE
        return path
    try:
        mpr = ctypes.WinDLL("mpr")
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if mpr.WNetGetConnectionW(drive, buf, ctypes.byref(size)) != 0:
            return path
    except OSError:
        return path
    rest = path[2:].lstrip("\\")
    return buf.value.rstrip("\\") + ("\\" + rest if rest else "")


def message_box(text: str, title: str = "구글 포토 폴더 앨범 올리기") -> None:
    if _WIN:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    else:
        print(text)


def open_path(path: str) -> None:
    if _WIN:
        os.startfile(path)  # noqa: S606 - 사용자가 누른 '폴더 열기'


def set_window_icon(title: str, ico_path: str) -> None:
    """이 프로세스의 창 가운데 제목이 같은 창의 작업 표시줄·제목 표시줄 아이콘을 바꾼다. 못 하면 조용히 넘어간다."""
    if not _WIN or not os.path.exists(ico_path):
        return
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    pid = os.getpid()
    found = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value == title:
                found.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(cb), 0)
    if not found:
        return
    image_icon, load_from_file = 1, 0x10
    big = user32.LoadImageW(None, ico_path, image_icon, 32, 32, load_from_file)
    small = user32.LoadImageW(None, ico_path, image_icon, 16, 16, load_from_file)
    wm_seticon = 0x0080
    for hwnd in found:
        if big:
            user32.SendMessageW(hwnd, wm_seticon, 1, big)
        if small:
            user32.SendMessageW(hwnd, wm_seticon, 0, small)


def set_app_id(app_id: str) -> None:
    """작업 표시줄에서 파이썬과 따로 묶이게 한다."""
    if _WIN:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except (OSError, AttributeError):
            pass
