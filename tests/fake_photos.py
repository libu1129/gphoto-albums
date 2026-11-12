"""시험용 가짜 구글 포토 서버.

공식 문서에 적힌 요청·응답 모양(업로드 머리글, 이어 올리기 명령, batchCreate 결과)만 흉내 낸다.
429 본문 문구, 청크 단위, 지워진 앨범에 넣을 때의 응답 같은 실서버 세부 값은 확인하지 못한 가정이다.
"""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MSG_RATE = ("Quota exceeded for quota metric 'Write requests' and limit "
            "'Write requests per minute per user' of service 'photoslibrary.googleapis.com'")
MSG_DAILY = ("Quota exceeded for quota metric 'All requests' and limit "
             "'All requests per day' of service 'photoslibrary.googleapis.com'")


class FakePhotos:
    def __init__(self):
        self.lock = threading.Lock()
        self.albums: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.faults: list[dict] = []
        self.bad_prefix = "bad"
        self.missing_album_status = 400   # 지운 앨범에 넣을 때 응답(실서버 값 미확인). 403으로 바꿔 시험한다
        self.granularity = 262144
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # 고장 넣기: kind = rate | daily | 500 | 400 | 403 | drop, where = upload | resumable | albums | batch
    # (resumable은 이어 올리기의 바이트 보내기 요청. drop은 after 바이트를 받은 뒤 연결을 끊는다)
    def add_fault(self, kind, where, times=1, **kw):
        with self.lock:
            self.faults.append(dict(kind=kind, where=where, times=times, **kw))

    def _take(self, where):
        with self.lock:
            for f in self.faults:
                if f["where"] == where and f["times"] > 0:
                    f["times"] -= 1
                    return f
        return None

    def count(self, what):
        with self.lock:
            return sum(1 for c in self.calls if c[0] == what)

    def delete_album(self, album_id):
        with self.lock:
            self.albums.pop(album_id, None)

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _reply(self, code, body=b"", ctype="application/json", headers=None):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                elif isinstance(body, str):
                    body = body.encode()
                    ctype = "text/plain"
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _err(self, code, msg, status):
                self._reply(code, {"error": {"code": code, "message": msg, "status": status}})

            def _fault(self, where):
                f = fake._take(where)
                if not f:
                    return False
                if f["kind"] == "drop":
                    return f  # 호출한 쪽이 처리
                self._body()
                self._fault_reply(f["kind"])
                return True

            def _fault_reply(self, kind):
                if kind == "rate":
                    self._err(429, MSG_RATE, "RESOURCE_EXHAUSTED")
                elif kind == "daily":
                    self._err(429, MSG_DAILY, "RESOURCE_EXHAUSTED")
                elif kind == "500":
                    self._err(500, "Internal error encountered.", "INTERNAL")
                elif kind == "400":
                    self._err(400, "Request contains an invalid argument.", "INVALID_ARGUMENT")
                elif kind == "403":
                    self._err(403, "Photos Library API has not been used in project 123 before or it is disabled.",
                              "PERMISSION_DENIED")

            def do_GET(self):  # noqa: N802
                path = urlparse(self.path).path
                if path.startswith("/v1/albums/"):
                    aid = path.rsplit("/", 1)[1]
                    with fake.lock:
                        fake.calls.append(("get_album", aid))
                        a = fake.albums.get(aid)
                    if a:
                        return self._reply(200, {"id": aid, "title": a["title"]})
                    if fake.missing_album_status == 403:
                        return self._err(403, "The caller does not have permission", "PERMISSION_DENIED")
                    return self._err(404, "Requested entity was not found.", "NOT_FOUND")
                self._err(404, "no route", "NOT_FOUND")

            def do_POST(self):  # noqa: N802
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path == "/v1/uploads" and "upload_id" in q:
                    return self._resumable(q["upload_id"][0])
                if u.path == "/v1/uploads":
                    proto = self.headers.get("X-Goog-Upload-Protocol")
                    if proto == "raw":
                        if self._fault("upload"):
                            return
                        data = self._body()
                        tok = "tok-" + uuid.uuid4().hex
                        with fake.lock:
                            fake.calls.append(("upload_raw", len(data), self.headers.get("X-Goog-Upload-Content-Type")))
                            fake.tokens[tok] = {"size": len(data)}
                        return self._reply(200, tok)
                    if proto == "resumable" and self.headers.get("X-Goog-Upload-Command") == "start":
                        self._body()
                        sid = uuid.uuid4().hex
                        with fake.lock:
                            fake.calls.append(("resumable_start", sid))
                            fake.sessions[sid] = {"size": int(self.headers["X-Goog-Upload-Raw-Size"]),
                                                  "received": 0, "final": False, "requests": 0}
                        return self._reply(200, b"", headers={
                            "X-Goog-Upload-URL": f"http://127.0.0.1:{fake.port}/v1/uploads?upload_id={sid}&upload_protocol=resumable",
                            "X-Goog-Upload-Chunk-Granularity": str(fake.granularity)})
                    self._body()
                    return self._err(400, "bad upload request", "INVALID_ARGUMENT")
                if u.path == "/v1/albums":
                    if self._fault("albums"):
                        return
                    body = json.loads(self._body() or b"{}")
                    title = body["album"]["title"]
                    aid = "alb-" + uuid.uuid4().hex[:10]
                    with fake.lock:
                        fake.calls.append(("create_album", title))
                        fake.albums[aid] = {"title": title, "items": []}
                    return self._reply(200, {"id": aid, "title": title,
                                             "productUrl": f"https://photos.google.com/lr/album/{aid}"})
                if u.path == "/v1/mediaItems:batchCreate":
                    if self._fault("batch"):
                        return
                    body = json.loads(self._body() or b"{}")
                    aid = body.get("albumId")
                    items = body.get("newMediaItems") or []
                    with fake.lock:
                        fake.calls.append(("batch", aid, len(items)))
                        album = fake.albums.get(aid)
                    if album is None:
                        if fake.missing_album_status == 403:
                            return self._err(403, "The caller does not have permission", "PERMISSION_DENIED")
                        return self._err(400, "Invalid album id.", "INVALID_ARGUMENT")
                    results, all_ok = [], True
                    for it in items:
                        smi = it["simpleMediaItem"]
                        tok, name = smi["uploadToken"], smi.get("fileName", "")
                        with fake.lock:
                            known = tok in fake.tokens
                        if not known:
                            results.append({"uploadToken": tok, "status": {"code": 3, "message": "Invalid upload token"}})
                            all_ok = False
                        elif name.startswith("full_"):  # 저장 공간 가득 참을 흉내(실서버 코드는 미확인)
                            results.append({"uploadToken": tok, "status": {"code": 8, "message": "Insufficient storage."}})
                            all_ok = False
                        elif name.startswith(fake.bad_prefix):
                            results.append({"uploadToken": tok, "status": {"code": 3, "message": "Failed: There was an error while trying to create this media item."}})
                            all_ok = False
                        else:
                            mid = "m-" + uuid.uuid4().hex[:12]
                            with fake.lock:
                                album["items"].append(name)
                            results.append({"uploadToken": tok, "status": {"message": "Success"},
                                            "mediaItem": {"id": mid, "filename": name}})
                    return self._reply(200 if all_ok else 207, {"newMediaItemResults": results})
                self._body()
                self._err(404, "no route", "NOT_FOUND")

            def _resumable(self, sid):
                cmd = self.headers.get("X-Goog-Upload-Command", "")
                with fake.lock:
                    s = fake.sessions.get(sid)
                if s is None:
                    self._body()
                    return self._err(404, "no session", "NOT_FOUND")
                if cmd == "query":
                    self._body()
                    with fake.lock:
                        fake.calls.append(("query", sid))
                    return self._reply(200, b"", headers={
                        "X-Goog-Upload-Status": "final" if s["final"] else "active",
                        "X-Goog-Upload-Size-Received": str(s["received"])})
                offset = int(self.headers.get("X-Goog-Upload-Offset") or 0)
                n = int(self.headers.get("Content-Length") or 0)
                with fake.lock:
                    s["requests"] += 1
                    fake.calls.append(("resumable_upload", sid, offset, n))
                if offset != s["received"]:
                    self._body()
                    return self._err(400, "offset mismatch", "INVALID_ARGUMENT")
                f = fake._take("resumable")
                if f and f["kind"] == "drop":
                    part = self.rfile.read(min(n, f.get("after", 1)))
                    with fake.lock:
                        s["received"] += len(part)
                    self.close_connection = True
                    try:
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    return
                if f:  # 429·500 등: 본문은 끝까지 읽어 버리고(받은 양에 안 넣음) 오류로 답한다
                    self._body()
                    return self._fault_reply(f["kind"])
                data = self.rfile.read(n) if n else b""
                with fake.lock:
                    s["received"] += len(data)
                if "finalize" in cmd:
                    if s["received"] != s["size"]:
                        return self._err(400, "size mismatch", "INVALID_ARGUMENT")
                    tok = "tok-" + uuid.uuid4().hex
                    with fake.lock:
                        s["final"] = True
                        fake.tokens[tok] = {"size": s["size"]}
                    return self._reply(200, tok)
                return self._reply(200, b"")

        return H
