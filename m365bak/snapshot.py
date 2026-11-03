"""앱 데이터 위치, 앱 종료·재실행, IndexedDB 스냅샷 복사.

- safe: 통화 중이 아니면 Teams·Outlook을 닫고 복사한 뒤, 원래 켜져 있던 앱만 다시 실행한다.
- hot : 앱을 켠 채 공유 모드(READ|WRITE|DELETE)로 열어 복사한다. 앱의 쓰기·삭제를 막지 않는다.
        복사 도중 CURRENT나 .ldb 집합이 바뀌면(compaction) 다시 복사한다.
라이브 파일을 DB 엔진으로 열지 않는다. 읽기 전용으로 복사만 한다.
"""
import ctypes
import ctypes.wintypes as wt
import dataclasses
import msvcrt
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import winreg

LOCAL = pathlib.Path(os.environ["LOCALAPPDATA"])


@dataclasses.dataclass(frozen=True)
class App:
    name: str
    exe: str
    aumid: str
    idb_dir: pathlib.Path
    origin: str            # IndexedDB 폴더 접두어
    profile_marker: str    # WebView2 자식 프로세스 명령줄에서 찾을 문자열

    @property
    def leveldb(self) -> pathlib.Path:
        return self.idb_dir / f"{self.origin}.leveldb"

    @property
    def blob(self) -> pathlib.Path:
        return self.idb_dir / f"{self.origin}.blob"


TEAMS = App(
    "teams", "ms-teams.exe", "MSTeams_8wekyb3d8bbwe!MSTeams",
    LOCAL / "Packages/MSTeams_8wekyb3d8bbwe/LocalCache/Microsoft/MSTeams/EBWebView/WV2Profile_tfw/IndexedDB",
    "https_teams.microsoft.com_0.indexeddb", "MSTeams",
)
OUTLOOK = App(
    "outlook", "olk.exe", "Microsoft.OutlookForWindows_8wekyb3d8bbwe!Microsoft.OutlookforWindows",
    LOCAL / "Microsoft/Olk/EBWebView/Default/IndexedDB",
    "https_outlook.office.com_0.indexeddb", "Olk",
)
OLK_ATTACHMENTS = LOCAL / "Microsoft/Olk/Attachments"
# Teams WebView2 HTTP 캐시(블록 파일 형식). 메시지에 붙여 넣은 이미지(AMS)가 열람된 적이 있으면 여기 남는다.
TEAMS_HTTP_CACHE = TEAMS.idb_dir.parent / "Cache" / "Cache_Data"
TEAMS_HTTP_CACHE_INDEX = ("index", "data_0", "data_1", "data_2", "data_3")
APPS = {"teams": TEAMS, "outlook": OUTLOOK}


class SnapshotError(RuntimeError):
    pass


class InCallError(SnapshotError):
    pass


@dataclasses.dataclass
class Snapshot:
    root: pathlib.Path
    mode: str
    apps: tuple

    def leveldb(self, app: App) -> pathlib.Path:
        return self.root / app.name / f"{app.origin}.leveldb"

    def blob(self, app: App) -> pathlib.Path:
        return self.root / app.name / f"{app.origin}.blob"

    @property
    def olk_attachments(self) -> pathlib.Path:
        return self.root / "outlook" / "Attachments"

    @property
    def teams_http_cache(self) -> pathlib.Path:
        """index·data_* 사본. 본문이 든 f_* 파일은 필요한 것만 처리 시점에 복사한다(media.TeamsImageCache)."""
        return self.root / "teams" / "Cache_Data"

    def cleanup(self) -> bool:
        """임시 스냅샷 삭제. 원본 캐시 사본이므로 남기지 않는다. 성공 여부를 돌려준다."""
        for _ in range(5):
            shutil.rmtree(self.root, ignore_errors=True)
            if not self.root.exists():
                return True
            time.sleep(1)
        return False


def sweep_stale(log, older_than_s: int = 3600):
    """비정상 종료로 남은 이전 임시 스냅샷(m365bak-*)을 지운다."""
    for d in pathlib.Path(tempfile.gettempdir()).glob("m365bak-*"):
        if d.is_dir() and time.time() - d.stat().st_mtime > older_than_s:
            shutil.rmtree(d, ignore_errors=True)
            log(f"남은 임시 스냅샷 삭제: {d.name}" + ("" if not d.exists() else " (실패)"))


# ---------------------------------------------------------------- 공유 모드 읽기

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wt.HANDLE
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
_GENERIC_READ = 0x80000000
_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_INVALID_HANDLE = wt.HANDLE(-1).value


def open_shared(path: pathlib.Path):
    """다른 프로세스의 읽기·쓰기·삭제를 막지 않는 읽기 전용 열기."""
    h = _k32.CreateFileW(str(path), _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING, 0x80, None)
    if h == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    fd = msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY)
    return os.fdopen(fd, "rb")


def copy_shared(src: pathlib.Path, dst: pathlib.Path) -> bool:
    """복사 성공 시 True, 원본이 그 사이 사라졌으면 False."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open_shared(src) as fi, open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1024 * 1024)
    except OSError as e:
        if getattr(e, "winerror", None) in (2, 3):
            return False
        raise
    return True


def _files(root: pathlib.Path) -> dict:
    if not root.exists():
        return {}
    return {p.relative_to(root): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def _copy_tree(src: pathlib.Path, dst: pathlib.Path, skip=("LOCK",)) -> int:
    n = 0
    for rel in _files(src):
        if rel.name in skip:
            continue
        n += copy_shared(src / rel, dst / rel)
    return n


def _read_current(leveldb: pathlib.Path) -> str:
    try:
        with open_shared(leveldb / "CURRENT") as f:
            return f.read().decode("ascii", "replace").strip()
    except OSError:
        return ""


def copy_leveldb_consistent(src: pathlib.Path, dst: pathlib.Path, attempts: int = 6):
    """LevelDB 디렉터리 복사. .ldb는 불변이고 .log/MANIFEST는 덧붙이기만 하므로,
    복사 전후로 CURRENT와 .ldb 집합이 같으면 일관된 시점의 복사본으로 본다."""
    for i in range(attempts):
        shutil.rmtree(dst, ignore_errors=True)
        cur0 = _read_current(src)
        ldb0 = {p.name for p in src.glob("*.ldb")}
        _copy_tree(src, dst)
        cur1 = _read_current(src)
        ldb1 = {p.name for p in src.glob("*.ldb")}
        copied = {p.name for p in dst.glob("*.ldb")}
        if cur0 and cur0 == cur1 and ldb0 == ldb1 == copied:
            return i + 1
        time.sleep(1.5)
    raise SnapshotError(f"LevelDB가 복사 중 계속 바뀜(compaction): {src}")


# ---------------------------------------------------------------- 앱 제어

def _running(exe: str) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH", "/FO", "CSV"],
                         capture_output=True, text=True, encoding="mbcs", errors="replace").stdout
    return exe.lower() in out.lower()


def _webview_children(marker: str) -> int:
    ps = ("@(Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | "
          f"Where-Object {{ $_.CommandLine -match '{marker}' }}).Count")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    try:
        return int(out.strip() or 0)
    except ValueError:
        return 0


def teams_in_call() -> bool:
    """Windows가 마이크·카메라 사용 표시에 쓰는 기록으로 Teams 통화·회의 여부를 추정한다(사용 중이면 Stop=0)."""
    for cap in ("microphone", "webcam"):
        path = rf"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\{cap}\MSTeams_8wekyb3d8bbwe"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                start = winreg.QueryValueEx(k, "LastUsedTimeStart")[0]
                stop = winreg.QueryValueEx(k, "LastUsedTimeStop")[0]
        except OSError:
            continue
        if start and (not stop or stop < start):
            return True
    return False


def close_apps(apps, log) -> list:
    was_running = [a for a in apps if _running(a.exe)]
    for a in was_running:
        subprocess.run(["taskkill", "/IM", a.exe], capture_output=True)
    deadline = time.time() + 8
    while time.time() < deadline and any(_running(a.exe) for a in was_running):
        time.sleep(0.5)
    for a in was_running:
        if _running(a.exe):  # Teams는 창을 닫아도 트레이에 남는다
            subprocess.run(["taskkill", "/F", "/IM", a.exe], capture_output=True)
    deadline = time.time() + 25
    while time.time() < deadline:
        if not any(_running(a.exe) for a in apps) and not any(_webview_children(a.profile_marker) for a in apps):
            break
        time.sleep(1)
    else:
        raise SnapshotError("앱 또는 WebView2 프로세스가 종료되지 않음")
    log(f"앱 종료: {', '.join(a.name for a in was_running) or '(실행 중인 앱 없음)'}")
    return was_running


def relaunch(apps, log):
    for a in apps:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{a.aumid}"])
    if apps:
        log(f"앱 재실행: {', '.join(a.name for a in apps)}")


# ---------------------------------------------------------------- 스냅샷

def take(mode: str, apps, log, force: bool = False, base_dir: pathlib.Path | None = None) -> Snapshot:
    root = pathlib.Path(tempfile.mkdtemp(prefix="m365bak-", dir=base_dir))
    snap = Snapshot(root, mode, tuple(apps))
    try:
        if mode == "safe":
            if TEAMS in apps and _running(TEAMS.exe) and teams_in_call() and not force:
                raise InCallError("Teams 통화·회의 중으로 보여 앱을 닫지 않고 건너뜀(--force로 무시 가능)")
            was_running = close_apps(apps, log)
            try:
                _copy_all(snap, apps, log, consistent=False)
            finally:
                relaunch(was_running, log)
        elif mode == "hot":
            _copy_all(snap, apps, log, consistent=True)
        else:
            raise ValueError(mode)
    except BaseException:
        snap.cleanup()
        raise
    return snap


def _copy_all(snap: Snapshot, apps, log, consistent: bool):
    for a in apps:
        if not a.leveldb.exists():
            log(f"{a.name}: IndexedDB 없음 → 건너뜀 ({a.leveldb})")
            continue
        if consistent:
            tries = copy_leveldb_consistent(a.leveldb, snap.leveldb(a))
        else:
            _copy_tree(a.leveldb, snap.leveldb(a))
            tries = 1
        nb = _copy_tree(a.blob, snap.blob(a))
        log(f"{a.name}: leveldb 복사(시도 {tries}회), blob {nb}개")
        if a is OUTLOOK and OLK_ATTACHMENTS.exists():
            na = _copy_tree(OLK_ATTACHMENTS, snap.olk_attachments, skip=())
            log(f"outlook: 첨부 캐시 {na}개 복사")
        if a is TEAMS and TEAMS_HTTP_CACHE.exists():
            nc = sum(copy_shared(TEAMS_HTTP_CACHE / n, snap.teams_http_cache / n) for n in TEAMS_HTTP_CACHE_INDEX)
            log(f"teams: HTTP 캐시 색인 {nc}개 복사")


def from_existing(path: pathlib.Path) -> Snapshot:
    """이미 떠 둔 스냅샷 폴더(teams/, outlook/ 하위)를 그대로 쓴다. 정리(삭제)하지 않는다."""
    return Snapshot(path, "existing", (TEAMS, OUTLOOK))
