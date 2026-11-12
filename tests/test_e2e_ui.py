"""실제 화면(ui/)과 실제 서버·컨트롤러를 Playwright로 끝까지 눌러 본다. 구글 대신 가짜 서버, 로그인 대신 가짜 계정.

GPHOTO_SHOTS=폴더 를 주면 그 폴더에 화면을 찍어 둔다. GPHOTO_HOLD=초 를 주면 끝난 뒤 그만큼 서버를 띄워 둔다(mockshot 점검용).
"""
import os
import re
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
        self.client_ok = True     # False면 열쇠 파일이 아직 없는 처음 상태

    def client_info(self):
        if not self.client_ok:
            return {"ok": False}
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
            title = page.locator("#stepTitle")
            nav = lambda n: page.locator(f".nav-item[data-step='{n}']")  # noqa: E731
            # 로그인돼 있고 폴더가 없으면 2단계에서 열고, 다른 단계는 숨긴다
            expect(title).to_have_text("사진 폴더")
            expect(page.locator("#page1")).to_be_hidden()
            expect(nav(1)).to_have_class(re.compile(r"\bdone\b"))

            # 1단계: 열쇠가 있으면 만드는 법은 접어 두고 버튼으로 연다
            nav(1).click()
            expect(page.locator("#tileLogin")).to_contain_text("tester@example.com")
            expect(page.locator("#guide")).to_be_hidden()
            page.click("#btnGuide")
            expect(page.locator("#guide")).to_be_visible()
            page.screenshot(path=str(self.shots / "01_account.png"), animations="disabled")
            page.click("#btnNext")
            expect(title).to_have_text("사진 폴더")

            # 2단계: 폴더 → 미리보기 만들기 → 다 되면 3단계로 넘어간다
            page.fill("#rootInput", str(self.root))
            page.press("#rootInput", "Enter")
            expect(page.locator("#example")).to_contain_text("사진")
            expect(page.locator("#btnScan")).to_be_enabled()
            page.screenshot(path=str(self.shots / "02_folder.png"), animations="disabled")
            page.click("#btnScan")
            expect(title).to_have_text("앨범 고르기", timeout=15000)
            expect(page.locator("#albumBody tr")).to_have_count(6)
            expect(page.locator("#stats")).to_contain_text("앨범")
            expect(page.locator("#albumBody")).to_contain_text("2024_봄 (2)")
            expect(page.locator("#footHint")).to_contain_text("1~2개")
            expect(page.locator("#btnUpload")).to_be_disabled()
            page.screenshot(path=str(self.shots / "03_albums.png"), animations="disabled")
            page.click("#btnSkipped")
            expect(page.locator("#skBody")).to_contain_text(".psd")
            page.screenshot(path=str(self.shots / "03b_skipped.png"), animations="disabled")
            page.keyboard.press("Escape")
            expect(page.locator("#skipModal")).to_be_hidden()

            # 거르기: 글자로 찾으면 '모두 선택'은 보이는 행에만 걸린다
            page.fill("#albumFilter", "2023")
            expect(page.locator("#albumBody tr")).to_have_count(2)
            expect(page.locator("#chkAllText")).to_have_text("보이는 2개 모두 선택")
            page.fill("#albumFilter", "")
            expect(page.locator("#albumBody tr")).to_have_count(6)

            # 앨범 이름 옵션을 바꾸면 미리보기가 낡았다고 알리고 올리기를 막는다(Alt+←로 2단계)
            page.evaluate("document.activeElement.blur()")
            page.keyboard.press("Alt+ArrowLeft")
            expect(title).to_have_text("사진 폴더")
            page.check("#optRoot")
            expect(page.locator("#scanText")).to_contain_text("다시 만드세요")
            expect(page.locator("#btnScan")).to_be_visible()
            nav(3).click()
            expect(page.locator("#staleBox")).to_be_visible()
            expect(page.locator("#btnUpload")).to_be_disabled()
            page.click("#btnPrev")
            page.uncheck("#optRoot")
            expect(page.locator("#scanText")).not_to_contain_text("다시 만드세요")
            page.click("#btnNext")
            expect(page.locator("#staleBox")).to_be_hidden()

            # 앨범 이름에 쓸 최상위 폴더 이름: 적는 동안 예시가 바뀌고, 적고 바로 '미리보기 만들기'를 눌러도 새 이름으로 훑는다
            page.click("#btnPrev")
            expect(title).to_have_text("사진 폴더")
            page.check("#optRoot")
            expect(page.locator("#rootTitle")).to_have_attribute("placeholder", "사진")
            page.fill("#rootTitle", "우리 집")
            expect(page.locator("#example")).to_contain_text("우리 집_2023_제주여행")
            page.screenshot(path=str(self.shots / "02b_root_title.png"), animations="disabled")
            page.click("#btnScan")
            expect(title).to_have_text("앨범 고르기", timeout=15000)
            expect(page.locator("#staleBox")).to_be_hidden()
            renamed = page.locator("#albumBody tr", has_text="우리 집_2023_제주여행")
            expect(renamed).to_contain_text("사진\\2023\\제주여행")   # 경로 칸은 실제 폴더 이름
            # 되돌린다: 이름을 비우고 옵션을 끈 뒤 다시 만든다
            page.click("#btnPrev")
            page.fill("#rootTitle", "")
            page.press("#rootTitle", "Enter")
            expect(page.locator("#example")).not_to_contain_text("우리 집")
            page.uncheck("#optRoot")
            expect(page.locator("#scanText")).to_contain_text("다시 만드세요")
            page.click("#btnScan")
            expect(title).to_have_text("앨범 고르기", timeout=15000)
            expect(page.locator("#albumBody")).not_to_contain_text("우리 집")
            expect(page.locator("#staleBox")).to_be_hidden()

            row = page.locator("#albumBody tr", has_text="2023_제주여행")
            row.locator("input[type=checkbox]").click()
            expect(page.locator("#selText")).to_contain_text("선택 1개")
            page.locator("#albumBody tr", has_text="2023_가족모임").locator("td.n").first.click()  # 행 아무 곳
            expect(page.locator("#selText")).to_contain_text("선택 2개")
            page.click("#btnUpload")
            expect(page.locator("#confirmModal")).to_be_visible()
            expect(page.locator("#cfCancel")).to_be_focused()    # Enter를 누르고 있어도 바로 시작되지 않게
            expect(page.locator("#cfBody")).to_contain_text("tester@example.com")
            expect(page.locator("#cfBody")).to_contain_text("새로 만들 앨범 2개")
            page.screenshot(path=str(self.shots / "04_confirm.png"), animations="disabled")
            page.click("#cfStart")
            expect(title).to_have_text("올리기")
            expect(page.locator("#progTitle")).to_have_text("다 올렸습니다", timeout=30000)
            expect(page.locator("#progMsg")).to_contain_text("실패 1개")
            prog = page.locator("#albumProg .row", has_text="2023_제주여행")
            expect(prog).to_contain_text("3/4")              # bad_0003 은 가짜 서버가 실패로 돌려준다
            expect(prog).to_contain_text("일부 실패")
            expect(page.locator("#albumProg .row", has_text="2023_가족모임")).to_contain_text("완료")
            expect(page.locator("#btnOpenPhotos")).to_be_visible()     # 남은 실패는 다시 해도 안 될 것뿐
            expect(page.locator("#btnResume")).to_be_hidden()
            page.screenshot(path=str(self.shots / "05_done.png"), animations="disabled")

            page.click("#btnFailures")
            expect(page.locator("#fmBody")).to_contain_text("bad_0003.JPG")
            page.screenshot(path=str(self.shots / "06_failures.png"), animations="disabled")
            page.click("#fmRetry")                           # 다시 해도 안 될 실패도 다시 시도하게
            expect(page.locator("#btnResume")).to_have_text("실패한 1개 다시 올리기")
            expect(page.locator("#btnOpenPhotos")).to_be_visible()
            expect(page.locator("#btnOpenPhotos")).not_to_have_class(re.compile(r"\bprimary\b"))
            page.click("#btnResume")
            expect(page.locator("#cfTitle")).to_have_text("이어서 올릴까요?")
            expect(page.locator("#cfBody")).to_contain_text("남은 파일")
            page.click("#cfCancel")

            page.evaluate("window.app.askClose()")
            expect(page.locator("#closeModal")).to_be_visible()
            page.screenshot(path=str(self.shots / "07_close.png"), animations="disabled")
            page.click("#clKeep")

            nav(3).click()
            expect(row).to_contain_text("일부 3/4")
            expect(row).to_contain_text("실패 1")
            expect(page.locator("#albumBody tr", has_text="2023_가족모임")).to_contain_text("올림 완료")
            page.screenshot(path=str(self.shots / "08_albums_after.png"), animations="disabled")
            page.emulate_media(color_scheme="dark")
            page.screenshot(path=str(self.shots / "09_dark_albums.png"), animations="disabled")
            nav(4).click()
            page.screenshot(path=str(self.shots / "09_dark_upload.png"), animations="disabled")
            page.emulate_media(color_scheme="light")
            page.set_viewport_size({"width": 900, "height": 640})   # 창 최소 크기
            for n in (1, 2, 3, 4):
                nav(n).click()
                page.screenshot(path=str(self.shots / f"10_narrow_{n}.png"), animations="disabled")

            self.assertEqual(sorted(a["title"] for a in self.fake.albums.values()),
                             ["2023_가족모임", "2023_제주여행"])
            self.assertEqual(self.fake.count("resumable_start"), 1)
            self.assertEqual(errors, [])
            b.close()

    def test_3_wait_stop_resume(self):
        """하루 한도로 기다리는 중 → 다른 단계의 작은 진행 버튼 → 멈추기 → 이어 올리기."""
        from playwright.sync_api import expect, sync_playwright
        ctl = self.ctl
        ctl.set_root(str(self.root))
        if ctl.plan is None or ctl._plan_stale():
            ctl.scan()
            for _ in range(150):
                if ctl.scan_state["phase"] != "scanning":
                    break
                time.sleep(0.1)
        ctl.select(all=False)
        ctl.select(keys=["스캔"], on=True)
        self.fake.add_fault("daily", "upload", times=1)
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            page = b.new_page(viewport={"width": 1120, "height": 860})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(self.srv.url + "#step=3")
            title = page.locator("#stepTitle")
            expect(title).to_have_text("앨범 고르기")
            expect(page.locator("#selText")).to_contain_text("선택 1개")
            # 키보드로 체크해도 표를 다시 그린 뒤 초점이 그 체크 칸에 남는다
            spring = page.locator("#albumBody tr", has_text="2024_봄").first.locator("input[type=checkbox]")
            spring.focus()
            page.keyboard.press("Space")
            expect(page.locator("#selText")).to_contain_text("선택 2개")
            expect(spring).to_be_focused()
            page.keyboard.press("Space")
            expect(page.locator("#selText")).to_contain_text("선택 1개")
            expect(spring).to_be_focused()
            page.click("#btnUpload")
            page.click("#cfStart")
            expect(title).to_have_text("올리기")
            expect(page.locator("#progTitle")).to_have_text("기다리는 중", timeout=10000)
            expect(page.locator("#waitBox")).to_contain_text("하루 호출 한도")
            expect(page.locator("#btnStop")).to_be_enabled()
            page.screenshot(path=str(self.shots / "11_waiting.png"), animations="disabled")
            page.locator(".nav-item[data-step='3']").click()
            expect(page.locator("#miniProg")).to_contain_text("기다리는 중")
            expect(page.locator("#albumBody input[type=checkbox]").first).to_be_disabled()   # 올리는 중엔 선택 잠금
            page.screenshot(path=str(self.shots / "12_mini_progress.png"), animations="disabled")
            page.click("#miniProg")
            expect(title).to_have_text("올리기")
            page.click("#btnStop")
            expect(page.locator("#progTitle")).to_have_text("멈춤", timeout=10000)
            expect(page.locator("#btnResume")).to_have_text("이어 올리기")
            expect(page.locator("#btnResume")).to_be_enabled()
            page.screenshot(path=str(self.shots / "13_stopped.png"), animations="disabled")
            page.click("#btnResume")
            expect(page.locator("#cfTitle")).to_have_text("이어서 올릴까요?")
            page.click("#cfStart")
            expect(page.locator("#progTitle")).to_have_text("다 올렸습니다", timeout=30000)
            expect(page.locator("#albumProg .row", has_text="스캔")).to_contain_text("완료")
            page.locator(".nav-item[data-step='3']").click()
            expect(page.locator("#albumBody input[type=checkbox]").first).to_be_enabled()    # 끝나면 선택 잠금이 풀린다
            self.assertIn("스캔", [a["title"] for a in self.fake.albums.values()])
            self.assertEqual(errors, [])
            b.close()

    def test_4_key_guide(self):
        """열쇠 만드는 법: 8단계 목록, 마쳤어요로 다음, 다시 열어도 이어서, 복사, 열쇠를 고르면 로그인 차례."""
        from playwright.sync_api import expect, sync_playwright
        auth = self.ctl.auth
        auth.client_ok, auth.logged = False, False     # 열쇠도 로그인도 없는 처음 상태
        try:
            with sync_playwright() as pw:
                b = pw.chromium.launch()
                ctx = b.new_context(viewport={"width": 1120, "height": 860})
                ctx.grant_permissions(["clipboard-read", "clipboard-write"])
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(self.srv.url)
                step = lambda n: page.locator(f"#guide .g-step[data-n='{n}']")  # noqa: E731
                item = lambda n: page.locator(f"#guideList .g-item[data-n='{n}']")  # noqa: E731
                expect(page.locator("#stepTitle")).to_have_text("계정 연결")
                expect(page.locator("#guide")).to_be_visible()
                expect(page.locator("#guideList .g-item")).to_have_count(8)
                expect(step(1)).to_be_visible()
                expect(page.locator("#guideCount")).to_contain_text("1 / 8")
                expect(step(1).locator(".g-help")).to_be_hidden()
                page.click("#gHelp")
                expect(step(1).locator(".g-help")).to_be_visible()
                page.screenshot(path=str(self.shots / "14_guide_start.png"), animations="disabled")
                page.click("#gNext")
                expect(step(2)).to_be_visible()
                expect(page.locator("#guideCount")).to_contain_text("2 / 8")
                expect(item(1)).to_have_class(re.compile(r"\bdone\b"))
                page.reload()                                  # 다시 열어도 이어서
                expect(step(2)).to_be_visible()
                expect(page.locator("#guideCount")).to_contain_text("2 / 8")
                item(5).click()                                # 앞 단계를 미리 봐도 차례는 그대로
                expect(step(5)).to_contain_text("데이터 액세스")
                expect(page.locator("#guideCount")).to_contain_text("2 / 8")
                page.click("#guide [data-copy='scopeList']")
                expect(page.locator("#toast")).to_have_text("복사했습니다")
                clip = page.evaluate("navigator.clipboard.readText()")
                self.assertIn("photoslibrary.appendonly", clip)
                self.assertIn("photoslibrary.readonly.appcreateddata", clip)
                page.screenshot(path=str(self.shots / "15_guide_scopes.png"), animations="disabled")
                item(6).click()
                expect(step(6).locator(".g-warn")).to_contain_text("다시 받을 수 없습니다")
                page.locator("#gNext").focus()
                page.keyboard.press("Enter")                   # 키보드로 6단계 '마쳤어요'
                expect(step(7)).to_be_visible()
                expect(item(7)).to_be_focused()                # 숨는 버튼 대신 목록 줄로 초점이 간다
                expect(page.locator("#guideCount")).to_contain_text("7 / 8")
                # 창 최소 크기에서 가장 긴 3단계: 설명은 안에서 스크롤해도 '마쳤어요'는 보인다
                page.set_viewport_size({"width": 900, "height": 640})
                item(3).click()
                page.click("#gHelp")
                box = page.locator("#gNext").bounding_box()
                self.assertIsNotNone(box)
                self.assertLessEqual(box["y"] + box["height"], 640)
                page.screenshot(path=str(self.shots / "16_guide_narrow.png"), animations="disabled")
                # 열쇠 파일을 고르면(앱이 알아챔) 1~7 끝, 로그인 차례
                auth.client_ok = True
                expect(page.locator("#guideCount")).to_contain_text("8 / 8", timeout=5000)
                expect(step(8)).to_be_visible()
                expect(step(8).locator("[data-proxy='btnLogin']")).to_be_enabled()
                expect(item(7)).to_have_class(re.compile(r"\bdone\b"))
                self.assertEqual(self.ctl.settings["guide_step"], 7)   # 저장값은 사용자가 '마쳤어요'로 간 곳 그대로
                self.assertEqual(errors, [])
                b.close()
        finally:
            auth.client_ok, auth.logged = True, True


if __name__ == "__main__":
    unittest.main()
