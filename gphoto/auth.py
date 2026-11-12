"""구글 로그인(OAuth, 데스크톱 앱 방식). 열쇠 파일·토큰은 사용자 자료 폴더에만 둔다.

근거: https://developers.google.com/identity/protocols/oauth2/native-app (루프백 주소, PKCE)
"""
from __future__ import annotations

import base64
import json
import os
import socket
import threading
import urllib.request
from pathlib import Path

SCOPE_APPEND = "https://www.googleapis.com/auth/photoslibrary.appendonly"
SCOPE_READ = "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"
SCOPES = [SCOPE_APPEND, SCOPE_READ, "openid", "https://www.googleapis.com/auth/userinfo.email"]
SUCCESS_TEXT = "로그인을 마쳤습니다. 이 탭을 닫고 '구글 포토 폴더 앨범 올리기' 창으로 돌아가세요."


class LoginError(Exception):
    pass


def _jwt_payload(tok: str) -> dict:
    try:
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError):
        return {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Auth:
    def __init__(self, data_dir: Path):
        self.client_path = Path(data_dir) / "client_secret.json"
        self.token_path = Path(data_dir) / "token.json"
        self.account_path = Path(data_dir) / "account.json"
        self._lock = threading.Lock()
        self._creds = None
        self._login_port = None
        self._cancelled = False
        self.problem = None  # 로그인이 풀린 까닭(화면에 보임)

    # ── 열쇠 파일 ──
    def client_info(self) -> dict:
        if not self.client_path.exists():
            return {"ok": False}
        try:
            d = json.loads(self.client_path.read_text(encoding="utf-8"))["installed"]
            return {"ok": True, "project": d.get("project_id") or "",
                    "client": d["client_id"].split("-")[0][:14], "client_id": d["client_id"]}
        except (OSError, ValueError, KeyError):
            return {"ok": False, "error": "열쇠 파일을 읽지 못했습니다. 다시 고르세요."}

    @staticmethod
    def validate_client(src) -> dict:
        try:
            d = json.loads(Path(src).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise LoginError("JSON 파일이 아닙니다. 구글 클라우드에서 받은 열쇠 파일(client_secret_….json)을 고르세요.")
        if not isinstance(d, dict):
            raise LoginError("열쇠 파일 형식이 아닙니다.")
        if "web" in d and "installed" not in d:
            raise LoginError("'웹 애플리케이션' 유형 열쇠입니다. 유형을 '데스크톱 앱'으로 새로 만들어 받으세요.")
        inst = d.get("installed")
        if not isinstance(inst, dict) or not inst.get("client_id") or not inst.get("client_secret"):
            raise LoginError("데스크톱 앱 열쇠 파일이 아닙니다(installed 항목이 없음).")
        return d

    def import_client(self, src) -> dict:
        d = self.validate_client(src)
        old = self.client_info()
        self.client_path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        new = self.client_info()
        if old.get("ok") and old.get("client") != new.get("client"):
            self.logout(revoke=False)  # 다른 열쇠로 바꾸면 예전 로그인은 못 쓴다
        return new

    # ── 로그인 상태 ──
    def account(self):
        if not self.account_path.exists() or not self.token_path.exists():
            return None
        try:
            return json.loads(self.account_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def credentials(self):
        """저장된 토큰으로 자격 증명을 만든다. 만료됐으면 갱신한다. 못 쓰면 None."""
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        with self._lock:
            if self._creds is None:
                if not self.token_path.exists():
                    return None
                try:
                    self._creds = Credentials.from_authorized_user_file(str(self.token_path))
                except (OSError, ValueError):
                    self.problem = "저장된 로그인을 읽지 못했습니다. 다시 로그인하세요."
                    return None
            c = self._creds
            if not c.valid:
                try:
                    c.refresh(Request())
                    self.token_path.write_text(c.to_json(), encoding="utf-8")
                except RefreshError as e:
                    if getattr(e, "retryable", False):  # 토큰 서버 일시 오류면 로그인을 지우지 않는다
                        raise LoginError("구글 로그인 서버가 잠시 응답하지 않습니다. 잠시 뒤 다시 시도하세요.") from e
                    self.problem = ("로그인이 만료됐습니다. 시험 상태로 만든 앱은 7일마다 다시 로그인해야 합니다. "
                                    "다시 로그인하세요.")
                    self._creds = None
                    try:
                        self.token_path.unlink()
                    except OSError:
                        pass
                    return None
                except Exception as e:  # noqa: BLE001 - 네트워크 끊김 등
                    raise LoginError(f"구글에 연결하지 못했습니다: {e}") from e
            return c

    def session(self):
        from google.auth.transport.requests import AuthorizedSession
        c = self.credentials()
        return AuthorizedSession(c) if c is not None else None

    # ── 로그인·로그아웃 ──
    def login(self, timeout=300) -> dict:
        from google_auth_oauthlib.flow import InstalledAppFlow
        # 구글 동의 화면에서 권한 체크를 일부 끄면 oauthlib가 예외를 내지 않게 하고, 받은 권한을 직접 확인한다.
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        if not self.client_path.exists():
            raise LoginError("먼저 열쇠 파일을 고르세요.")
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_path), SCOPES)
        port = _free_port()
        self._login_port = port
        self._cancelled = False
        try:
            creds = flow.run_local_server(host="localhost", port=port, open_browser=True,
                                          authorization_prompt_message=None,
                                          success_message=SUCCESS_TEXT,
                                          timeout_seconds=timeout, prompt="consent")
        except Exception as e:  # noqa: BLE001
            if self._cancelled:
                raise LoginError("로그인을 취소했습니다.") from e
            text = f"{type(e).__name__} {e}"
            if "Timeout" in text:
                raise LoginError("로그인 시간(5분)이 지났습니다. 다시 눌러 주세요.") from e
            if "access_denied" in text or "AccessDenied" in text:
                raise LoginError("구글 화면에서 허용하지 않았습니다.") from e
            raise LoginError(f"로그인하지 못했습니다: {e}") from e
        finally:
            self._login_port = None
        tok = flow.oauth2session.token or {}
        scope = tok.get("scope") or []
        granted = set(scope.split() if isinstance(scope, str) else scope)
        if SCOPE_APPEND not in granted:
            raise LoginError("'구글 포토에 사진 추가' 권한에 체크하지 않았습니다. 다시 로그인해 권한을 모두 체크하세요.")
        info = _jwt_payload(tok.get("id_token") or getattr(creds, "id_token", None) or "")
        acc = {"email": info.get("email") or "", "sub": info.get("sub") or "",
               "scopes": sorted(granted)}
        with self._lock:
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
            self.account_path.write_text(json.dumps(acc, ensure_ascii=False), encoding="utf-8")
            self._creds = creds
            self.problem = None
        return acc

    def cancel_login(self):
        port = self._login_port
        if not port:
            return
        self._cancelled = True
        try:  # 기다리는 로컬 서버에 오류 응답을 보내 흐름을 끝낸다
            urllib.request.urlopen(f"http://localhost:{port}/?error=cancelled", timeout=3).read()
        except OSError:
            pass

    def logout(self, revoke=True):
        with self._lock:
            if revoke and self.token_path.exists():
                try:
                    import requests
                    data = json.loads(self.token_path.read_text(encoding="utf-8"))
                    t = data.get("refresh_token") or data.get("token")
                    if t:
                        requests.post("https://oauth2.googleapis.com/revoke", params={"token": t},
                                      timeout=10)
                except Exception:  # noqa: BLE001 - 철회는 최선만 다한다
                    pass
            for p in (self.token_path, self.account_path):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            self._creds = None
            self.problem = None
