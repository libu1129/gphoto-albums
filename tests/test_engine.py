"""업로드 엔진 시험: 가짜 구글 포토 서버를 상대로 앨범 만들기, 이어 올리기, 재시도, 멈추기를 본다."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_photos import FakePhotos  # noqa: E402
from gphoto import engine as E  # noqa: E402
from gphoto.photos_api import PhotosClient  # noqa: E402
from gphoto.scan import scan  # noqa: E402
from gphoto.store import Store  # noqa: E402

BIG = 300_000          # 이어 올리기 기준(100KB)보다 큰 영상
RESUMABLE_MIN = 100_000


class FakeClock:
    """기다리기를 실제로 하지 않고 시계만 앞으로 돌린다."""

    def __init__(self):
        self.t = 1_000_000.0
        self.slept = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.slept += s


def touch(p: Path, size=1000):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(bytes((i * 7) % 256 for i in range(size)))
    return p


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "사진"
        touch(self.root / "2023" / "제주여행" / "a.jpg")
        touch(self.root / "2023" / "제주여행" / "b.jpg")
        touch(self.root / "2023" / "제주여행" / "bad_c.jpg")   # 가짜 서버가 실패로 돌려줌
        touch(self.root / "2023" / "x.png")
        touch(self.root / "영상" / "v.mp4", BIG)
        touch(self.root / "r.jpg")
        self.fake = FakePhotos()
        self.store = Store(base / "state.db")
        self.clock = FakeClock()
        self.logs = []
        self.old_album_max = E.ALBUM_MAX

    def tearDown(self):
        E.ALBUM_MAX = self.old_album_max
        self.store.close()
        self.fake.close()
        self.tmp.cleanup()

    def run_engine(self, stop=None, progress=None, folders=None):
        stop = stop or threading.Event()
        progress = progress or E.Progress()
        client = PhotosClient(requests.Session(), self.fake.base, stop=stop, sleep=self.clock.sleep,
                              clock=self.clock.time, resumable_min=RESUMABLE_MIN, log=self.logs.append)
        plan = scan(self.root, include_videos=True)
        eng = E.Engine(client, self.store, "acct", plan.root, folders or plan.folders, progress,
                       self.logs.append, stop)
        eng.run()
        return progress, plan

    def album_items(self):
        return {a["title"]: sorted(a["items"]) for a in self.fake.albums.values()}

    def test_basic_upload(self):
        p, plan = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)
        self.assertEqual(self.album_items(), {
            "사진": ["r.jpg"], "2023": ["x.png"], "2023_제주여행": ["a.jpg", "b.jpg"], "영상": ["v.mp4"]})
        self.assertEqual(p.files_done, 5)
        self.assertEqual(p.files_failed, 1)
        self.assertEqual(self.fake.count("resumable_start"), 1)   # 큰 파일만 이어 올리기
        # 앨범에 넣다 실패한 bad_c.jpg도 바이트는 보냈으니 '보낸 용량'에 든다
        self.assertEqual(p.bytes_done, sum(fp.bytes for fp in plan.folders))
        st = self.store.item_map("acct", plan.root, "2023/제주여행")
        statuses = sorted(v["status"] for v in st.values())
        self.assertEqual(statuses, ["done", "done", "failed_perm"])

    def test_rerun_does_not_duplicate(self):
        self.run_engine()
        uploads = self.fake.count("upload_raw")
        albums = len(self.fake.albums)
        p, _ = self.run_engine()
        self.assertEqual(p.phase, "done")
        self.assertEqual(len(self.fake.albums), albums)          # 앨범을 또 만들지 않음
        self.assertEqual(self.fake.count("upload_raw"), uploads)  # 다 올린 것과 영구 실패는 다시 안 보냄
        self.assertEqual(p.files_total, 0)

    def test_stop_and_resume(self):
        stop = threading.Event()

        class StopAfter(E.Progress):
            def add(self, **kw):
                super().add(**kw)
                if self.files_done >= 1:
                    stop.set()

        p, plan = self.run_engine(stop=stop, progress=StopAfter())
        self.assertEqual(p.phase, "stopped")
        first_albums = len(self.fake.albums)
        p2, _ = self.run_engine()
        self.assertEqual(p2.phase, "done", p2.message)
        self.assertEqual(sum(len(v) for v in self.album_items().values()), 5)
        self.assertGreaterEqual(len(self.fake.albums), first_albums)
        self.assertEqual(len(self.fake.albums), 4)

    def test_rate_limit_then_daily_quota(self):
        self.fake.add_fault("rate", "batch")
        self.fake.add_fault("daily", "upload")
        p, _ = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)
        self.assertGreaterEqual(self.clock.slept, 3600 + 30)
        self.assertTrue(any("429" in m for m in self.logs))
        self.assertTrue(any("하루 호출 한도" in m for m in self.logs))

    def test_server_error_retry(self):
        self.fake.add_fault("500", "albums", times=2)
        p, _ = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)
        self.assertEqual(len(self.fake.albums), 4)

    def test_resumable_drop_resumes_from_offset(self):
        self.fake.add_fault("drop", "resumable", after=120_000)
        p, _ = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)
        self.assertEqual(self.fake.count("resumable_start"), 1)   # 처음부터 다시 시작하지 않음
        self.assertGreaterEqual(self.fake.count("query"), 1)
        sess = list(self.fake.sessions.values())[0]
        self.assertTrue(sess["final"])
        self.assertEqual(sess["received"], BIG)
        uploads = [c for c in self.fake.calls if c[0] == "resumable_upload"]
        self.assertEqual(uploads[-1][2], 120_000)                 # 서버가 받은 곳부터 이어 보냄

    def test_deleted_album_is_not_recreated(self):
        self.run_engine()
        aid = next(k for k, a in self.fake.albums.items() if a["title"] == "2023")
        self.fake.delete_album(aid)
        touch(self.root / "2023" / "new.jpg")
        p, plan = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)
        self.assertEqual(p.folder_status.get("2023"), "missing")
        self.assertEqual(len(self.fake.albums), 3)                # 저절로 다시 만들지 않음
        self.store.forget_folder("acct", plan.root, "2023")       # 화면의 '다시 만들기'
        p2, _ = self.run_engine()
        self.assertEqual(p2.phase, "done")
        self.assertEqual(self.album_items()["2023"], ["new.jpg", "x.png"])

    def test_deleted_album_detected_when_google_says_403(self):
        self.run_engine()
        aid = next(k for k, a in self.fake.albums.items() if a["title"] == "2023")
        self.fake.delete_album(aid)
        self.fake.missing_album_status = 403
        touch(self.root / "2023" / "new.jpg")
        p, _ = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)            # 전체를 멈추지 않는다
        self.assertEqual(p.folder_status.get("2023"), "missing")

    def test_extensionless_photo_uploaded_with_detected_type(self):
        (self.root / "2023" / "IMG_7777").write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x01" * 500)
        p, _ = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)
        self.assertIn("IMG_7777.heic", self.album_items()["2023"])          # 구글에 넘긴 이름에 확장자
        ctypes_sent = {c[2] for c in self.fake.calls if c[0] == "upload_raw"}
        self.assertIn("image/heic", ctypes_sent)                           # MIME도 알아낸 형식으로

    def test_mime_follows_content_when_extension_lies(self):
        (self.root / "2023" / "real_jpeg.png").write_bytes(b"\xff\xd8\xff\xe0" + b"\x02" * 800)        # 804B
        (self.root / "2023" / "real_heic.jpg").write_bytes(
            b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x03" * 800)                       # 824B
        (self.root / "2023" / "true.heic").write_bytes(
            b"\x00\x00\x00\x14ftypmif1\x00\x00\x00\x00mif1" + b"\x04" * 900)                           # HEIF 계열: 이름 따름
        p, _ = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)
        sent = {c[1]: c[2] for c in self.fake.calls if c[0] == "upload_raw"}
        self.assertEqual(sent[804], "image/jpeg")
        self.assertEqual(sent[824], "image/heic")
        self.assertEqual(sent[920], "image/heic")
        # 내용을 모르는 파일(setUp의 1000바이트 파일들)은 이름대로: .jpg → jpeg, .png → png
        self.assertEqual({c[2] for c in self.fake.calls if c[0] == "upload_raw" and c[1] == 1000},
                         {"image/jpeg", "image/png"})
        self.assertEqual(sum("확장자와 내용이 달라" in m for m in self.logs), 2)

    def test_token_reused_after_crash(self):
        stop = threading.Event()
        client = PhotosClient(requests.Session(), self.fake.base, stop=stop, sleep=self.clock.sleep,
                              clock=self.clock.time, resumable_min=RESUMABLE_MIN)

        def boom(*a, **k):
            raise RuntimeError("batchCreate 직전 끊김")

        client.batch_create = boom
        plan = scan(self.root)
        p = E.Progress()
        E.Engine(client, self.store, "acct", plan.root, plan.folders, p, self.logs.append, stop).run()
        self.assertEqual(p.phase, "error")
        sent = self.fake.count("upload_raw")
        p2, _ = self.run_engine()
        self.assertEqual(p2.phase, "done", p2.message)
        # 첫 폴더(최상위 r.jpg)는 토큰을 다시 써서 바이트를 다시 보내지 않는다
        self.assertEqual(sent, 1)
        self.assertEqual(self.fake.count("upload_raw") - sent, 4)  # x.png, a.jpg, b.jpg, bad_c.jpg 만 새로
        self.assertIn("r.jpg", self.album_items()["사진"])

    def test_split_into_parts(self):
        E.ALBUM_MAX = 2
        for i in range(3):
            touch(self.root / "2023" / "제주여행" / f"z{i}.jpg")
        p, _ = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)
        titles = sorted(t for t in self.album_items() if t.startswith("2023_제주여행"))
        self.assertEqual(titles, ["2023_제주여행", "2023_제주여행 #2", "2023_제주여행 #3"])
        self.assertTrue(all(len(v) <= 2 for v in self.album_items().values()))

    def test_circuit_breaker(self):
        for name in ("a", "b", "c", "d"):
            touch(self.root / "모두실패" / name / "full_1.jpg")
        plan = scan(self.root)
        only = [fp for fp in plan.folders if fp.key.startswith("모두실패")]
        p, _ = self.run_engine(folders=only)
        self.assertEqual(p.phase, "error")
        self.assertIn("잇달아", p.message)
        self.assertEqual(self.fake.count("batch"), 3)            # 세 묶음째에서 멈춤

    def test_bad_files_do_not_trip_breaker(self):
        for name in ("a", "b", "c", "d"):
            touch(self.root / "형식불량" / name / "bad_1.jpg")
        p, _ = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)            # 파일 탓 실패만으로는 멈추지 않는다
        self.assertIn("영상", self.album_items())

    def test_album_create_failure_skips_only_that_folder(self):
        self.fake.add_fault("400", "albums")                     # 첫 폴더(최상위) 앨범 만들기 실패
        p, plan = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)
        self.assertNotIn("사진", self.album_items())
        self.assertIn("2023_제주여행", self.album_items())
        self.assertEqual(p.folder_status.get(""), "partial")
        p2, _ = self.run_engine()                                # 다음 실행 때 다시 시도
        self.assertEqual(self.album_items()["사진"], ["r.jpg"])

    def test_token_refresh_network_error_is_retried(self):
        from google.auth.exceptions import TransportError

        class Flaky(requests.Session):
            n = 0

            def request(self, *a, **k):
                Flaky.n += 1
                if Flaky.n == 2:
                    raise TransportError("갱신 중 연결 끊김")
                return super().request(*a, **k)

        stop = threading.Event()
        client = PhotosClient(Flaky(), self.fake.base, stop=stop, sleep=self.clock.sleep,
                              clock=self.clock.time, resumable_min=RESUMABLE_MIN, log=self.logs.append)
        plan = scan(self.root)
        p = E.Progress()
        E.Engine(client, self.store, "acct", plan.root, plan.folders, p, self.logs.append, stop).run()
        self.assertEqual(p.phase, "done", p.message)
        self.assertTrue(any("TransportError" in m for m in self.logs))

    def test_upload_rejected_and_progress_reaches_total(self):
        self.fake.add_fault("400", "upload")
        p, _ = self.run_engine()
        self.assertEqual(p.phase, "done", p.message)
        self.assertEqual(p.files_failed, 2)                      # 거절된 1개 + bad_c
        self.assertEqual(p.bytes_done, p.bytes_total)            # 막대가 100%에 닿는다

    def test_unaccepted_bytes_not_counted_while_waiting(self):
        """429·서버 오류·끊김으로 기다리는 동안 진행량에는 서버가 받아들인 바이트만 남는다."""
        total = [0]
        at_wait = []

        def on_wait(reason, until):
            if reason:                      # 기다림이 끝날 때도 (None, None)으로 불린다
                at_wait.append(total[0])

        def on_bytes(d):
            total[0] += d

        client = PhotosClient(requests.Session(), self.fake.base, stop=threading.Event(), on_wait=on_wait,
                              sleep=self.clock.sleep, clock=self.clock.time, resumable_min=RESUMABLE_MIN,
                              log=self.logs.append)
        files = {f.name: f for fp in scan(self.root, include_videos=True).folders for f in fp.files}
        cases = [("a.jpg", "daily", "upload", {}),                  # 작은 파일: 하루 한도
                 ("v.mp4", "rate", "resumable", {}),                # 큰 파일: 429
                 ("v.mp4", "500", "resumable", {}),                 # 큰 파일: 서버 오류
                 ("v.mp4", "drop", "resumable", {"after": 120_000})]  # 큰 파일: 보내다 끊김
        for name, kind, where, kw in cases:
            with self.subTest(kind=kind, where=where):
                total[0] = 0
                at_wait.clear()
                self.fake.add_fault(kind, where, **kw)
                client.upload(files[name], on_bytes)
                self.assertEqual(at_wait, [0])                      # 기다림을 시작할 때 0
                self.assertEqual(total[0], files[name].size)        # 끝나면 파일 크기

    def test_forbidden_stops_with_message(self):
        self.fake.add_fault("403", "upload")
        p, _ = self.run_engine()
        self.assertEqual(p.phase, "error")
        self.assertIn("Photos Library API", p.message)


if __name__ == "__main__":
    unittest.main()
