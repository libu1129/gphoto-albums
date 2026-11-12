"""구글 포토 Library API 호출. 다시 시도·기다리기 규칙을 여기서 맡는다.

근거 문서
- 업로드: https://developers.google.com/photos/library/guides/upload-media
- 이어 올리기: https://developers.google.com/photos/library/guides/resumable-uploads
- 한도·429: https://developers.google.com/photos/library/guides/api-limits-quotas
"""
from __future__ import annotations

import os
import time

import requests

from .scan import RESUMABLE_MIN, long_path, sniff

try:  # 시험(가짜 서버)에서는 google-auth 없이도 돌게 한다
    from google.auth.exceptions import RefreshError, TransportError
except ImportError:  # pragma: no cover
    class RefreshError(Exception):
        pass

    class TransportError(Exception):
        pass

API_BASE = "https://photoslibrary.googleapis.com/v1"

_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg", ".jfif": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
    ".heif": "image/heif", ".avif": "image/avif", ".bmp": "image/bmp", ".ico": "image/x-icon",
    ".tif": "image/tiff", ".tiff": "image/tiff",
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv", ".asf": "video/x-ms-asf",
    ".mkv": "video/x-matroska", ".3gp": "video/3gpp", ".3g2": "video/3gpp2",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
    ".m2t": "video/mp2t", ".divx": "video/x-msvideo",
}
# batchCreate 항목 상태 코드 중 다시 해도 안 될 것: INVALID_ARGUMENT, NOT_FOUND, PERMISSION_DENIED, FAILED_PRECONDITION
_PERMANENT_ITEM_CODES = {3, 5, 7, 9}
# 토큰을 갱신하다 네트워크가 끊기면 google-auth는 TransportError를 낸다(RefreshError와 다른 갈래)
_NET_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
               TransportError)


def _is_temporary_refresh(e) -> bool:
    """토큰 서버 5xx 같은 일시 실패면 google-auth가 retryable을 붙인다."""
    return bool(getattr(e, "retryable", False))


class StopRequested(Exception):
    """사용자가 '멈추기'를 눌렀다."""


class AuthError(Exception):
    """로그인이 풀렸거나 권한이 없다. 다시 로그인해야 한다."""


class FileChanged(Exception):
    """올리는 도중 파일을 읽지 못했다(지워짐·잠김·짧아짐). OSError가 아니어야 네트워크 재시도에 섞이지 않는다."""


class ApiError(Exception):
    def __init__(self, status, message, permanent=False):
        super().__init__(f"{status} {message}")
        self.status = status
        self.message = message
        self.permanent = permanent


def mime_of(name: str) -> str:
    return _MIME.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


# 같은 계열로 보는 MIME(이름과 내용이 이 안에서만 다르면 이름을 따른다)
_SAME_FAMILY = [{"image/heic", "image/heif"}, {"video/mp4", "video/x-m4v"}]


def content_mime(f):
    """올릴 MIME과, 이름(확장자)과 내용이 달랐으면 (이름 기준, 내용 기준)을 돌려준다.
    머리 바이트로 알아낸 형식이 있으면 그것을, 없으면(TIFF·RAW 등) 이름으로 정한다.
    근거: upload-media 문서 "Always set the correct MIME type"."""
    by_name = mime_of(f.upload_name)
    try:
        found = sniff(f.path)
    except OSError:
        return by_name, None  # 못 읽으면 바이트를 보낼 때 FileChanged로 잡힌다
    if not found:
        return by_name, None
    by_content = mime_of("x" + found[1])
    if by_content == by_name or any({by_name, by_content} <= g for g in _SAME_FAMILY):
        return by_name, None
    return by_content, (by_name, by_content)


def error_message(r) -> str:
    try:
        d = r.json()
        e = d.get("error", d) if isinstance(d, dict) else d
        if isinstance(e, dict):
            return str(e.get("message") or e.get("status") or "")[:300]
        return str(e)[:300]
    except ValueError:
        return (r.text or r.reason or "").strip()[:300]


class _Reader:
    """파일의 한 구간을 보내면서 보낸 양을 알린다. '멈추기'를 누르면 보내기를 끊는다.
    tell/fileno를 두지 않아야 requests가 Content-Length를 __len__으로 잡는다."""

    def __init__(self, path, offset, length, stop, on_bytes):
        try:
            self._f = open(long_path(path), "rb")
            if offset:
                self._f.seek(offset)
        except OSError as e:
            raise FileChanged(f"파일을 열지 못했습니다: {e.strerror or e}") from e
        self._left = length
        self._length = length
        self._stop = stop
        self._on = on_bytes

    def __len__(self):
        return self._length

    def read(self, n=-1):
        if self._stop is not None and self._stop.is_set():
            raise StopRequested()
        if self._left <= 0:
            return b""
        if n is None or n < 0 or n > self._left:
            n = self._left
        try:
            b = self._f.read(min(n, 1 << 20))
        except OSError as e:
            raise FileChanged(f"파일을 읽지 못했습니다: {e.strerror or e}") from e
        if not b:
            raise FileChanged("파일이 올리는 도중 짧아졌습니다")
        self._left -= len(b)
        self._on(len(b))
        return b

    def close(self):
        try:
            self._f.close()
        except OSError:
            pass


class PhotosClient:
    def __init__(self, session, base=API_BASE, *, stop=None, on_wait=None, on_request=None,
                 log=None, sleep=time.sleep, clock=time.time, resumable_min=RESUMABLE_MIN,
                 rate_wait=30, rate_cap=600, daily_wait=3600, net_wait=5, net_cap=300, max_5xx=6):
        self.s = session
        self.base = base.rstrip("/")
        self.stop = stop
        self._on_wait = on_wait or (lambda reason, until: None)
        self._on_request = on_request or (lambda: None)
        self._log = log or (lambda msg: None)
        self._sleep = sleep
        self._clock = clock
        self.resumable_min = resumable_min
        self.rate_wait, self.rate_cap, self.daily_wait = rate_wait, rate_cap, daily_wait
        self.net_wait, self.net_cap, self.max_5xx = net_wait, net_cap, max_5xx
        self._n429 = 0  # 잇단 429 횟수. 성공하면 0

    # ── 공통 ──
    def _check_stop(self):
        if self.stop is not None and self.stop.is_set():
            raise StopRequested()

    def _wait(self, seconds, reason):
        until = self._clock() + seconds
        self._on_wait(reason, until)
        try:
            while True:
                self._check_stop()
                left = until - self._clock()
                if left <= 0:
                    break
                self._sleep(min(1.0, left))
        finally:
            self._on_wait(None, None)

    def _handle_429(self, r):
        self._n429 += 1
        msg = error_message(r)
        # 문서 밖 의존: 하루 한도 응답 메시지에 'per day'가 든다고 본다. 아니어도 6번 잇달으면 하루 한도로 본다.
        if "per day" in msg.lower() or self._n429 >= 6:
            self._log("하루 호출 한도에 걸린 것으로 보입니다. 1시간 뒤 다시 시도합니다(그대로 두면 이어서 올립니다).")
            self._wait(self.daily_wait, "하루 호출 한도에 걸림 — 1시간마다 다시 시도합니다")
            self._n429 = max(self._n429, 5)
        else:
            w = min(self.rate_cap, self.rate_wait * 2 ** (self._n429 - 1))
            self._log(f"요청이 많다는 응답(429)을 받아 {int(w)}초 쉬고 다시 시도합니다.")
            self._wait(w, "요청이 많아 잠시 쉬는 중")

    def _net_retry(self, n, e, what="네트워크 오류"):
        self._check_stop()
        w = min(self.net_cap, self.net_wait * 2 ** min(n - 1, 8))
        self._log(f"{what}({type(e).__name__}). {int(w)}초 뒤 다시 시도합니다.")
        self._wait(w, f"{what} — 다시 연결을 기다리는 중")

    def _send(self, method, url, *, headers=None, json_body=None, body_factory=None,
              timeout=(20, 120), fatal_403=True):
        """fatal_403=False면 권한 범위가 아닌 403을 ApiError로 돌려준다(특정 앨범에 넣거나 볼 때: 앨범이 지워졌을 수 있다)."""
        n5xx = nnet = 0
        while True:
            self._check_stop()
            body = body_factory() if body_factory else None
            try:
                self._on_request()
                r = self.s.request(method, url, headers=headers, json=json_body, data=body,
                                   timeout=timeout)
            except (StopRequested, FileChanged):
                raise
            except RefreshError as e:
                if not _is_temporary_refresh(e):
                    raise AuthError("로그인이 만료됐습니다. 다시 로그인하세요.") from e
                nnet += 1
                self._net_retry(nnet, e, "구글 로그인 서버 일시 오류")
                continue
            except _NET_ERRORS as e:
                nnet += 1
                self._net_retry(nnet, e)
                continue
            finally:
                if body is not None and hasattr(body, "close"):
                    body.close()
            if r.status_code == 429:
                self._handle_429(r)
                continue
            if r.status_code in (500, 502, 503, 504):
                n5xx += 1
                if n5xx > self.max_5xx:
                    raise ApiError(r.status_code, error_message(r), permanent=False)
                w = min(300, 5 * 2 ** (n5xx - 1))
                self._log(f"구글 서버 오류({r.status_code}). {w}초 뒤 다시 시도합니다.")
                self._wait(w, "구글 서버 오류 — 다시 시도 대기")
                continue
            self._n429 = 0
            if r.status_code == 401:
                raise AuthError("로그인이 만료됐습니다. 다시 로그인하세요.")
            if r.status_code == 403:  # 파일 탓이 아니라 계정·프로젝트 설정 문제라 멈춘다
                msg = error_message(r)
                if "scope" in msg.lower():
                    raise AuthError("구글 포토 권한이 없습니다. 다시 로그인해 권한에 모두 체크하세요.")
                if not fatal_403:
                    raise ApiError(403, msg, permanent=True)
                raise AuthError(f"구글이 요청을 거부했습니다(403): {msg} — 구글 클라우드에서 Photos Library API를 "
                                "켰는지, 로그인한 계정이 테스트 사용자인지 확인하세요.")
            if r.status_code >= 400:
                raise ApiError(r.status_code, error_message(r),
                               permanent=r.status_code in (400, 403, 404, 413))
            return r

    # ── 바이트 올리기 ──
    def upload(self, f, on_bytes) -> str:
        """파일 바이트를 올리고 업로드 토큰을 돌려준다. on_bytes(늘어난 바이트, 되돌릴 땐 음수)."""
        mime, differ = content_mime(f)  # 확장자와 내용이 다르면 내용을 따른다
        if differ:
            self._log(f"확장자와 내용이 달라 형식을 내용대로 보냅니다: {f.path} ({differ[0]} → {differ[1]})")
        if f.size > self.resumable_min:
            return self._upload_resumable(f, mime, on_bytes)
        return self._upload_raw(f, mime, on_bytes)

    def _upload_raw(self, f, mime, on_bytes):
        sent = [0]

        def count(n):
            sent[0] += n
            on_bytes(n)

        def factory():
            if sent[0]:  # 다시 보낼 때 진행량을 되돌린다
                on_bytes(-sent[0])
                sent[0] = 0
            return _Reader(f.path, 0, f.size, self.stop, count)

        headers = {"Content-Type": "application/octet-stream",
                   "X-Goog-Upload-Content-Type": mime, "X-Goog-Upload-Protocol": "raw"}
        try:
            r = self._send("POST", f"{self.base}/uploads", headers=headers, body_factory=factory)
        except BaseException:
            if sent[0]:
                on_bytes(-sent[0])
            raise
        token = r.text.strip()
        if not token:
            raise ApiError(r.status_code, "업로드 토큰이 비어 있습니다")
        return token

    def _upload_resumable(self, f, mime, on_bytes):
        counted = [0]

        def count(n):
            counted[0] += n
            on_bytes(n)

        def set_counted(v):
            on_bytes(v - counted[0])
            counted[0] = v

        try:
            for _restart in range(3):
                r = self._send("POST", f"{self.base}/uploads", headers={
                    "Content-Length": "0", "X-Goog-Upload-Command": "start",
                    "X-Goog-Upload-Content-Type": mime, "X-Goog-Upload-Protocol": "resumable",
                    "X-Goog-Upload-Raw-Size": str(f.size)})
                url = r.headers.get("X-Goog-Upload-URL")
                if not url:
                    raise ApiError(r.status_code, "이어 올리기 주소를 받지 못했습니다")
                offset = 0
                set_counted(0)
                tries = 0
                while True:
                    self._check_stop()
                    # 남은 바이트를 한 요청으로 보내고 끝낸다(요청 수를 줄이려고). 끊기면 받은 만큼 물어 이어서 보낸다.
                    reader = _Reader(f.path, offset, f.size - offset, self.stop, count)
                    resp = None
                    try:
                        self._on_request()
                        resp = self.s.post(url, headers={"X-Goog-Upload-Command": "upload, finalize",
                                                         "X-Goog-Upload-Offset": str(offset)},
                                           data=reader, timeout=(20, 900))
                    except (StopRequested, FileChanged):
                        raise
                    except RefreshError as e:
                        if not _is_temporary_refresh(e):
                            raise AuthError("로그인이 만료됐습니다. 다시 로그인하세요.") from e
                        tries += 1
                        if tries > 30:
                            raise ApiError(0, "구글 로그인 서버가 계속 응답하지 않습니다") from e
                        self._net_retry(tries, e, "구글 로그인 서버 일시 오류")
                    except _NET_ERRORS as e:
                        tries += 1
                        if tries > 30:
                            raise ApiError(0, "큰 파일 올리기가 계속 끊깁니다") from e
                        self._net_retry(tries, e, "큰 파일 전송이 끊김")
                    finally:
                        reader.close()
                    if resp is not None:
                        if resp.status_code == 200:
                            token = resp.text.strip()
                            if not token:
                                raise ApiError(200, "업로드 토큰이 비어 있습니다")
                            self._n429 = 0
                            return token
                        if resp.status_code == 429:
                            self._handle_429(resp)
                        elif resp.status_code in (500, 502, 503, 504):
                            tries += 1
                            if tries > 30:
                                raise ApiError(resp.status_code, error_message(resp))
                            self._wait(min(300, 5 * 2 ** min(tries - 1, 6)), "구글 서버 오류 — 이어 올리기 대기")
                        elif resp.status_code == 401:
                            raise AuthError("로그인이 만료됐습니다. 다시 로그인하세요.")
                        elif resp.status_code == 403:
                            raise AuthError(f"구글이 요청을 거부했습니다(403): {error_message(resp)}")
                        else:
                            raise ApiError(resp.status_code, error_message(resp),
                                           permanent=resp.status_code in (400, 403, 404, 413))
                    q = self._send("POST", url, headers={"Content-Length": "0",
                                                         "X-Goog-Upload-Command": "query"})
                    status = (q.headers.get("X-Goog-Upload-Status") or "active").lower()
                    if status != "active":
                        self._log(f"이어 올리기 세션이 끝나({status}) 처음부터 다시 보냅니다: {f.name}")
                        break
                    offset = int(q.headers.get("X-Goog-Upload-Size-Received") or 0)
                    set_counted(offset)
            raise ApiError(0, "큰 파일 올리기를 세 번 다시 시작했지만 실패했습니다")
        except BaseException:
            set_counted(0)
            raise

    # ── 앨범·항목 ──
    def create_album(self, title):
        r = self._send("POST", f"{self.base}/albums", json_body={"album": {"title": title}})
        d = r.json()
        return d["id"], d.get("productUrl")

    def album_exists(self, album_id):
        """앨범이 아직 있으면 True, 없으면 False, 알 수 없으면 None(읽기 권한을 안 받았을 때 등)."""
        try:
            self._send("GET", f"{self.base}/albums/{album_id}", fatal_403=False)
            return True
        except AuthError:  # 읽기 권한(범위)을 안 받았을 때 등
            return None
        except ApiError as e:
            if e.status in (400, 403, 404):  # 없거나 이 앱이 더는 다룰 수 없는 앨범
                return False
            raise

    def batch_create(self, album_id, items):
        """items: [(업로드 토큰, 파일 이름)] 최대 50개. 결과: [(성공, media_id, 오류, 다시 해도 안 될지, 상태 코드)]"""
        body = {"albumId": album_id,
                "newMediaItems": [{"simpleMediaItem": {"uploadToken": t, "fileName": n}}
                                  for t, n in items]}
        r = self._send("POST", f"{self.base}/mediaItems:batchCreate", json_body=body, fatal_403=False)
        results = (r.json() or {}).get("newMediaItemResults") or []
        by_token = {x.get("uploadToken"): x for x in results if x.get("uploadToken")}
        out = []
        for i, (t, _n) in enumerate(items):
            x = by_token.get(t) or (results[i] if i < len(results) else None)
            if not x:
                out.append((False, None, "결과가 오지 않았습니다", False, -1))
                continue
            st = x.get("status") or {}
            code = int(st.get("code") or 0)
            mi = x.get("mediaItem")
            if mi and code == 0:
                out.append((True, mi.get("id"), None, False, 0))
            else:
                out.append((False, None, st.get("message") or f"오류 코드 {code}",
                            code in _PERMANENT_ITEM_CODES, code))
        return out
