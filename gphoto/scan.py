"""폴더를 훑어 앨범 계획을 만든다. 구글에 아무것도 보내지 않고 PC 안에서만 돈다."""
from __future__ import annotations

import math
import os
import re
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

# 구글 포토 API 문서(upload-media)와 도움말(answer/6193313)의 형식 목록을 합쳤다.
PHOTO_EXT = {".jpg", ".jpeg", ".jpe", ".jfif", ".heic", ".heif", ".png", ".webp", ".gif",
             ".avif", ".bmp", ".ico", ".tif", ".tiff"}
# 도움말은 "most RAW files"라고만 적어 흔한 RAW 확장자를 넣었다. 구글이 안 받으면 올릴 때 실패로 남는다.
RAW_EXT = {".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf",
           ".rw2", ".raf", ".pef", ".srw", ".x3f", ".3fr", ".rwl", ".kdc", ".erf", ".mrw",
           ".mos", ".iiq"}
VIDEO_EXT = {".mpg", ".mpeg", ".mod", ".mmv", ".tod", ".wmv", ".asf", ".avi", ".divx", ".mov",
             ".m4v", ".3gp", ".3g2", ".mp4", ".m2t", ".m2ts", ".mts", ".mkv"}
# 미리보기 목록에 띄우지 않고 조용히 넘기는 부속 파일. 확장자 없는 파일은 여기 넣지 않고 내용으로 형식을 알아낸다.
QUIET_EXT = {".ini", ".db", ".xmp", ".aae", ".thm", ".lnk", ".url", ".json", ".xml",
             ".txt", ".pp3", ".dop", ".on1", ".lrv", ".ctg", ".dat", ".tmp", ".log", ".md5",
             ".bak"}
QUIET_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db", "zbthumbnail.info"}
# NAS·휴지통·썸네일 폴더. 점(.)으로 시작하는 폴더도 건너뛴다.
SKIP_DIRS = {"@eadir", "#recycle", "#snapshot", "@recycle", "@__thumb", "$recycle.bin",
             "system volume information", "@sharebin", "@tmp"}

PHOTO_MAX = 200 * 1024 ** 2      # 사진 200MB (API 문서)
VIDEO_MAX = 20 * 1024 ** 3       # 영상 20GB (API 문서)
ALBUM_MAX = 20_000               # 앨범당 항목 수 (manage-albums 문서)
TITLE_MAX = 500                  # 앨범 제목 길이 (manage-albums 문서)
BATCH = 50                       # batchCreate 한 번에 넣는 수 (upload-media 문서)
RESUMABLE_MIN = 50 * 1024 ** 2   # 이보다 크면 이어 올리기 방식 (문서: 50MB 넘으면 성능 문제 가능)
DAILY_QUOTA = 10_000             # 프로젝트당 하루 요청 한도 (api-limits-quotas 문서)

_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2)
_SYSTEM = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 4)
# 클라우드 전용 자리표시 파일(열면 내려받기가 시작됨): OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS
_CLOUD_ONLY = 0x1000 | 0x40000 | 0x400000


class ScanCancelled(Exception):
    pass


class ScanError(Exception):
    pass


@dataclass
class MediaFile:
    path: str
    name: str
    size: int
    mtime_ns: int
    kind: str  # 'photo' | 'video'
    upload_name: str = ""  # 구글에 넘길 이름. 확장자 없는 파일은 내용으로 알아낸 확장자를 붙인다

    def __post_init__(self):
        if not self.upload_name:
            self.upload_name = self.name


@dataclass
class FolderPlan:
    key: str                 # 최상위 기준 상대 경로('/'로 이음). 최상위 바로 아래 파일은 ''
    title: str
    files: list = field(default_factory=list)
    photos: int = 0
    videos: int = 0
    bytes: int = 0
    parts: int = 1
    warnings: list = field(default_factory=list)


@dataclass
class ScanResult:
    root: str
    root_name: str
    include_root: bool
    include_videos: bool
    folders: list
    skipped: dict            # 까닭 → 개수
    skipped_ext: dict        # 지원하지 않는 확장자 → 개수
    examples: list           # (상대 경로, 까닭) 앞의 몇 개
    errors: list
    files: int = 0
    photos: int = 0
    videos: int = 0
    bytes: int = 0
    est_requests: int = 0
    elapsed: float = 0.0


_num = re.compile(r"(\d+)")


def natural_key(s: str):
    """IMG_2 가 IMG_10 보다 앞에 오게 정렬하는 키. split 결과의 홀수 자리가 숫자다."""
    return [int(p) if i % 2 else p.casefold() for i, p in enumerate(_num.split(s))]


def long_path(p: str) -> str:
    """260자 넘는 경로도 열리게 \\\\?\\ 접두어를 붙인다."""
    if os.name != "nt" or len(p) < 240 or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def normalize_root(root) -> str:
    root = str(root).strip().strip('"').strip()
    if re.fullmatch(r"[A-Za-z]:", root):
        root += "\\"
    return os.path.abspath(root)


def root_label(root: str) -> str:
    """앨범 이름에 쓸 최상위 폴더 이름. 드라이브 루트면 드라이브 문자, UNC 공유 루트면 공유 이름."""
    p = PureWindowsPath(root)
    if p.name:
        return p.name
    drive = p.drive
    if drive.startswith("\\\\"):
        parts = [x for x in drive.split("\\") if x]
        return parts[-1] if parts else "root"
    return drive.rstrip(":") or "root"


def album_title(key: str, root_name: str, include_root: bool) -> str:
    parts = [x for x in key.split("/") if x]
    if include_root or not parts:
        parts = [root_name] + parts
    return unicodedata.normalize("NFC", "_".join(parts))


def fit_title(title: str, suffix: str = "", limit: int = TITLE_MAX) -> str:
    """제목이 길면 앞을 잘라 '…'를 붙인다. 깊은 폴더 이름이 더 알아보기 쉬워서 끝을 남긴다."""
    if len(title) + len(suffix) <= limit:
        return title + suffix
    keep = max(1, limit - len(suffix) - 1)
    return "…" + title[-keep:] + suffix


def part_title(title: str, part: int, limit: int = TITLE_MAX) -> str:
    """2만 장이 넘어 나눈 앨범의 이름. 1번은 그대로, 2번부터 ' #2'."""
    if part <= 1:
        return title
    return fit_title(title, f" #{part}", limit)


def estimate_requests(n_files: int, n_big: int, parts: int) -> int:
    """파일마다 업로드 1건, 50개마다 batchCreate 1건, 앨범 만들기, 큰 파일은 시작 요청 1건 더."""
    return n_files + math.ceil(n_files / BATCH) + parts + n_big


_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"}
_MP4_BRANDS = {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash",
               b"MSNV", b"XAVC", b"mmp4"}


def sniff(path: str):
    """확장자 없는 파일의 머리 바이트로 형식을 알아낸다. (종류, 붙일 확장자) 또는 모르면 None. 못 읽으면 OSError.
    근거: JPEG FF D8 FF, PNG 89 50 4E 47 0D 0A 1A 0A, GIF8, RIFF/WEBP·AVI,
    ISO BMFF ftyp 상자(주 브랜드와 호환 브랜드: heic·avif·qt·isom 등).
    머리만으로 가를 수 없는 형식은 판별하지 않는다(화면에 '확장자 없음'으로 뜬다):
    TIFF(NEF·DNG 같은 RAW와 머리가 같음), BMP(머리 검사가 약함), MKV(WebM과 같음), WMV(WMA 음악과 같음)."""
    with open(long_path(path), "rb") as f:
        h = f.read(64)
    if h.startswith(b"\xff\xd8\xff"):
        return "photo", ".jpg"
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "photo", ".png"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "photo", ".gif"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "photo", ".webp"
    if h[:4] == b"RIFF" and h[8:12] == b"AVI ":
        return "video", ".avi"
    if h[4:8] == b"ftyp" and len(h) >= 12:
        major = h[8:12]
        box = min(int.from_bytes(h[0:4], "big"), len(h))
        brands = {major} | {h[i:i + 4] for i in range(16, box - 3, 4)}
        if major in (b"avif", b"avis"):
            return "photo", ".avif"
        if brands & _HEIC_BRANDS:
            return "photo", ".heic"
        if b"avif" in brands:
            return "photo", ".avif"
        if major in (b"mif1", b"msf1"):
            return "photo", ".heif"
        if major == b"qt  ":
            return "video", ".mov"
        if major[:3] in (b"3gp", b"3g2", b"3gg"):
            return "video", ".3gp"
        if major in (b"M4V ", b"M4VH", b"M4VP"):
            return "video", ".m4v"
        if major in _MP4_BRANDS:
            return "video", ".mp4"
    return None


def _skip_dir(name: str, attrs: int) -> bool:
    low = name.lower()
    if low in SKIP_DIRS or low.startswith("."):
        return True
    return bool(attrs & (_HIDDEN | _SYSTEM))


def scan(root, include_root=False, include_videos=True, progress=None, stop=None, *,
         photo_max=PHOTO_MAX, video_max=VIDEO_MAX, album_max=ALBUM_MAX, title_max=TITLE_MAX,
         resumable_min=RESUMABLE_MIN, max_examples=300) -> ScanResult:
    t0 = time.monotonic()
    root = normalize_root(root)
    if not os.path.isdir(long_path(root)):
        raise ScanError("폴더를 찾을 수 없습니다: " + root)
    rname = root_label(root)
    groups: dict[str, list[MediaFile]] = {}
    skipped: dict[str, int] = {}
    skipped_ext: dict[str, int] = {}
    examples: list = []
    errors: list = []
    sniffed: dict[str, int] = {}  # 폴더 → 내용으로 형식을 알아낸 파일 수
    scanned = 0
    last_report = 0.0

    def skip(reason, rel):
        skipped[reason] = skipped.get(reason, 0) + 1
        if len(examples) < max_examples:
            examples.append((rel, reason))

    stack = [(root, "")]
    while stack:
        if stop is not None and stop.is_set():
            raise ScanCancelled()
        d, key = stack.pop()
        try:
            with os.scandir(long_path(d)) as it:
                entries = list(it)
        except OSError as e:
            errors.append(f"{d}: {e.strerror or e}")
            continue
        entries.sort(key=lambda e: natural_key(e.name))
        subdirs = []
        files = []
        for e in entries:
            name = e.name
            rel = f"{key}/{name}" if key else name
            try:
                if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                    skip("바로가기 링크(따라가지 않음)", rel)
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError as ex:
                errors.append(f"{os.path.join(d, name)}: {ex.strerror or ex}")
                continue
            attrs = getattr(st, "st_file_attributes", 0)
            if stat.S_ISDIR(st.st_mode):
                if not _skip_dir(name, attrs):
                    subdirs.append((os.path.join(d, name), rel))
                continue
            scanned += 1
            low = name.lower()
            if low.startswith("._") or low in QUIET_NAMES or attrs & (_HIDDEN | _SYSTEM):
                continue
            ext = os.path.splitext(low)[1]
            upload_name = name
            if not ext:  # 확장자 없는 파일(사진 앱 내보내기 등): 내용으로 형식을 알아낸다
                if st.st_size == 0:
                    skip("빈 파일", rel)
                    continue
                if attrs & _CLOUD_ONLY:  # 미리보기 때문에 내려받기가 시작되지 않게 열지 않는다
                    found = None
                else:
                    if stop is not None and stop.is_set():
                        raise ScanCancelled()
                    try:
                        found = sniff(os.path.join(d, name))
                    except OSError as ex:  # 잠긴 파일 등: 조용히 빼지 않고 '읽지 못한 곳'에 띄운다
                        errors.append(f"{os.path.join(d, name)}: {ex.strerror or ex}")
                        continue
                if found is None:
                    skipped_ext[""] = skipped_ext.get("", 0) + 1
                    skip("지원하지 않는 형식", rel)
                    continue
                kind, add = found
                upload_name = name + add
            elif ext in PHOTO_EXT or ext in RAW_EXT:
                kind = "photo"
            elif ext in VIDEO_EXT:
                kind = "video"
            elif ext in QUIET_EXT:
                continue
            else:
                skipped_ext[ext] = skipped_ext.get(ext, 0) + 1
                skip("지원하지 않는 형식", rel)
                continue
            size = st.st_size
            if kind == "video" and not include_videos:
                skip("영상 제외(설정)", rel)
            elif size == 0:
                skip("빈 파일", rel)
            elif kind == "photo" and size > photo_max:
                skip("사진 200MB 초과", rel)
            elif kind == "video" and size > video_max:
                skip("영상 20GB 초과", rel)
            else:
                files.append(MediaFile(os.path.join(d, name), name, size, st.st_mtime_ns, kind,
                                       upload_name))
                if upload_name != name:  # 실제로 올릴 목록에 든 것만 센다(경고 문구의 예시가 이 안에서 나온다)
                    sniffed[key] = sniffed.get(key, 0) + 1
        if files:
            groups[key] = files
        stack.extend(reversed(subdirs))
        if progress is not None:
            now = time.monotonic()
            if now - last_report > 0.25:
                last_report = now
                progress(scanned, d)

    keys = sorted(groups, key=lambda k: [natural_key(p) for p in k.split("/")] if k else [])
    folders = []
    used: set[str] = set()
    tot_files = tot_photos = tot_bytes = tot_req = 0
    for k in keys:
        fl = groups[k]
        base = album_title(k, rname, include_root)
        warnings = []
        title = fit_title(base, "", title_max)
        if title != base:
            warnings.append(f"앨범 이름이 {title_max}자를 넘어 앞부분을 줄였습니다")
        if title in used:
            n = 2
            while fit_title(base, f" ({n})", title_max) in used:
                n += 1
            title = fit_title(base, f" ({n})", title_max)
            warnings.append(f"같은 이름의 앨범이 있어 ' ({n})'을 붙였습니다")
        used.add(title)
        parts = max(1, math.ceil(len(fl) / album_max))
        if parts > 1:
            warnings.append(f"파일이 {album_max:,}개를 넘어 앨범 {parts}개로 나눕니다('{part_title(title, 2, title_max)}' …)")
            for p in range(2, parts + 1):
                used.add(part_title(title, p, title_max))
        if sniffed.get(k):
            ex = next(f for f in fl if f.upload_name != f.name)
            warnings.append(f"확장자 없는 파일 {sniffed[k]:,}개는 내용으로 형식을 알아내 올립니다"
                            f"(예: {ex.name} → {ex.upload_name})")
        photos = sum(1 for f in fl if f.kind == "photo")
        size = sum(f.size for f in fl)
        big = sum(1 for f in fl if f.size > resumable_min)
        folders.append(FolderPlan(k, title, fl, photos, len(fl) - photos, size, parts, warnings))
        tot_files += len(fl)
        tot_photos += photos
        tot_bytes += size
        tot_req += estimate_requests(len(fl), big, parts)

    return ScanResult(root=root, root_name=rname, include_root=include_root,
                      include_videos=include_videos, folders=folders, skipped=skipped,
                      skipped_ext=skipped_ext, examples=examples, errors=errors,
                      files=tot_files, photos=tot_photos, videos=tot_files - tot_photos,
                      bytes=tot_bytes, est_requests=tot_req, elapsed=time.monotonic() - t0)
