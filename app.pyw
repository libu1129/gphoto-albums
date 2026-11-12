"""구글 포토 폴더 앨범 올리기 — 시작 파일(바로가기나 더블클릭으로 실행)."""
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

LOCALIZATION = {
    "global.quitConfirmation": "정말 닫을까요?",
    "global.ok": "확인",
    "global.quit": "닫기",
    "global.cancel": "취소",
    "global.saveFile": "파일 저장",
    "windows.fileFilter.allFiles": "모든 파일",
    "windows.fileFilter.otherFiles": "다른 형식",
}


def _setup_logging(data: Path):
    handler = logging.handlers.RotatingFileHandler(data / "app.log", maxBytes=2_000_000,
                                                   backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("pywebview").setLevel(logging.WARNING)
    # pythonw에는 stdout/stderr가 없다. 라이브러리가 print해도 죽지 않게 파일로 돌린다.
    if sys.stdout is None or sys.stderr is None:
        stream = open(data / "console.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream


def _browser_fallback(ctl, srv, log):
    """창(WebView2)을 못 띄우면 기본 브라우저로 연다. 화면이 90초 넘게 안 읽고 일도 없으면 끝낸다."""
    selftest = os.environ.get("GPHOTO_SELFTEST")
    if selftest:  # 설치 확인 중이면 결과만 적고 끝낸다
        Path(selftest).write_text(json.dumps({"fallback": True}), encoding="utf-8")
        ctl.shutdown()
        srv.stop()
        return
    webbrowser.open(srv.url)
    while True:
        time.sleep(5)
        if not ctl.busy() and time.time() - ctl.last_seen > 90:
            log.info("브라우저 화면이 닫혀 끝냅니다.")
            break
    ctl.shutdown()
    srv.stop()


def _selftest(window, out_path, log):
    """설치 확인용: 창이 뜨고 화면이 서버와 주고받는지 적고 닫는다(GPHOTO_SELFTEST=결과 파일)."""
    time.sleep(2.5)
    try:
        res = window.evaluate_js(
            "JSON.stringify({title: document.title, pages: document.querySelectorAll('section.page').length,"
            " account: document.getElementById('accountText').textContent,"
            " offline: !document.getElementById('offline').classList.contains('hidden'),"
            " version: document.getElementById('footVersion').textContent})")
        Path(out_path).write_text(str(res), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        Path(out_path).write_text(json.dumps({"error": repr(e)}), encoding="utf-8")
        log.exception("자가 시험 실패")
    finally:
        window.destroy()


def main():
    from gphoto import APP_TITLE, paths, winutil
    # 두 번째 실행이 로그 파일을 잡지 않게 한 번만 실행 검사를 먼저 한다
    if not winutil.single_instance("Local\\GPhotoAlbums.SingleInstance"):
        winutil.message_box("이미 실행 중입니다. 작업 표시줄에서 창을 찾아 주세요.")
        return
    data = paths.data_dir()
    _setup_logging(data)
    log = logging.getLogger("gphoto")
    winutil.set_app_id("GPhotoAlbums.App")
    from gphoto.controller import Controller
    from gphoto.server import AppServer
    ctl = Controller(data)
    srv = AppServer(ctl, HERE / "ui")
    srv.start()
    log.info("시작: 127.0.0.1:%s", srv.port)
    try:
        import webview
    except Exception:  # noqa: BLE001
        log.exception("pywebview를 불러오지 못해 브라우저로 엽니다")
        return _browser_fallback(ctl, srv, log)

    window = webview.create_window(APP_TITLE, url=srv.url, width=1120, height=860,
                                   min_size=(900, 640), text_select=True,
                                   background_color="#F3F3F3")
    ctl.attach_window(window)
    window.events.closing += ctl.on_closing
    ico = str(HERE / "app.ico")
    window.events.shown += lambda: winutil.set_window_icon(APP_TITLE, ico)
    selftest = os.environ.get("GPHOTO_SELFTEST")
    if selftest:
        window.events.loaded += lambda: threading.Thread(
            target=_selftest, args=(window, selftest, log), daemon=True).start()

    # WebView2 초기화가 실패해도 pywebview는 예외 없이 빈 창으로 남는다(세션 0에서 0x80070578 확인).
    # 화면이 정해진 시간 안에 안 뜨면 창을 닫고 기본 브라우저로 연다.
    failed = threading.Event()

    def watchdog():
        wait = float(os.environ.get("GPHOTO_WEBVIEW_TIMEOUT") or 40)
        if not window.events.loaded.wait(wait):
            log.error("창 화면이 %s초 안에 뜨지 않아 브라우저로 엽니다", wait)
            failed.set()
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                log.exception("빈 창 닫기 실패")

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        webview.start(gui="edgechromium", private_mode=True, storage_path=str(data / "webview"),
                      localization=LOCALIZATION)
    except Exception:  # noqa: BLE001
        log.exception("창을 띄우지 못해 브라우저로 엽니다")
        failed.set()
    if failed.is_set():
        ctl.attach_window(None)
        return _browser_fallback(ctl, srv, log)
    ctl.shutdown()
    srv.stop()
    log.info("끝")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        logging.getLogger("gphoto").exception("시작 실패")
        try:
            from gphoto import winutil as _w
            _w.message_box("프로그램을 시작하지 못했습니다. %LOCALAPPDATA%\\GPhotoAlbums\\app.log 를 확인하세요.")
        except Exception:  # noqa: BLE001
            pass
        raise
