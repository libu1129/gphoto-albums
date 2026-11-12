"""사용자별 자료 폴더 위치."""
import os
from pathlib import Path

APP_DIR_NAME = "GPhotoAlbums"


def data_dir() -> Path:
    """%LOCALAPPDATA%\\GPhotoAlbums 를 돌려준다. 시험 때는 GPHOTO_ALBUMS_DATA 로 바꾼다."""
    override = os.environ.get("GPHOTO_ALBUMS_DATA")
    if override:
        p = Path(override)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        p = Path(base) / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p
