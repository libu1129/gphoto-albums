"""앨범을 만들고 파일을 올려 넣는 순서를 맡는다."""
from __future__ import annotations

import logging
import threading
import time

from . import winutil
from .photos_api import ApiError, AuthError, FileChanged, StopRequested
from .scan import ALBUM_MAX, BATCH, part_title

log = logging.getLogger("gphoto")

TOKEN_TTL = 20 * 3600   # 문서상 토큰은 1일 유효. 넉넉히 20시간 안의 것만 다시 쓴다
FAIL_STREAK_STOP = 3    # 묶음이 잇달아 이만큼 전부 실패하면 멈춘다(저장 공간 가득 참 같은 경우)


class CircuitOpen(Exception):
    pass


class Progress:
    """올리기 진행 상황. 화면이 0.7~2초마다 읽는다."""

    def __init__(self):
        self._lock = threading.Lock()
        self.status_version = 0
        self.reset()

    def reset(self):
        with self._lock:
            self.phase = "idle"        # idle | running | waiting | stopping | stopped | done | error
            self.message = ""
            self.files_total = self.files_done = self.files_failed = self.files_skipped = 0
            self.bytes_total = self.bytes_done = 0
            self.current = ""
            self.album = ""
            self.wait_reason = None
            self.wait_until = None
            self.started = None
            self.finished = None
            self.requests = 0
            self.albums_created = 0
            self.folder_status = {}
            self.status_version += 1

    def update(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def add(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)

    def set_folder(self, key, status):
        with self._lock:
            self.folder_status[key] = status
            self.status_version += 1

    def bump(self):
        with self._lock:
            self.status_version += 1

    def folder_status_copy(self):
        with self._lock:
            return dict(self.folder_status)

    def snapshot(self):
        with self._lock:
            end = self.finished or time.time()
            elapsed = (end - self.started) if self.started else 0
            speed = self.bytes_done / elapsed if elapsed > 3 else 0
            left = max(0, self.bytes_total - self.bytes_done)
            return dict(phase=self.phase, message=self.message, files_total=self.files_total,
                        files_done=self.files_done, files_failed=self.files_failed,
                        files_skipped=self.files_skipped, bytes_total=self.bytes_total,
                        bytes_done=self.bytes_done, current=self.current, album=self.album,
                        wait_reason=self.wait_reason, wait_until=self.wait_until,
                        started=self.started, finished=self.finished, elapsed=elapsed,
                        speed=speed, eta=(left / speed) if speed > 0 else None,
                        requests=self.requests, albums_created=self.albums_created,
                        status_version=self.status_version)


class Engine:
    def __init__(self, client, store, account, root, folders, progress, logf, stop,
                 retry_failed=False):
        self.client = client
        self.store = store
        self.account = account
        self.root = root
        self.folders = folders
        self.p = progress
        self.log = logf
        self.stop = stop
        self.retry_failed = retry_failed
        self._streak = 0

    def _check_stop(self):
        if self.stop.is_set():
            raise StopRequested()

    def _pending(self, fp):
        items = self.store.item_map(self.account, self.root, fp.key)
        pending = []
        for f in fp.files:
            st = items.get((f.path, f.size, f.mtime_ns))
            if st and st["status"] == "done":
                continue
            if st and st["status"] == "failed_perm" and not self.retry_failed:
                continue
            pending.append((f, st))
        return pending

    def run(self):
        p = self.p
        work, files_total, bytes_total = [], 0, 0
        for fp in self.folders:
            pending = self._pending(fp)
            work.append((fp, pending))
            files_total += len(pending)
            bytes_total += sum(f.size for f, _ in pending)
            p.set_folder(fp.key, "queued" if pending else "done")
        p.update(phase="running", files_total=files_total, bytes_total=bytes_total,
                 started=time.time(), message="")
        self.log(f"올리기 시작: 앨범 {len(work)}개, 남은 파일 {files_total:,}개")
        winutil.keep_awake(True)
        try:
            for fp, pending in work:
                if pending:
                    self._check_stop()
                    self._folder(fp, pending)
            msg = f"다 올렸습니다. 성공 {p.files_done:,}개"
            if p.files_failed:
                msg += f", 실패 {p.files_failed:,}개(기록 참고)"
            if p.files_skipped:
                msg += f", 건너뜀 {p.files_skipped:,}개"
            p.update(phase="done", message=msg, finished=time.time(), current="")
            self.log(msg)
        except StopRequested:
            p.update(phase="stopped", finished=time.time(), current="",
                     message="멈췄습니다. '이어 올리기'를 누르면 이어서 올립니다.")
            self.log("멈춤")
        except AuthError as e:
            p.update(phase="error", finished=time.time(), current="", message=str(e))
            self.log(str(e))
        except CircuitOpen as e:
            p.update(phase="error", finished=time.time(), current="", message=str(e))
            self.log(str(e))
        except Exception as e:  # noqa: BLE001 - 어떤 오류든 화면에 알리고 멈춘다
            log.exception("올리기 중 예상하지 못한 오류")
            p.update(phase="error", finished=time.time(), current="",
                     message=f"예상하지 못한 오류로 멈췄습니다: {e}")
            self.log(f"예상하지 못한 오류: {e!r}")
        finally:
            winutil.keep_awake(False)
            p.update(wait_reason=None, wait_until=None)
            for key, st in p.folder_status_copy().items():
                if st in ("queued", "uploading"):
                    p.set_folder(key, "stopped")

    def _new_album(self, fp, part):
        title = part_title(fp.title, part)
        used = self.store.titles_in_use(self.account)
        if title in used:  # 설정을 바꿔 다른 폴더가 이미 이 이름을 쓰는 경우
            n = 2
            while f"{title} ({n})" in used:
                n += 1
            title = f"{title} ({n})"
        album_id, url = self.client.create_album(title)
        self.store.add_album(self.account, self.root, fp.key, part, title, album_id, url)
        self.p.add(albums_created=1)
        self.log(f"앨범 만듦: {title}")
        return dict(part=part, title=title, album_id=album_id, product_url=url, missing=False)

    def _skip_rest(self, fp, items, status):
        """남은 파일을 건너뜀으로 센다. 진행 막대가 100%에 닿게 전체 용량에서도 뺀다."""
        self.p.add(files_skipped=len(items), bytes_total=-sum(f.size for f, _ in items))
        self.p.set_folder(fp.key, status)

    def _trip(self, systemic: bool):
        """묶음 하나가 모두 실패했을 때 부른다. 파일 탓(형식이 나쁨 등)이 아닌 실패가 섞였을 때만 센다."""
        if not systemic:
            return
        self._streak += 1
        if self._streak >= FAIL_STREAK_STOP:
            raise CircuitOpen(f"묶음 {FAIL_STREAK_STOP}개가 잇달아 모두 실패해 멈췄습니다. 구글 저장 공간이 "
                              "가득 찼거나 다른 문제가 있을 수 있습니다. 아래 기록을 확인하세요.")

    def _folder(self, fp, pending):
        p = self.p
        albums = self.store.albums_for_folder(self.account, self.root, fp.key)
        if any(a["missing"] for a in albums):
            self.log(f"'{fp.title}' 앨범이 구글 포토에서 지워져 건너뜁니다. 다시 올리려면 '앨범 고르기' 표에서 '다시 만들기'를 누르세요.")
            self._skip_rest(fp, pending, "missing")
            return
        p.set_folder(fp.key, "uploading")
        cur = albums[-1] if albums else None
        count = self.store.count_done(cur["album_id"]) if cur else 0
        failed_here = 0
        i = 0
        while i < len(pending):
            self._check_stop()
            if cur is None or count >= ALBUM_MAX:
                try:
                    cur = self._new_album(fp, (cur["part"] + 1) if cur else 1)
                except ApiError as e:  # 이 폴더만 건너뛰고 다음 폴더로 간다
                    rest = pending[i:]
                    self.log(f"앨범 '{fp.title}'을(를) 만들지 못해 이 폴더를 건너뜁니다({e.status}): {e.message}")
                    self.store.mark_failed(self.account, self.root, fp.key,
                                           [(f, None, f"앨범을 만들지 못함: {e.message}", False) for f, _ in rest])
                    p.add(files_failed=len(rest), bytes_total=-sum(f.size for f, _ in rest))
                    p.set_folder(fp.key, "partial")
                    self._trip(True)
                    return
                count = 0
            n = min(BATCH, ALBUM_MAX - count, len(pending) - i)
            batch = pending[i:i + n]
            i += n
            p.update(album=cur["title"])
            tokens = []
            systemic = 0  # 파일 탓이 아닌 실패 수
            for f, st in batch:
                self._check_stop()
                if (st and st.get("token") and st.get("token_time")
                        and time.time() - st["token_time"] < TOKEN_TTL):
                    tokens.append((f, st["token"]))   # 지난번에 바이트는 올렸고 앨범에 넣기 전에 끊긴 파일
                    p.add(bytes_done=f.size)
                    continue
                p.update(current=f.name)
                try:
                    tok = self.client.upload(f, lambda d: p.add(bytes_done=d))
                except (FileChanged, ApiError) as e:
                    perm = isinstance(e, ApiError) and e.permanent
                    msg = e.message if isinstance(e, ApiError) else str(e)
                    self.store.mark_failed(self.account, self.root, fp.key,
                                           [(f, cur["album_id"], msg, perm)])
                    p.add(files_failed=1, bytes_total=-f.size)
                    failed_here += 1
                    if not (isinstance(e, FileChanged) or e.status in (400, 413)):
                        systemic += 1
                    self.log(f"실패: {f.path} — {msg}")
                    continue
                self.store.save_tokens(self.account, self.root, fp.key, [(f, tok)])
                tokens.append((f, tok))
            ok = 0
            if tokens:
                try:
                    results = self.client.batch_create(cur["album_id"],
                                                       [(t, f.upload_name) for f, t in tokens])
                except ApiError as e:
                    exists = None
                    if e.status in (400, 403, 404):  # 지운 앨범에 어떤 코드가 오는지 문서에 없어 셋 다 확인한다
                        try:
                            exists = self.client.album_exists(cur["album_id"])
                        except ApiError:
                            exists = None
                    if exists is False:
                        self.store.mark_album_missing(cur["album_id"])
                        self.log(f"'{cur['title']}' 앨범이 구글 포토에서 지워진 것으로 보여 이 폴더를 건너뜁니다. "
                                 "다시 올리려면 '앨범 고르기' 표에서 '다시 만들기'를 누르세요.")
                        p.add(files_skipped=len(tokens))   # 바이트는 이미 보냈으니 전체 용량은 그대로
                        self._skip_rest(fp, pending[i:], "missing")
                        return
                    self.store.mark_failed(self.account, self.root, fp.key,
                                           [(f, cur["album_id"], f"앨범에 넣지 못함: {e.message}", False)
                                            for f, _t in tokens])
                    p.add(files_failed=len(tokens))
                    failed_here += len(tokens)
                    systemic += len(tokens)
                    self.log(f"앨범에 넣지 못함({e.status}): {e.message}")
                    results = None
                if results is not None:
                    done_rows, fail_rows = [], []
                    for (f, _t), (success, media_id, err, perm, code) in zip(tokens, results):
                        if success:
                            done_rows.append((f, cur["album_id"], media_id))
                        else:
                            fail_rows.append((f, cur["album_id"], err, perm))
                            if code != 3:  # INVALID_ARGUMENT(형식이 나쁜 파일 등)가 아니면 파일 탓이 아니다
                                systemic += 1
                            self.log(f"실패: {f.path} — {err}")
                    self.store.mark_done(self.account, self.root, fp.key, done_rows)
                    self.store.mark_failed(self.account, self.root, fp.key, fail_rows)
                    ok = len(done_rows)
                    count += ok
                    failed_here += len(fail_rows)
                    p.add(files_done=ok, files_failed=len(fail_rows))
                    p.bump()
            if ok > 0:
                self._streak = 0
            elif batch:
                self._trip(systemic > 0)
        p.set_folder(fp.key, "partial" if failed_here else "done")
