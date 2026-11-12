"""실제 화면(ui/)과 실제 서버·컨트롤러를 Playwright로 끝까지 눌러 본다. 구글 대신 가짜 서버, 로그인 대신 가짜 계정.

GPHOTO_SHOTS=폴더 를 주면 그 폴더에 화면을 찍어 둔다. GPHOTO_HOLD=초 를 주면 끝난 뒤 그만큼 서버를 띄워 둔다(mockshot 점검용).
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(HERE))

from fake_photos import FakePhotos  # noqa: E402
from gphoto.auth import SCOPE_APPEND, SCOPE_READ  # noqa: E402
from gphoto.controller import Controller  # noqa: E402
from gphoto.server import AppServer  # noqa: E402


class FakeAuth:
    def __init__(self):
        self.problem = None
        self.logged = True

    def client_info(self):
        return {"ok": True, "project": "photo-upload-123", "client": "123456789012"}

    def account(self):
        if not self.logged:
            return None
        return {"email": "tester@example.com", "sub": "sub-1", "scopes": [SCOPE_APPEND, SCOPE_READ]}

    def session(self):
        return requests.Session()

    def login(self, timeout=300):
        self.logged = True
        return self.account()

    def cancel_login(self):
        pass

    def logout(self, revoke=True):
        self.logged = False

    def import_client(self, path):
        return self.client_info()


def touch(p: Path, size=2000):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(size))


class E2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.root = base / "사진"
        touch(cls.root / "2023" / "제주여행" / "IMG_0001.JPG")
        touch(cls.root / "2023" / "제주여행" / "IMG_0002.JPG")
        touch(cls.root / "2023" / "제주여행" / "bad_0003.JPG")
        touch(cls.root / "2023" / "제주여행" / "VID_0004.MP4", 250_000)
        touch(cls.root / "2023" / "가족모임" / "a.heic")
        touch(cls.root / "2024" / "아이 돌잔치 (스튜디오 원본, 보정 전 사진 모음)" / "b.jpg")
        touch(cls.root / "2024" / "아이 돌잔치 (스튜디오 원본, 보정 전 사진 모음)" / "c.jpg")
        touch(cls.root / "2024_봄" / "d.png")
        touch(cls.root / "2024" / "봄" / "e.png")      # 2024_봄 과 이름이 겹친다
        touch(cls.root / "2024" / "봄" / "notes.psd")
        touch(cls.root / "스캔" / "f.tif")
        (base / "data").mkdir()
        cls.fake = FakePhotos()
        cls.ctl = Controller(base / "data", auth=FakeAuth(), photos_base=cls.fake.base,
                             client_kwargs=dict(resumable_min=100_000))
        cls.srv = AppServer(cls.ctl, APP / "ui")
        cls.srv.start()
        cls.shots = Path(os.environ.get("GPHOTO_SHOTS") or (base / "shots"))
        cls.shots.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        hold = float(os.environ.get("GPHOTO_HOLD") or 0)
        if hold:
            print("HOLD_URL", cls.srv.url, flush=True)
            time.sleep(hold)
        cls.srv.stop()
        cls.ctl.shutdown()
        cls.fake.close()
        cls.tmp.cleanup()

    def test_1_api_guard(self):
        base = f"http://127.0.0.1:{self.srv.port}"
        self.assertEqual(requests.get(base + "/api/state").status_code, 403)                 # 토큰 없음
        r = requests.get(base + "/api/state", headers={"X-App-Token": self.srv.token,
                                                       "Host": "evil.example:80"})
        self.assertEqual(r.status_code, 403)                                                 # Host 위조
        r = requests.get(base + "/api/state", headers={"X-App-Token": self.srv.token})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(requests.get(base + "/../gphoto/auth.py").status_code, 404)         # 폴더 밖 파일

    def test_2_full_flow(self):
        from playwright.sync_api import expect, sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            page = b.new_page(viewport={"width": 1120, "height": 860})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(self.srv.url)
            expect(page.locator("#accountText")).to_have_text("tester@example.com")
            page.screenshot(path=str(self.shots / "01_start.png"), full_page=True)

            page.fill("#rootInput", str(self.root))
            page.press("#rootInput", "Enter")
            expect(page.locator("#example")).to_contain_text("사진")
            page.click("#btnScan")
            expect(page.locator("#albumBody tr")).to_have_count(6, timeout=15000)
            expect(page.locator("#stats")).to_contain_text("앨범")
            expect(page.locator("#skipped")).to_contain_text(".psd")
            expect(page.locator("#albumBody")).to_contain_text("2024_봄 (2)")
            expect(page.locator("#uploadHint")).to_contain_text("1~2개")
            page.screenshot(path=str(self.shots / "02_preview.png"), full_page=True)

            # 앨범 이름 옵션: 최상위 폴더 이름 넣기 → 미리보기가 낡았다고 알림 → 다시 만들기
            page.check("#optRoot")
            expect(page.locator("#scanText")).to_contain_text("다시 만드세요")
            expect(page.locator("#btnUpload")).to_be_disabled()
            page.uncheck("#optRoot")
            expect(page.locator("#scanText")).to_have_text("")

            row = page.locator("#albumBody tr", has_text="2023_제주여행")
            row.locator("input[type=checkbox]").click()
            expect(page.locator("#selText")).to_contain_text("선택 1개")
            page.locator("#albumBody tr", has_text="2023_가족모임").locator("input[type=checkbox]").click()
            expect(page.locator("#selText")).to_contain_text("선택 2개")
            page.click("#btnUpload")
            expect(page.locator("#confirmModal")).to_be_visible()
            expect(page.locator("#cfBody")).to_contain_text("tester@example.com")
            expect(page.locator("#cfBody")).to_contain_text("새로 만들 앨범 2개")
            page.screenshot(path=str(self.shots / "03_confirm.png"))
            page.click("#cfStart")
            expect(page.locator("#progTitle")).to_have_text("다 올렸습니다", timeout=30000)
            expect(row).to_contain_text("일부 3/4")           # bad_0003 은 가짜 서버가 실패로 돌려준다
            expect(row).to_contain_text("실패 1")
            expect(page.locator("#albumBody tr", has_text="2023_가족모임")).to_contain_text("올림 완료")
            expect(page.locator("#progMsg")).to_contain_text("실패 1개")
            page.screenshot(path=str(self.shots / "04_done.png"), full_page=True)

            page.click("#btnFailures")
            expect(page.locator("#fmBody")).to_contain_text("bad_0003.JPG")
            page.screenshot(path=str(self.shots / "05_failures.png"))
            page.click("#fmClose")

            page.evaluate("window.app.askClose()")
            expect(page.locator("#closeModal")).to_be_visible()
            page.screenshot(path=str(self.shots / "06_close.png"))
            page.click("#clKeep")

            page.click("#btnGuide")
            expect(page.locator("#guide")).to_be_visible()
            page.emulate_media(color_scheme="dark")
            page.screenshot(path=str(self.shots / "07_dark_guide.png"), full_page=True)
            page.emulate_media(color_scheme="light")
            page.set_viewport_size({"width": 900, "height": 700})
            page.screenshot(path=str(self.shots / "08_narrow.png"), full_page=True)

            self.assertEqual(sorted(a["title"] for a in self.fake.albums.values()),
                             ["2023_가족모임", "2023_제주여행"])
            self.assertEqual(self.fake.count("resumable_start"), 1)
            self.assertEqual(errors, [])
            b.close()


if __name__ == "__main__":
    unittest.main()
