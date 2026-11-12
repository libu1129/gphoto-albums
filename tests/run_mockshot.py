"""화면 점검: 시험 흐름으로 '다 올렸습니다' 상태까지 간 뒤 mockshot으로 단계(1~4)·폭마다 잘림·넘침·겹침을 잰다.

사용: .venv\\Scripts\\python.exe tests\\run_mockshot.py <출력 폴더>
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_e2e_ui as T  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "_mockshot")
os.environ.setdefault("GPHOTO_SHOTS", str(out / "flow"))
T.E2ETest.setUpClass()
try:
    # unittest 실행기를 쓰면 클래스 준비·정리가 저절로 다시 돌아 서버가 먼저 닫힌다. 메서드를 직접 부른다.
    T.E2ETest("test_2_full_flow").test_2_full_flow()
    T.E2ETest("test_3_wait_stop_resume").test_3_wait_stop_resume()
    T.E2ETest("test_4_key_guide").test_4_key_guide()
    # 1단계에 '열쇠 만드는 법'이 펴져 보이게 열쇠·로그인이 없는 처음 상태로 둔다(5단계 차례: 가장 긴 설명 중 하나)
    T.E2ETest.ctl.auth.client_ok, T.E2ETest.ctl.auth.logged = False, False
    T.E2ETest.ctl.set_guide_step(5)
    mockshot = Path.home() / ".claude" / "tools" / "mockshot.py"
    targets = [f"{T.E2ETest.srv.url}#step={n}" for n in (1, 2, 3, 4)]
    # 창 기본 크기, 창 최소 크기, 그보다 좁은 브라우저 대비책
    for width, height in ((1120, 860), (900, 640), (720, 640)):
        # mockshot은 시스템 파이썬(Playwright 있음)으로 돌린다
        res = subprocess.run(["python", str(mockshot), *targets, "-w", str(width), "--height", str(height),
                              "-o", str(out / f"mockshot_{width}x{height}"), "--wait-ms", "1500", "--scroll"],
                             capture_output=True, text=True, encoding="utf-8")
        print(f"== {width}x{height} ==")
        print(res.stdout[-6000:])
        print(res.stderr[-2000:])
finally:
    T.E2ETest.tearDownClass()
