"""설치 확인: 라이브러리를 불러오고, 내부 서버가 화면 파일과 상태 API를 내주는지 본다(구글에는 접속하지 않는다).

사용: <설치 폴더>\\.venv\\Scripts\\python.exe <설치 폴더>\\selfcheck.py
"""
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["GPHOTO_ALBUMS_DATA"] = tempfile.mkdtemp(prefix="gphoto-check-")

import google_auth_oauthlib  # noqa: E402,F401
import requests  # noqa: E402,F401
import webview  # noqa: E402,F401

from gphoto import __version__, paths  # noqa: E402
from gphoto.controller import Controller  # noqa: E402
from gphoto.server import AppServer  # noqa: E402

ctl = Controller(paths.data_dir())
srv = AppServer(ctl, HERE / "ui")
srv.start()
try:
    base = f"http://127.0.0.1:{srv.port}"
    html = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")
    assert "구글 포토 폴더 앨범 올리기" in html, "화면 파일을 내주지 못함"
    req = urllib.request.Request(base + "/api/state", headers={"X-App-Token": srv.token})
    state = json.load(urllib.request.urlopen(req, timeout=10))
    assert state["ok"] and state["data"]["version"] == __version__, "상태 API 응답 이상"
    from importlib.metadata import version
    print(f"설치 확인 통과: 버전 {__version__}, pywebview {version('pywebview')}")
finally:
    srv.stop()
    ctl.shutdown()
