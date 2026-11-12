"""화면 점검: 시험 흐름으로 '다 올렸습니다' 상태까지 간 뒤 mockshot으로 폭마다 잘림·넘침·겹침을 잰다.

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
    mockshot = Path.home() / ".claude" / "tools" / "mockshot.py"
    # mockshot은 시스템 파이썬(Playwright 있음)으로 돌린다
    res = subprocess.run(["python", str(mockshot), T.E2ETest.srv.url, "-w", "1120,900,720",
                          "-o", str(out / "mockshot"), "--wait-ms", "1500", "--scroll"],
                         capture_output=True, text=True, encoding="utf-8")
    print(res.stdout[-6000:])
    print(res.stderr[-2000:])
finally:
    T.E2ETest.tearDownClass()
