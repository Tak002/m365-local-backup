"""첨부 보강 소스.

- TeamsImageCache: Teams WebView2 HTTP 캐시에서 메시지 속 이미지(AMS objects/<id>/views/<view>)를 꺼낸다.
  색인(index·data_*)은 스냅샷 사본을 읽고, 본문이 든 f_* 파일은 필요한 것만 공유 모드로 복사한 뒤 읽는다.
- OneDriveMap: OneDrive 동기화 매핑(UrlNamespace → MountPoint)으로 Teams 파일 링크를 로컬 경로로 바꾼다.
  매핑은 HKCU\\Software\\SyncEngines\\Providers\\OneDrive에서 읽는다.
"""
import contextlib
import gzip
import logging
import pathlib
import re
import urllib.parse
import winreg
import zlib

from . import snapshot as S

AMS_RE = re.compile(r"objects/([^/?#\s\"'<>]+)/views/([A-Za-z0-9_]+)")
# 원본 화질 우선. 썸네일 view는 원본이 없을 때만 쓴다.
FULL_VIEWS = ("imgpsh_fullsize_anim", "imgpsh_fullsize", "imgo", "imgpsh_mobile_save_anim", "imgpsh_mobile_save")
THUMB_VIEWS = ("imgt1_anim", "imgt1", "thumbnail_small", "thumbnail")

_MAGIC = ((b"\x89PNG", ".png", "image/png"), (b"\xff\xd8\xff", ".jpg", "image/jpeg"), (b"GIF8", ".gif", "image/gif"),
          (b"RIFF", ".webp", "image/webp"), (b"BM", ".bmp", "image/bmp"))
_EXT_BY_MIME = {m: e for _, e, m in _MAGIC}


def image_kind(data: bytes) -> tuple[str, str] | None:
    """(확장자, mime). 이미지가 아니면 None."""
    for magic, ext, mime in _MAGIC:
        if data.startswith(magic):
            return ext, mime
    return None


def ext_for(mime: str | None, data: bytes) -> str:
    k = image_kind(data)
    if k:
        return k[0]
    return _EXT_BY_MIME.get((mime or "").split(";")[0].strip(), "")


class TeamsImageCache:
    def __init__(self, snap_cache_dir: pathlib.Path, live_cache_dir: pathlib.Path = S.TEAMS_HTTP_CACHE):
        self.dir, self.live = snap_cache_dir, live_cache_dir
        self.objects: dict[str, dict[str, object]] = {}   # object_id → {view: CacheKey}
        self.cache = None
        logging.getLogger("ccl_chromium_reader").setLevel(logging.ERROR)
        if not (snap_cache_dir / "index").exists():
            return
        from ccl_chromium_reader import ccl_chromium_cache as cc
        try:
            self.cache = cc.ChromiumBlockFileCache(snap_cache_dir)
        except Exception:  # 핫 카피 중 색인이 깨졌으면 이번 실행은 건너뛴다
            self.cache = None
            return
        for k in self.cache.cache_keys():
            m = AMS_RE.search(getattr(k, "url", "") or str(k))
            if m:
                self.objects.setdefault(m.group(1), {})[m.group(2)] = k

    def _ensure_external(self, key):
        for getter in (self.cache.get_location_for_metadata, self.cache.get_location_for_cachefile):
            try:
                locs = getter(key)
            except Exception:
                continue
            for loc in locs:
                name = getattr(loc, "file_name", "")
                if name.startswith("f_") and not (self.dir / name).exists():
                    S.copy_shared(self.live / name, self.dir / name)

    def _read(self, key) -> tuple[bytes, str | None] | None:
        self._ensure_external(key)
        try:
            data = (self.cache.get_cachefile(key) or [None])[0]
            meta = (self.cache.get_metadata(key) or [None])[0]
        except Exception:
            return None
        if not data:
            return None
        ct = (meta.get_attribute("content-type") or [None])[0] if meta else None
        enc = ((meta.get_attribute("content-encoding") or [""])[0] if meta else "").lower()
        with contextlib.suppress(Exception):
            if enc == "gzip":
                data = gzip.decompress(data)
            elif enc == "deflate":
                data = zlib.decompress(data)
            elif enc == "br":
                import brotli
                data = brotli.decompress(data)
        return (data, ct) if image_kind(data) else None

    def get(self, object_id: str) -> tuple[bytes, str, bool] | None:
        """(바이트, mime, 썸네일 여부). 원본 view 우선."""
        if self.cache is None:
            return None
        views = self.objects.get(object_id) or {}
        order = [v for v in FULL_VIEWS if v in views] + [v for v in views if v not in FULL_VIEWS + THUMB_VIEWS] \
            + [v for v in THUMB_VIEWS if v in views]
        for v in order:
            got = self._read(views[v])
            if got:
                data, ct = got
                return data, ct or image_kind(data)[1], v in THUMB_VIEWS
        return None

    def close(self):
        if self.cache is not None:
            self.cache.close()


def message_images(content: str) -> list[str]:
    """메시지 HTML의 <img>가 가리키는 AMS object id(순서 유지, 중복 제거)."""
    ids = []
    for src in re.findall(r"<img\b[^>]*\bsrc=\"([^\"]+)\"", content or "", re.I):
        m = AMS_RE.search(src)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


class OneDriveMap:
    """동기화된 OneDrive/SharePoint 라이브러리의 URL 접두어 → 로컬 폴더."""

    def __init__(self):
        self.prefixes: list[tuple[str, pathlib.Path]] = []
        path = r"Software\SyncEngines\Providers\OneDrive"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    with winreg.OpenKey(k, sub) as sk:
                        try:
                            ns = winreg.QueryValueEx(sk, "UrlNamespace")[0]
                            mp = winreg.QueryValueEx(sk, "MountPoint")[0]
                        except OSError:
                            continue
                    if ns and mp:
                        self.prefixes.append((ns.rstrip("/") + "/", pathlib.Path(mp)))
        except OSError:
            pass
        self.prefixes.sort(key=lambda p: -len(p[0]))

    def local_path(self, url: str | None, file_name: str | None = None) -> pathlib.Path | None:
        if not url:
            return None
        url = url.split("?", 1)[0]
        for ns, mount in self.prefixes:
            if url.lower().startswith(ns.lower()):
                p = mount / urllib.parse.unquote(url[len(ns):]).replace("/", "\\")
                if p.exists():
                    return p
                # 이름이 바뀌지 않은 채 다른 Teams 채팅 파일 폴더에 있을 수 있다
                for d in ("Microsoft Teams 채팅 파일", "Microsoft Teams Chat Files"):
                    q = mount / d / (file_name or p.name)
                    if q.exists():
                        return q
        return None
