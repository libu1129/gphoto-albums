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

    def test_root_title_kept_per_folder(self):
        """앨범 이름에 쓸 최상위 폴더 이름: 폴더마다 따로 남고(오가도 돌아옴), 다시 열어도 남고, 검사한다."""
        with self.assertRaises(UserError):
            self.ctl.set_options(include_root=True, root_title="가족")   # 폴더를 고르기 전
        self.assertFalse(self.ctl.settings["include_root"])                # 거절하면 다른 설정도 그대로
        self.ctl.set_root(str(self.root))
        out = self.ctl.set_options(root_title="  우리\t집 ")
        self.assertEqual(out["root_title"], "우리 집")
        self.assertNotIn("root_titles", out)
        st = self.ctl.snapshot()["settings"]
        self.assertEqual((st["root_title"], st["root_label"]), ("우리 집", "사진"))
        self.assertNotIn("root_titles", st)
        for bad in ("가" * 101, 3):
            with self.assertRaises(UserError):
                self.ctl.set_options(root_title=bad)
        self.assertEqual(self.ctl.snapshot()["settings"]["root_title"], "우리 집")
        # 다른 폴더로 갔다가 돌아오면 그 폴더의 이름이 돌아온다. 대소문자만 다른 경로도 같은 폴더로 본다
        other = self.root.parent / "다른 폴더"
        other.mkdir()
        self.ctl.set_root(str(other))
        self.assertEqual(self.ctl.snapshot()["settings"]["root_title"], "")
        self.ctl.set_root(str(self.root).upper())
        self.assertEqual(self.ctl.snapshot()["settings"]["root_title"], "우리 집")
        again = Controller(self.ctl.data, auth=self.auth, photos_base=self.fake.base)
        try:
            self.assertEqual(again.snapshot()["settings"]["root_title"], "우리 집")
        finally:
            again.shutdown()
        # 폴더 이름과 같거나 빈칸·탭뿐이면 따로 두지 않는다(폴더 이름을 씀)
        for same in ("사진", " \t "):
            self.ctl.set_options(root_title="우리 집")
            self.assertEqual(self.ctl.set_options(root_title=same)["root_title"], "")
            self.assertEqual(len(self.ctl.settings["root_titles"]), 0)

    def test_broken_root_titles_in_settings_file(self):
        """설정 파일의 폴더별 이름이 깨져 있어도(목록·숫자) 시작하고 빈 것으로 본다."""
        (self.ctl.data / "settings.json").write_text(
            '{"root": "", "root_titles": ["x", 1], "include_videos": false}', encoding="utf-8")
        again = Controller(self.ctl.data, auth=self.auth, photos_base=self.fake.base)
        try:
            self.assertEqual(again.settings["root_titles"], {})
            self.assertFalse(again.settings["include_videos"])
        finally:
            again.shutdown()

    def test_root_title_change_keeps_existing_album(self):
        """이름을 바꾸면 미리보기가 낡아 올리기를 막고, 다시 만들어 올려도 이미 만든 앨범을 이어 쓴다(새 앨범 없음)."""
        self.ctl.set_root(str(self.root))
        self.ctl.set_options(include_root=True, root_title="가족")
        self.scan_and_select()
        self.assertEqual(self.ctl.plan.folders[0].title, "가족_앨범")
        self.ctl.start()
        self.wait_upload()
        self.assertEqual([a["title"] for a in self.fake.albums.values()], ["가족_앨범"])
        self.ctl.set_options(root_title="우리 집")
        self.assertTrue(self.ctl.snapshot()["plan"]["stale"])
        with self.assertRaises(UserError):
            self.ctl.start()
        (self.root / "앨범" / "새사진.jpg").write_bytes(b"y" * 500)
        self.scan_and_select()
        item = self.ctl.albums()["items"][0]
        self.assertEqual((item["title"], item["album_titles"]), ("우리 집_앨범", ["가족_앨범"]))
        self.ctl.start()
        self.wait_upload()
        self.assertEqual(len(self.fake.albums), 1)
        self.assertEqual(len(next(iter(self.fake.albums.values()))["items"]), 31)

    def test_choosing_key_marks_guide_until_login(self):
        """열쇠 파일을 고르면 안내 1~7단계는 끝난 것으로 남긴다(나중에 열쇠가 깨져도 처음부터 다시 뜨지 않게)."""
        self.ctl.set_guide_step(3)
        self.ctl.choose_client("C:/다운로드/client_secret_1.json")
        self.assertEqual(self.ctl.settings["guide_step"], 8)

    def test_guide_step_clamped_and_kept(self):
        """열쇠 만드는 법의 진행 단계: 1~8로 자르고, 다시 열어도(새 컨트롤러) 남고, 다른 설정을 지우지 않는다."""
        self.ctl.set_options(include_videos=False)
        self.assertEqual(self.ctl.set_guide_step(5), 5)
        self.assertEqual(self.ctl.set_guide_step(99), 8)
        self.assertEqual(self.ctl.set_guide_step("0"), 1)
        for bad in ("다음", None, float("inf")):
            with self.assertRaises(UserError):
                self.ctl.set_guide_step(bad)
        self.ctl.set_guide_step(4)
        again = Controller(self.ctl.data, auth=self.auth, photos_base=self.fake.base)
        try:
            self.assertEqual(again.snapshot()["settings"]["guide_step"], 4)
            self.assertFalse(again.settings["include_videos"])
        finally:
            again.shutdown()


if __name__ == "__main__":
    unittest.main()
