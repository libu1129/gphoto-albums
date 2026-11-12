"""폴더 훑기·앨범 이름 규칙 시험(구글 없이)."""
import ctypes
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gphoto import scan as S  # noqa: E402


def touch(p: Path, size=10):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "사진"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def titles(self, res):
        return {fp.key: fp.title for fp in res.folders}

    def test_names_without_and_with_root(self):
        touch(self.root / "2023" / "제주여행" / "a.jpg")
        touch(self.root / "2023" / "b.png")
        touch(self.root / "c.heic")
        res = S.scan(self.root)
        self.assertEqual(self.titles(res), {"": "사진", "2023": "2023", "2023/제주여행": "2023_제주여행"})
        res2 = S.scan(self.root, include_root=True)
        self.assertEqual(self.titles(res2), {"": "사진", "2023": "사진_2023",
                                             "2023/제주여행": "사진_2023_제주여행"})
        self.assertEqual([fp.key for fp in res.folders], ["", "2023", "2023/제주여행"])

    def test_nfd_folder_becomes_nfc_title(self):
        nfd = unicodedata.normalize("NFD", "여행")
        touch(self.root / nfd / "a.jpg")
        res = S.scan(self.root)
        self.assertEqual(res.folders[0].title, unicodedata.normalize("NFC", "여행"))
        self.assertTrue(res.folders[0].files[0].path.endswith(os.path.join(nfd, "a.jpg")))  # 파일 키는 원래 경로

    def test_collision_gets_suffix(self):
        touch(self.root / "x_y" / "a.jpg")
        touch(self.root / "x" / "y" / "b.jpg")
        res = S.scan(self.root)
        titles = sorted(fp.title for fp in res.folders)
        self.assertEqual(titles, ["x_y", "x_y (2)"])
        self.assertTrue(any("(2)" in w for fp in res.folders for w in fp.warnings))

    def test_split_and_truncate(self):
        for i in range(7):
            touch(self.root / "많음" / f"{i}.jpg")
        long_name = "가" * 40
        touch(self.root / long_name / "a.jpg")
        res = S.scan(self.root, album_max=3, title_max=20)
        many = next(fp for fp in res.folders if fp.key == "많음")
        self.assertEqual(many.parts, 3)
        self.assertTrue(any("나눕니다" in w for w in many.warnings))
        lng = next(fp for fp in res.folders if fp.key == long_name)
        self.assertEqual(len(lng.title), 20)
        self.assertTrue(lng.title.startswith("…"))
        self.assertEqual(S.part_title("a" * 20, 2, 20), "…" + "a" * 16 + " #2")

    def test_skips_and_filters(self):
        touch(self.root / "a.jpg")
        touch(self.root / "._a.jpg")                 # 맥 부속 파일
        touch(self.root / "Thumbs.db")
        touch(self.root / "@eaDir" / "t.jpg")        # 시놀로지 썸네일
        touch(self.root / ".hidden" / "h.jpg")
        touch(self.root / "doc.psd")                 # 지원 안 함
        touch(self.root / "note.txt")                # 조용히 넘김
        touch(self.root / "empty.jpg", 0)
        touch(self.root / "big.jpg", 150)            # 사진 한도 100으로 시험
        touch(self.root / "clip.mp4", 50)
        hid = self.root / "숨김"
        touch(hid / "x.jpg")
        ctypes.windll.kernel32.SetFileAttributesW(str(hid), 0x2)
        res = S.scan(self.root, include_videos=False, photo_max=100)
        names = [f.name for fp in res.folders for f in fp.files]
        self.assertEqual(names, ["a.jpg"])
        self.assertEqual(res.skipped.get("지원하지 않는 형식"), 1)
        self.assertEqual(res.skipped_ext, {".psd": 1})
        self.assertEqual(res.skipped.get("빈 파일"), 1)
        self.assertEqual(res.skipped.get("사진 200MB 초과"), 1)
        self.assertEqual(res.skipped.get("영상 제외(설정)"), 1)
        res2 = S.scan(self.root, include_videos=True, photo_max=100)
        self.assertIn("clip.mp4", [f.name for fp in res2.folders for f in fp.files])

    def test_junction_not_followed(self):
        touch(self.root / "real" / "a.jpg")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.root / "link"), str(self.root / "real")],
                           capture_output=True)
        if r.returncode != 0:
            self.skipTest("정션을 만들 수 없음")
        res = S.scan(self.root)
        self.assertEqual([fp.key for fp in res.folders], ["real"])
        self.assertEqual(res.skipped.get("바로가기 링크(따라가지 않음)"), 1)

    def test_natural_order_and_estimate(self):
        for n in (10, 2, 1):
            touch(self.root / f"IMG_{n}.jpg")
        res = S.scan(self.root)
        self.assertEqual([f.name for f in res.folders[0].files], ["IMG_1.jpg", "IMG_2.jpg", "IMG_10.jpg"])
        self.assertEqual(res.est_requests, 3 + 1 + 1)  # 업로드 3 + batchCreate 1 + 앨범 1

    def test_extensionless_files_are_sniffed(self):
        d = self.root / "내보내기"
        d.mkdir()
        (d / "IMG_0001").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 60)                       # JPEG
        (d / "IMG_0002").write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 40)
        (d / "IMG_0003").write_bytes(b"\x00\x00\x00\x1cftypmif1\x00\x00\x00\x00mif1heicmiaf" + b"\x00" * 40)
        (d / "IMG_0004").write_bytes(b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + b"\x00" * 40)
        (d / "IMG_0005").write_bytes(b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + b"\x00" * 40)   # MOV
        (d / "IMG_0006").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
        (d / "README").write_bytes(b"just some text without extension")                         # 모름
        res = S.scan(self.root)
        fp = next(x for x in res.folders if x.key == "내보내기")
        names = {f.name: (f.kind, f.upload_name) for f in fp.files}
        self.assertEqual(names, {
            "IMG_0001": ("photo", "IMG_0001.jpg"), "IMG_0002": ("photo", "IMG_0002.heic"),
            "IMG_0003": ("photo", "IMG_0003.heic"), "IMG_0004": ("photo", "IMG_0004.avif"),
            "IMG_0005": ("video", "IMG_0005.mov"), "IMG_0006": ("photo", "IMG_0006.png")})
        self.assertTrue(any("내용으로 형식을" in w for w in fp.warnings))
        self.assertEqual(res.skipped_ext, {"": 1})                 # 화면에 '확장자 없음 1'로 뜬다
        self.assertEqual(res.skipped.get("지원하지 않는 형식"), 1)
        res2 = S.scan(self.root, include_videos=False)             # 영상 끔이면 MOV는 빠지고 경고 예시는 남은 사진에서
        fp2 = next(x for x in res2.folders if x.key == "내보내기")
        self.assertNotIn("IMG_0005", [f.name for f in fp2.files])
        self.assertEqual(res2.skipped.get("영상 제외(설정)"), 1)

    def test_sniff_signatures(self):
        cases = {
            b"\xff\xd8\xff\xdb": ("photo", ".jpg"),
            b"\x89PNG\r\n\x1a\n": ("photo", ".png"),
            b"GIF89a": ("photo", ".gif"),
            b"RIFF\x00\x00\x00\x00WEBPVP8 ": ("photo", ".webp"),
            b"RIFF\x00\x00\x00\x00AVI LIST": ("video", ".avi"),
            b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic": ("photo", ".heic"),
            b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1heic": ("photo", ".heic"),
            b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1": ("photo", ".avif"),
            b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1avif": ("photo", ".avif"),
            b"\x00\x00\x00\x14ftypmif1\x00\x00\x00\x00mif1": ("photo", ".heif"),
            b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ": ("video", ".mov"),
            b"\x00\x00\x00\x14ftyp3gp4\x00\x00\x00\x00isom": ("video", ".3gp"),
            b"\x00\x00\x00\x14ftypM4V \x00\x00\x00\x00isom": ("video", ".m4v"),
            b"\x00\x00\x00\x14ftypisom\x00\x00\x02\x00isom": ("video", ".mp4"),
            # 머리만으로 가를 수 없어 판별하지 않는 것
            b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c": None,  # WMV·WMA 공통 ASF
            b"\x1a\x45\xdf\xa3\x01": None,                                              # MKV·WebM 공통 EBML
            b"II*\x00\x08\x00\x00\x00": None,                                           # TIFF·RAW
            b"BM text that is not a bitmap": None,
            b"\x00\x00\x00\x18ftypcrx \x00\x00\x00\x01crx ": None,
            b"ab": None,
        }
        for i, (head, want) in enumerate(cases.items()):
            p = self.root / f"s{i}"
            p.write_bytes(head + b"\x00" * 40)
            self.assertEqual(S.sniff(str(p)), want, head)

    def test_extensionless_empty_and_unreadable(self):
        touch(self.root / "빈것", 0)
        locked = self.root / "잠김"
        locked.write_bytes(b"\xff\xd8\xff" + b"\x00" * 40)
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = ctypes.c_void_p
        h = k32.CreateFileW(str(locked), 0x80000000, 0, None, 3, 0, None)  # 공유 없이 열어 둠
        try:
            res = S.scan(self.root)
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
        self.assertEqual(res.skipped.get("빈 파일"), 1)
        self.assertTrue(any("잠김" in e for e in res.errors))          # 조용히 빠지지 않고 '읽지 못한 곳'에 뜬다
        self.assertEqual(res.files, 0)

    def test_root_label(self):
        self.assertEqual(S.root_label("D:\\"), "D")
        self.assertEqual(S.root_label("\\\\NAS\\photo"), "photo")
        self.assertEqual(S.root_label("\\\\NAS\\photo\\2023"), "2023")
        self.assertEqual(S.root_label("C:\\Users\\me\\Pictures"), "Pictures")

    def test_missing_root(self):
        with self.assertRaises(S.ScanError):
            S.scan(self.root / "없음")


if __name__ == "__main__":
    unittest.main()
