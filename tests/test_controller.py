"""컨트롤러 시험: 두 번 눌린 '올리기 시작'이 한 번만 돌고, 열쇠 파일이 바뀌면 올린 기록을 따로 센다."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_photos import FakePhotos  # noqa: E402
from gphoto.controller import Controller, UserError  # noqa: E402
from test_e2e_ui import FakeAuth  # noqa: E402


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "사진"
        for i in range(30):
            p = self.root / "앨범" / f"{i}.jpg"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 500)
        (base / "data").mkdir()
        self.fake = FakePhotos()
        self.auth = FakeAuth()
        self.ctl = Controller(base / "data", auth=self.auth, photos_base=self.fake.base)

    def tearDown(self):
        self.ctl.shutdown()
        self.fake.close()
        self.tmp.cleanup()

    def scan_and_select(self):
        self.ctl.set_root(str(self.root))
        self.ctl.scan()
        for _ in range(100):
            if self.ctl.scan_state["phase"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual(self.ctl.select(all=True), 1)

    def wait_upload(self):
        for _ in range(200):
            if not self.ctl.uploading():
                return
            time.sleep(0.05)
        self.fail("올리기가 끝나지 않음")

    def test_double_start_runs_once(self):
        self.scan_and_select()
        results = []
        barrier = threading.Barrier(2)

        def press():
            barrier.wait()
            try:
                results.append(self.ctl.start())
            except UserError as e:
                results.append(str(e))

        ts = [threading.Thread(target=press) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.wait_upload()
        self.assertEqual(results.count(True), 1, results)
        self.assertEqual(len(self.fake.albums), 1)
        self.assertEqual(self.fake.count("upload_raw"), 30)     # 두 번 올리지 않음

    def test_records_are_per_client(self):
        self.scan_and_select()
        self.ctl.start()
        self.wait_upload()
        self.assertEqual(self.ctl.albums()["items"][0]["done"], 30)
        # 열쇠 파일(클라이언트)을 바꾸면 그 클라이언트로는 옛 앨범을 못 다루므로 새로 센다
        self.auth.client_info = lambda: {"ok": True, "project": "other", "client": "999", "client_id": "999-x"}
        self.assertEqual(self.ctl.albums()["items"][0]["done"], 0)


if __name__ == "__main__":
    unittest.main()
