"""화면(웹 페이지)이 부르는 동작과 상태를 모은다."""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from . import __version__, winutil
from .auth import Auth, LoginError
from .engine import Engine, Progress
from .photos_api import API_BASE, PhotosClient
from .scan import (DAILY_QUOTA, RESUMABLE_MIN, ROOT_TITLE_MAX, ScanCancelled, ScanError,
                   clean_root_title, estimate_requests, normalize_root, root_label, scan)
from .store import Store

log = logging.getLogger("gphoto")

GUIDE_URLS = {
    "project": "https://console.cloud.google.com/projectcreate",
    "library": "https://console.cloud.google.com/apis/library/photoslibrary.googleapis.com",
    "branding": "https://console.cloud.google.com/auth/branding",
    "audience": "https://console.cloud.google.com/auth/audience",
    "scopes": "https://console.cloud.google.com/auth/scopes",      # 데이터 액세스(범위)
    "clients": "https://console.cloud.google.com/auth/clients",
    "photos": "https://photos.google.com/albums",
}


# pywebview FileDialog.OPEN / FOLDER 값과 같다(pywebview 없이도 돌게 숫자로 둔다)
DIALOG_OPEN, DIALOG_FOLDER = 10, 20


class UserError(Exception):
    """화면에 그대로 보여 줄 오류."""


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


class Controller:
    def __init__(self, data_dir, *, auth=None, photos_base=API_BASE, client_kwargs=None,
                 session_factory=None):
        self.data = Path(data_dir)
        self.store = Store(self.data / "state.db")
        self.auth = auth or Auth(self.data)
        self.photos_base = photos_base
        self.client_kwargs = client_kwargs or {}
        self.session_factory = session_factory  # 시험용: 구글 대신 가짜 서버 세션
        self.window = None
        self.allow_close = False
        self._lock = threading.RLock()
        self._settings_path = self.data / "settings.json"
        # guide_step: '열쇠 만드는 법'에서 '마쳤어요'로 간 가장 먼 단계(1~8). 다시 열어도 그 단계부터 보인다
        # root_titles: 최상위 폴더마다 앨범 이름에 쓸 이름(없으면 폴더 이름). 열쇠는 os.path.normcase(root).
        #   폴더를 오가도(예: PC 사본 ↔ NAS) 그 폴더의 이름이 돌아오고, 다른 폴더에 엉뚱한 이름이 붙지 않게 폴더마다 둔다
        self.settings = {"root": "", "include_root": False, "include_videos": True, "guide_step": 1,
                         "root_titles": {}}
        self._load_settings()
        self.scan_state = {"phase": "idle", "scanned": 0, "current": "", "error": None}
        self.plan = None
        self.plan_version = 0
        self.plan_settings = None
        self.selected: set[str] = set()
        self.progress = Progress()
        self.login_state = {"phase": "idle", "error": None}
        self._log = deque(maxlen=600)
        self._log_seq = 0
        self._scan_stop = threading.Event()
        self._stop = threading.Event()
        self._scan_thread = None
        self._upload_thread = None
        self._start_lock = threading.Lock()  # 올리기 시작·훑기 시작이 겹치지 않게(두 번 눌림 대비)
        self._scan_lock = threading.Lock()
        self._login_thread = None
        self.last_seen = time.time()

    # ── 공통 ──
    def log(self, msg: str):
        line = time.strftime("%H:%M:%S ") + msg
        with self._lock:
            self._log.append(line)
            self._log_seq += 1
        log.info(msg)

    def _load_settings(self):
        try:
            d = json.loads(self._settings_path.read_text(encoding="utf-8"))
            for k in self.settings:
                if k in d:
                    self.settings[k] = d[k]
        except (OSError, ValueError):
            pass
        rt = self.settings.get("root_titles")  # 손으로 고쳤거나 저장이 끊긴 파일이어도 시작은 되게
        self.settings["root_titles"] = ({k: v for k, v in rt.items() if isinstance(k, str) and isinstance(v, str) and v}
                                        if isinstance(rt, dict) else {})

    def _save_settings(self):
        try:
            self._settings_path.write_text(json.dumps(self.settings, ensure_ascii=False),
                                           encoding="utf-8")
        except OSError:
            log.exception("설정 저장 실패")

    def _account_key(self):
        """올린 기록의 열쇠: 구글 계정 + 열쇠 파일(클라이언트). 앱이 만든 앨범은 그 클라이언트로만 다룰 수 있어서다."""
        acc = self.auth.account()
        if not acc:
            return None
        who = acc.get("sub") or acc.get("email") or "unknown"
        return f"{who}|{self.auth.client_info().get('client_id') or ''}"

    def uploading(self) -> bool:
        return bool(self._upload_thread and self._upload_thread.is_alive())

    def scanning(self) -> bool:
        return bool(self._scan_thread and self._scan_thread.is_alive())

    def busy(self) -> bool:
        return self.uploading() or self.scanning() or self.login_state["phase"] == "waiting"

    def _root_title(self) -> str:
        """지금 최상위 폴더에 따로 정해 둔 앨범용 이름. 없으면 ''(폴더 이름을 쓴다)."""
        root = self.settings["root"]
        return self.settings["root_titles"].get(os.path.normcase(root), "") if root else ""

    def _title_used(self) -> str:
        """앨범 이름에 실제로 들어갈 최상위 이름(따로 정한 이름, 없으면 폴더 이름)."""
        root = self.settings["root"]
        return self._root_title() or (root_label(root) if root else "")

    def _public_settings(self) -> dict:
        """화면에 보낼 설정: 폴더별 이름 사전 대신 지금 폴더의 이름만. 부를 때 self._lock을 잡고 있어야 한다."""
        s = dict(self.settings)
        s.pop("root_titles", None)
        s["root_label"] = root_label(s["root"]) if s["root"] else ""
        s["root_title"] = self._root_title()
        return s

    def _plan_stale(self) -> bool:
        s = self.settings
        return self.plan is not None and self.plan_settings != (s["root"], s["include_root"],
                                                                 s["include_videos"], self._title_used())

    # ── 상태 ──
    def snapshot(self) -> dict:
        self.last_seen = time.time()
        acc = self.auth.account()
        with self._lock:
            plan = self.plan
            plan_info = None
            if plan is not None:
                top_ext = sorted(plan.skipped_ext.items(), key=lambda kv: -kv[1])[:8]
                plan_info = dict(
                    version=self.plan_version, root=plan.root, folders=len(plan.folders),
                    files=plan.files, photos=plan.photos, videos=plan.videos, bytes=plan.bytes,
                    est_requests=plan.est_requests,
                    est_days=max(1, math.ceil(plan.est_requests / DAILY_QUOTA)),
                    skipped=plan.skipped, skipped_ext=top_ext, examples=plan.examples[:200],
                    errors=plan.errors[:30], n_errors=len(plan.errors), stale=self._plan_stale(),
                    elapsed=round(plan.elapsed, 1))
            sel_files = sel_bytes = 0
            if plan is not None:
                for fp in plan.folders:
                    if fp.key in self.selected:
                        sel_files += len(fp.files)
                        sel_bytes += fp.bytes
            settings = self._public_settings()
            lines = list(self._log)[-200:]
            log_seq = self._log_seq
            n_selected = len(self.selected)
        return dict(
            version=__version__, data_dir=str(self.data), client=self.auth.client_info(),
            account=dict(email=(acc or {}).get("email"), logged_in=bool(acc),
                         problem=self.auth.problem,
                         read_scope=bool(acc and any("readonly.appcreateddata" in s
                                                     for s in acc.get("scopes", [])))),
            login=dict(self.login_state), settings=settings, scan=dict(self.scan_state),
            plan=plan_info, selection=dict(count=n_selected, files=sel_files, bytes=sel_bytes),
            upload=self.progress.snapshot(), log=dict(seq=log_seq, lines=lines),
            busy=self.busy(), uploading=self.uploading())

    def albums(self) -> dict:
        with self._lock:
            plan = self.plan
            selected = set(self.selected)
        if plan is None:
            return dict(version=0, items=[])
        acc = self._account_key()
        status = self.store.folder_status(acc, plan.root) if acc else {}
        live = self.progress.folder_status_copy()
        items = []
        for fp in plan.folders:
            st = status.get(fp.key, {})
            total = len(fp.files)
            albums = st.get("albums", [])
            items.append(dict(
                key=fp.key, title=fp.title, photos=fp.photos, videos=fp.videos, bytes=fp.bytes,
                files=total, parts=fp.parts, warnings=fp.warnings,
                done=min(total, st.get("done", 0)), failed=st.get("failed", 0),
                failed_perm=st.get("failed_perm", 0),
                missing=any(a["missing"] for a in albums),
                album_url=next((a["url"] for a in albums if a.get("url")), None),
                album_titles=[a["title"] for a in albums],
                live=live.get(fp.key), selected=fp.key in selected))
        return dict(version=self.plan_version, items=items)

    def confirm_info(self) -> dict:
        with self._lock:
            plan = self.plan
            selected = set(self.selected)
        if plan is None:
            raise UserError("미리보기를 먼저 만드세요.")
        acc = self.auth.account()
        key = self._account_key()
        pending_files = pending_bytes = new_albums = req = perm = 0
        for fp in plan.folders:
            if fp.key not in selected:
                continue
            items = self.store.item_map(key, plan.root, fp.key) if key else {}
            n = b = big = 0
            for f in fp.files:
                st = items.get((f.path, f.size, f.mtime_ns))
                if st and st["status"] == "done":
                    continue
                if st and st["status"] == "failed_perm":
                    perm += 1
                    continue
                n += 1
                b += f.size
                big += f.size > RESUMABLE_MIN
            has_album = bool(key and self.store.albums_for_folder(key, plan.root, fp.key))
            if n:
                req += estimate_requests(n, big, 0 if has_album else 1)
                new_albums += 0 if has_album else 1
            pending_files += n
            pending_bytes += b
        return dict(email=(acc or {}).get("email") or "", folders=len(selected),
                    files=pending_files, bytes=pending_bytes, bytes_text=fmt_bytes(pending_bytes),
                    new_albums=new_albums, est_requests=req,
                    est_days=max(1, math.ceil(req / DAILY_QUOTA)) if req else 0,
                    skipped_perm=perm)

    # ── 열쇠 파일·로그인 ──
    def _dialog(self, kind, **kw):
        if self.window is None:
            return self._tk_dialog(kind, kw.get("directory", ""))
        res = self.window.create_file_dialog(kind, **kw)
        if not res:
            return None
        return res[0] if isinstance(res, (list, tuple)) else res

    @staticmethod
    def _tk_dialog(kind, directory=""):
        """창(WebView2) 없이 브라우저로 열었을 때 쓰는 대신 고르기 창."""
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError as e:
            raise UserError("파일 고르기 창을 열 수 없습니다. 경로를 직접 넣어 주세요.") from e
        r = tk.Tk()
        r.withdraw()
        r.attributes("-topmost", True)
        try:
            if kind == DIALOG_FOLDER:
                p = filedialog.askdirectory(parent=r, initialdir=directory or None, mustexist=True)
            else:
                p = filedialog.askopenfilename(parent=r, filetypes=[("JSON 파일", "*.json")])
        finally:
            r.destroy()
        return os.path.normpath(p) if p else None

    def choose_client(self, path=None):
        if not path:
            path = self._dialog(DIALOG_OPEN, file_types=("JSON 파일 (*.json)",))
            if not path:
                return self.auth.client_info()
        if self.uploading():
            raise UserError("올리는 중에는 열쇠 파일을 바꿀 수 없습니다.")
        try:
            info = self.auth.import_client(path)
        except LoginError as e:
            raise UserError(str(e)) from e
        self.log(f"열쇠 파일을 등록했습니다(프로젝트 {info.get('project') or '?'}).")
        with self._lock:  # 안내 1~7단계는 끝난 것: 나중에 열쇠가 깨져도 안내가 처음부터 다시 뜨지 않게
            cur = self.settings.get("guide_step")
            if not isinstance(cur, int) or cur < 8:
                self.settings["guide_step"] = 8
                self._save_settings()
        return info

    def open_guide(self, page):
        url = GUIDE_URLS.get(page)
        if not url:
            raise UserError("알 수 없는 안내 페이지")
        webbrowser.open(url)
        return True

    def set_guide_step(self, step):
        try:
            n = int(step)
        except (TypeError, ValueError, OverflowError):
            raise UserError("안내 단계 번호가 잘못됐습니다.") from None
        n = max(1, min(8, n))
        with self._lock:  # 다른 설정 저장과 겹쳐 settings.json이 반쯤 써지지 않게
            self.settings["guide_step"] = n
            self._save_settings()
        return n

    def login(self):
        if self.login_state["phase"] == "waiting":
            return True
        if not self.auth.client_info().get("ok"):
            raise UserError("먼저 열쇠 파일을 고르세요.")
        if self.uploading():
            raise UserError("올리는 중에는 다시 로그인할 수 없습니다.")
        self.login_state = {"phase": "waiting", "error": None}

        def run():
            try:
                acc = self.auth.login()
                self.login_state = {"phase": "idle", "error": None}
                self.log(f"로그인했습니다: {acc.get('email') or '(이메일 모름)'}")
                if not any("readonly.appcreateddata" in s for s in acc.get("scopes", [])):
                    self.log("참고: '앱이 만든 앨범 보기' 권한을 받지 못해, 구글에서 지운 앨범을 알아채지 못할 수 있습니다.")
                self.progress.bump()
            except LoginError as e:
                self.login_state = {"phase": "error", "error": str(e)}
                self.log(str(e))
            except Exception as e:  # noqa: BLE001
                log.exception("로그인 오류")
                self.login_state = {"phase": "error", "error": f"로그인 중 오류: {e}"}

        self._login_thread = threading.Thread(target=run, daemon=True)
        self._login_thread.start()
        return True

    def cancel_login(self):
        self.auth.cancel_login()
        return True

    def logout(self):
        if self.uploading():
            raise UserError("올리는 중에는 로그아웃할 수 없습니다. 먼저 멈추세요.")
        self.auth.logout()
        self.login_state = {"phase": "idle", "error": None}
        self.log("로그아웃했습니다.")
        self.progress.bump()
        return True

    # ── 폴더·설정·미리보기 ──
    def choose_root(self, path=None):
        if not path:
            path = self._dialog(DIALOG_FOLDER, directory=self.settings["root"] or "")
            if not path:
                return self.settings["root"]
        return self.set_root(path)

    def set_root(self, path):
        if self.uploading() or self.scanning():
            raise UserError("올리거나 훑는 중에는 폴더를 바꿀 수 없습니다.")
        path = winutil.to_unc(normalize_root(path))
        if not os.path.isdir(path):
            raise UserError(f"폴더를 찾을 수 없습니다: {path}")
        with self._lock:
            if path != self.settings["root"]:
                self.settings["root"] = path
                self.plan = None
                self.plan_version += 1
                self.selected = set()
                self.scan_state = {"phase": "idle", "scanned": 0, "current": "", "error": None}
            self._save_settings()
        return path

    def set_options(self, include_root=None, include_videos=None, root_title=None):
        """root_title: 지금 최상위 폴더를 앨범 이름에서 부를 이름. ''이거나 폴더 이름과 같으면 폴더 이름을 쓴다."""
        with self._lock:
            titles = None
            if root_title is not None:  # 검사를 먼저 해서, 거절하면 다른 설정도 바꾸지 않는다
                root = self.settings["root"]
                if not root:
                    raise UserError("먼저 사진 폴더를 고르세요.")
                if not isinstance(root_title, str):
                    raise UserError("최상위 폴더 이름이 잘못됐습니다.")
                name = clean_root_title(root_title)
                if len(name) > ROOT_TITLE_MAX:
                    raise UserError(f"최상위 폴더 이름은 {ROOT_TITLE_MAX}자까지 쓸 수 있습니다.")
                titles = dict(self.settings["root_titles"])  # 새 사전으로 바꿔 끼운다(앞서 내보낸 사전은 그대로)
                key = os.path.normcase(root)
                if name and name != root_label(root):
                    titles[key] = name
                else:
                    titles.pop(key, None)
            if include_root is not None:
                self.settings["include_root"] = bool(include_root)
            if include_videos is not None:
                self.settings["include_videos"] = bool(include_videos)
            if titles is not None:
                self.settings["root_titles"] = titles
            self._save_settings()
            return self._public_settings()

    def scan(self):
        if not self._scan_lock.acquire(blocking=False):
            return True
        try:
            return self._scan_locked()
        finally:
            self._scan_lock.release()

    def _scan_locked(self):
        if self.uploading():
            raise UserError("올리는 중에는 미리보기를 다시 만들 수 없습니다.")
        if self.scanning():
            return True
        root = self.settings["root"]
        if not root:
            raise UserError("먼저 사진 폴더를 고르세요.")
        with self._lock:  # 앨범 이름에 닿는 설정을 한 번에 읽어, 낡음 판정과 만든 이름이 같은 값을 기준으로 하게
            inc_root, inc_videos = self.settings["include_root"], self.settings["include_videos"]
            title, title_used = self._root_title(), self._title_used()
        self._scan_stop.clear()
        self.scan_state = {"phase": "scanning", "scanned": 0, "current": root, "error": None}

        def prog(n, d):
            self.scan_state = {"phase": "scanning", "scanned": n, "current": d, "error": None}

        def run():
            try:
                res = scan(root, inc_root, inc_videos, progress=prog, stop=self._scan_stop,
                           root_title=title)
                with self._lock:
                    self.plan = res
                    self.plan_version += 1
                    self.plan_settings = (root, inc_root, inc_videos, title_used)
                    self.selected = set()
                self.scan_state = {"phase": "done", "scanned": res.files, "current": "",
                                   "error": None}
                skipped = sum(res.skipped.values())
                self.log(f"미리보기: 앨범 {len(res.folders):,}개, 파일 {res.files:,}개, "
                         f"{fmt_bytes(res.bytes)}, 제외 {skipped:,}개 ({res.elapsed:.1f}초)")
            except ScanCancelled:
                self.scan_state = {"phase": "idle", "scanned": 0, "current": "", "error": None}
            except ScanError as e:
                self.scan_state = {"phase": "error", "scanned": 0, "current": "", "error": str(e)}
            except Exception as e:  # noqa: BLE001
                log.exception("훑기 오류")
                self.scan_state = {"phase": "error", "scanned": 0, "current": "",
                                   "error": f"폴더를 훑다 오류가 났습니다: {e}"}

        self._scan_thread = threading.Thread(target=run, daemon=True)
        self._scan_thread.start()
        return True

    def select(self, keys=None, on=True, all=None):  # noqa: A002 - 화면 API 이름
        with self._lock:
            if self.plan is None:
                return 0
            if all is not None:
                self.selected = {fp.key for fp in self.plan.folders} if all else set()
            else:
                valid = {fp.key for fp in self.plan.folders}
                for k in keys or []:
                    if k in valid:
                        (self.selected.add if on else self.selected.discard)(k)
            return len(self.selected)

    # ── 올리기 ──
    def start(self):
        if not self._start_lock.acquire(blocking=False):
            raise UserError("이미 올리기를 시작하는 중입니다.")
        try:
            return self._start_locked()
        finally:
            self._start_lock.release()

    def _start_locked(self):
        with self._lock:
            if self.uploading():
                raise UserError("이미 올리는 중입니다.")
            if self.plan is None:
                raise UserError("미리보기를 먼저 만드세요.")
            if self._plan_stale():
                raise UserError("설정이 바뀌었습니다. 미리보기를 다시 만드세요.")
            acc_key = self._account_key()
            if not acc_key:
                raise UserError("먼저 구글에 로그인하세요.")
            folders = [fp for fp in self.plan.folders if fp.key in self.selected]
            if not folders:
                raise UserError("올릴 앨범을 하나 이상 고르세요.")
            root = self.plan.root
        try:
            session = self.session_factory() if self.session_factory else self.auth.session()
        except LoginError as e:
            raise UserError(str(e)) from e
        if session is None:
            self.progress.bump()
            raise UserError(self.auth.problem or "다시 로그인하세요.")
        self._stop.clear()
        self.progress.reset()

        def on_wait(reason, until):
            if self._stop.is_set():
                return
            self.progress.update(wait_reason=reason, wait_until=until,
                                 phase="waiting" if reason else "running")

        client = PhotosClient(session, self.photos_base, stop=self._stop, on_wait=on_wait,
                              on_request=lambda: self.progress.add(requests=1), log=self.log,
                              **self.client_kwargs)
        engine = Engine(client, self.store, acc_key, root, folders, self.progress, self.log,
                        self._stop)
        self._upload_thread = threading.Thread(target=engine.run, daemon=True)
        self._upload_thread.start()
        return True

    def stop(self):
        if self.uploading():
            self._stop.set()
            self.progress.update(phase="stopping", message="멈추는 중입니다(지금 파일을 끝내는 대로)…")
            self.log("멈추기를 눌렀습니다.")
        return True

    def forget_album(self, key):
        if self.uploading():
            raise UserError("올리는 중에는 할 수 없습니다.")
        acc = self._account_key()
        if not acc or self.plan is None:
            raise UserError("로그인과 미리보기가 필요합니다.")
        self.store.forget_folder(acc, self.plan.root, key)
        self.log(f"'{key or self.plan.root_name}' 폴더의 앨범 기록을 지웠습니다. 다음에 올릴 때 앨범을 새로 만듭니다.")
        self.progress.bump()
        return True

    def retry_failed(self):
        acc = self._account_key()
        if not acc or self.plan is None:
            raise UserError("로그인과 미리보기가 필요합니다.")
        self.store.reset_failed(acc, self.plan.root)
        self.log("다시 해도 안 될 것으로 본 실패도 다음 올리기 때 다시 시도합니다.")
        self.progress.bump()
        return True

    def failures(self):
        acc = self._account_key()
        if not acc or self.plan is None:
            return []
        return [dict(path=p, error=e) for p, e in self.store.failed_examples(acc, self.plan.root)]

    def open_target(self, what, key=None):
        if what == "data":
            winutil.open_path(str(self.data))
        elif what == "album":
            acc = self._account_key()
            albums = self.store.albums_for_folder(acc, self.plan.root, key) if acc and self.plan else []
            url = next((a["product_url"] for a in albums if a.get("product_url")), None)
            if not url:
                raise UserError("앨범 주소가 아직 없습니다.")
            webbrowser.open(url)
        elif what == "photos":
            webbrowser.open(GUIDE_URLS["photos"])
        else:
            raise UserError("알 수 없는 대상")
        return True

    # ── 창 ──
    def attach_window(self, window):
        self.window = window

    def on_closing(self):
        """창 닫기 직전. 올리는 중이면 화면에 확인 창을 띄우고 이번 닫기는 막는다."""
        if self.allow_close or not self.uploading():
            self._stop.set()
            self._scan_stop.set()
            self.auth.cancel_login()
            return True
        win = self.window
        if win is not None:
            threading.Thread(target=lambda: win.run_js("window.app && window.app.askClose()"),
                             daemon=True).start()
        return False

    def quit(self):
        self.allow_close = True
        self._stop.set()
        self._scan_stop.set()
        win = self.window
        if win is not None:
            threading.Thread(target=win.destroy, daemon=True).start()
        return True

    def shutdown(self):
        self._stop.set()
        self._scan_stop.set()
        self.auth.cancel_login()
        for t in (self._upload_thread, self._scan_thread):
            if t is not None:
                t.join(timeout=5)
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
