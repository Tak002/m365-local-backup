"""m365bak 명령줄.

  python -m m365bak run [--mode safe|hot] [--archive DIR] [--only teams|outlook] [--snapshot DIR] [--rerender]
  python -m m365bak status [--archive DIR]
  python -m m365bak verify-hot
"""
import argparse
import hashlib
import json
import pathlib
import sys
import time
import traceback

from . import archive as A
from . import jsonable as J
from . import media, outlook, render as R, snapshot as S, teams
from .idb import IdbSnapshot

DEFAULT_ARCHIVE = pathlib.Path.home() / "M365-Archive"


class Logger:
    def __init__(self, root: pathlib.Path | None):
        self.file = None
        if root:
            (root / "logs").mkdir(parents=True, exist_ok=True)
            self.file = open(root / "logs" / "m365bak.log", "a", encoding="utf-8")

    def __call__(self, msg: str):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        if self.file:
            self.file.write(line + "\n")
            self.file.flush()


def _load_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ---------------------------------------------------------------- Teams

TEAMS_AD = A.Adapter(teams.item_id, teams.item_version, teams.item_time, teams.prefer_old, teams.VOLATILE,
                     teams.item_order)
OUTLOOK_AD = A.Adapter(outlook.item_id, outlook.item_version, outlook.item_time, outlook.prefer_old,
                       outlook.VOLATILE, outlook.item_order)


def _teams_attachments(arch, folder, msgs, images, onedrive, st) -> int:
    """메시지의 파일(OneDrive 로컬 사본)과 이미지(HTTP 캐시)를 대화 폴더에 보관. 새로 저장한 개수."""
    saved = 0
    for mid, m in msgs.items():
        for f in teams.files(m):
            if arch.has_attachment("teams", folder, mid, f["id"]):
                continue
            src = onedrive.local_path(f["url"], f["name"])
            if src is None:
                st["files_not_local"] += 1
                continue
            try:  # OneDrive 온라인 전용 파일은 읽는 순간 OneDrive 앱이 내려받는다
                n = arch.store_attachment("teams", folder, mid, f["id"], f["name"], src=src)
            except OSError:
                st["files_unreadable"] += 1
                continue
            saved += n
            st["files_saved"] += n
        if images is None or m.get("messageType") not in ("RichText/Html", "RichText", "Text"):
            continue
        for oid in media.message_images(J.as_text(m.get("content"))):
            if arch.has_attachment("teams", folder, mid, "img:" + oid, need_full=True):
                continue
            got = images.get(oid)
            if not got:
                st["images_not_cached"] += 1
                continue
            data, mime, thumb = got
            name = f"image_{hashlib.sha1(oid.encode()).hexdigest()[:10]}{media.ext_for(mime, data)}"
            n = arch.store_attachment("teams", folder, mid, "img:" + oid, name, data=data, thumb=thumb)
            saved += n
            st["images_saved"] += n
    return saved


def _render_teams(arch, cid, folder, people, me):
    msgs = {TEAMS_AD.item_id(r): r for r in A.load_jsonl(folder / "raw.jsonl")}
    conv = _load_json(folder / "conversation.json")
    thread = teams.normalize(cid, conv, msgs, people, me, arch.annotations("teams", cid),
                             arch.attachment_paths("teams", folder))
    R.write_atomic(folder / "thread.json", R.dump_json(thread))
    R.write_atomic(folder / "transcript.md", teams.transcript(thread, arch.now))
    arch.update_conversation("teams", cid, thread["title"], thread["first_message"], thread["last_message"],
                             thread["message_count"])


def process_teams(arch, idb: IdbSnapshot, log, rerender=False, images=None, onedrive=None) -> dict:
    data = teams.extract(idb)
    people = arch_people = {**(_load_json(arch.root / "people.json") or {}), **data.people}
    arch.write_people(data.people)
    st = {**data.stats, "added": 0, "updated": 0, "kept_old": 0, "older_skipped": 0, "rendered": 0,
          "files_saved": 0, "files_not_local": 0, "files_unreadable": 0, "images_saved": 0, "images_not_cached": 0}
    onedrive = onedrive or media.OneDriveMap()
    me = data.me
    for cid, msgs in data.messages.items():
        conv = data.conversations.get(cid)
        first = R.iso(R.ms_to_dt(min(teams.item_time(m) for m in msgs.values())))
        folder = arch.folder("teams", cid, teams.title(conv, msgs, people, me), first)
        res = arch.merge("teams", cid, folder, msgs, TEAMS_AD)
        meta_changed = arch.write_meta("teams", cid, folder, conv, teams.meta_order)
        st["added"] += res.added
        st["updated"] += res.updated
        st["kept_old"] += res.kept_old
        st["older_skipped"] += res.older_skipped
        saved = _teams_attachments(arch, folder, msgs, images, onedrive, st)
        arch.commit()
        if res.changed or meta_changed or saved or rerender or not (folder / "transcript.md").exists():
            _render_teams(arch, cid, folder, arch_people, me)
            st["rendered"] += 1
    if rerender:  # 이번 스냅샷에 없는(캐시에서 빠진) 대화도 다시 그린다
        for cid, rel in arch.db.execute("SELECT conv_id, folder FROM conversations WHERE source='teams'").fetchall():
            if cid not in data.messages:
                _render_teams(arch, cid, arch.root / rel, arch_people, me)
                st["rendered"] += 1
    log(f"teams: 메시지 {st['messages']}건/대화 {st['conversations_with_messages']}개 읽음 → "
        f"신규 {st['added']}, 갱신 {st['updated']}, 삭제본 보존 {st['kept_old']}, "
        f"과거본 무시 {st['older_skipped']}, 전사본 갱신 {st['rendered']} | "
        f"파일 저장 {st['files_saved']}(로컬 사본 없음 {st['files_not_local']}, 읽기 실패 {st['files_unreadable']}), "
        f"이미지 저장 {st['images_saved']}(캐시에 없음 {st['images_not_cached']})")
    return st


# ---------------------------------------------------------------- Outlook

def _render_outlook(arch, cid, folder, data_folders, mailbox):
    msgs = {OUTLOOK_AD.item_id(r): r for r in A.load_jsonl(folder / "raw.jsonl")}
    meta = _load_json(folder / "conversation.json") or {}
    thread = outlook.normalize(cid, meta.get("conversation"), meta.get("nodes"), msgs, data_folders,
                               arch.attachment_paths("outlook", folder))
    R.write_atomic(folder / "thread.json", R.dump_json(thread))
    R.write_atomic(folder / "transcript.md", outlook.transcript(thread, arch.now, mailbox))
    arch.update_conversation("outlook", cid, thread["title"], thread["first_message"], thread["last_message"],
                             thread["message_count"])


def process_outlook(arch, idb: IdbSnapshot, att_root: pathlib.Path, log, rerender=False) -> dict:
    data = outlook.extract(idb)
    idx = outlook.AttachmentIndex(att_root, data.mailbox_guid)
    images = outlook.ImageStore(idb)
    folders_path = arch.root / "outlook" / "folders.json"
    known_folders = {**(_load_json(folders_path) or {}), **data.folders}
    R.write_atomic(folders_path, R.dump_json(known_folders))
    st = {**data.stats, "added": 0, "updated": 0, "kept_old": 0, "older_skipped": 0, "rendered": 0,
          "attachments_copied": 0, "images_from_store": 0, "attachments_not_cached": 0, "inline_not_cached": 0}
    for cid in sorted(set(data.messages) | set(data.conversations)):
        msgs = data.messages.get(cid, {})
        conv, node = data.conversations.get(cid), data.nodes.get(cid)
        times = [outlook.item_time(m) for m in msgs.values() if outlook.item_time(m)]
        times += [R.iso(R.iso_to_dt(it.get("DateTimeSent")))
                  for n in (node or {}).get("conversationNodes") or [] for it in n.get("Items") or []
                  if it.get("DateTimeSent")]
        if not times and conv:
            times = [R.iso(R.iso_to_dt(conv.get("LastDeliveryTime")))]
        title = (conv or {}).get("ConversationTopic") or next(
            (m.get("Subject") for m in msgs.values() if m.get("Subject")), "(제목 없음)")
        folder = arch.folder("outlook", cid, title, min(t for t in times if t) if any(times) else None)
        folder.mkdir(parents=True, exist_ok=True)
        res = arch.merge("outlook", cid, folder, msgs, OUTLOOK_AD) if msgs else A.MergeResult()
        prev = _load_json(folder / "conversation.json") or {}
        meta = {"conversation": conv or prev.get("conversation"), "nodes": node or prev.get("nodes")}
        meta_changed = arch.write_meta("outlook", cid, folder, meta, outlook.meta_order)
        copied = 0
        for iid, m in msgs.items():
            for a in m.get("Attachments") or []:
                aid = (a.get("AttachmentId") or {}).get("Id")
                if not aid or arch.has_attachment("outlook", folder, iid, aid, need_full=True):
                    continue
                name = a.get("Name") or ""
                src = idx.match(aid, name, a.get("Size"))
                if src:  # Olk\Attachments: 앱이 내려받아 둔 원본 파일
                    n = arch.store_attachment("outlook", folder, iid, aid, name, src=src)
                    copied += n
                    st["attachments_copied"] += n
                    continue
                img = images.best(aid)
                if img:  # owa-image-data: 본문 속 이미지·이미지 첨부(원본 또는 썸네일)
                    data_, mime, thumb = img
                    if not media.image_kind(data_) or not name.lower().endswith(media.ext_for(mime, data_)):
                        name = (name or "image") + media.ext_for(mime, data_)
                    n = arch.store_attachment("outlook", folder, iid, aid, name, data=data_, thumb=thumb)
                    copied += n
                    st["images_from_store"] += n
                    continue
                if not arch.has_attachment("outlook", folder, iid, aid):
                    st["inline_not_cached" if outlook.is_inline(a) else "attachments_not_cached"] += 1
        arch.commit()
        st["added"] += res.added
        st["updated"] += res.updated
        st["kept_old"] += res.kept_old
        st["older_skipped"] += res.older_skipped
        if res.changed or meta_changed or copied or rerender or not (folder / "transcript.md").exists():
            _render_outlook(arch, cid, folder, known_folders, data.mailbox)
            st["rendered"] += 1
    if rerender:
        seen = set(data.messages) | set(data.conversations)
        for cid, rel in arch.db.execute("SELECT conv_id, folder FROM conversations WHERE source='outlook'").fetchall():
            if cid not in seen:
                _render_outlook(arch, cid, arch.root / rel, known_folders, data.mailbox)
                st["rendered"] += 1
    log(f"outlook: 본문 {st['bodies']}건/대화 {len(set(data.messages) | set(data.conversations))}개 읽음 → "
        f"신규 {st['added']}, 갱신 {st['updated']}, 잘린본 대신 기존본 유지 {st['kept_old']}, "
        f"과거본 무시 {st['older_skipped']}, "
        f"첨부 복사 {st['attachments_copied']}, 이미지 저장소에서 {st['images_from_store']}"
        f"(캐시에 없음: 파일 {st['attachments_not_cached']}, 인라인 이미지 {st['inline_not_cached']}), "
        f"전사본 갱신 {st['rendered']}")
    return st


# ---------------------------------------------------------------- 명령

def _apps(only):
    return [S.APPS[only]] if only else [S.TEAMS, S.OUTLOOK]


def cmd_run(args) -> int:
    root = pathlib.Path(args.archive)
    log = Logger(root)
    started = A.now_iso()
    apps = _apps(args.only)
    mode = "existing" if args.snapshot else args.mode
    log(f"시작: mode={mode}, archive={root}")
    S.sweep_stale(log)
    try:
        snap = S.from_existing(pathlib.Path(args.snapshot)) if args.snapshot else S.take(args.mode, apps, log, args.force)
    except S.InCallError as e:
        log(f"건너뜀: {e}")
        return 3
    arch = A.Archive(root)
    stats, ok = {}, False
    try:
        if S.TEAMS in apps and snap.leveldb(S.TEAMS).exists():
            images = media.TeamsImageCache(snap.teams_http_cache) if snap.teams_http_cache.exists() else None
            try:
                with IdbSnapshot(snap.leveldb(S.TEAMS), snap.blob(S.TEAMS)) as idb:
                    stats["teams"] = process_teams(arch, idb, log, args.rerender, images, media.OneDriveMap())
                    stats["teams"]["undecodable"] = idb.undecodable
            finally:
                if images:
                    images.close()
            arch.commit()
        if S.OUTLOOK in apps and snap.leveldb(S.OUTLOOK).exists():
            with IdbSnapshot(snap.leveldb(S.OUTLOOK), snap.blob(S.OUTLOOK)) as idb:
                att_root = snap.olk_attachments if snap.olk_attachments.exists() else S.OLK_ATTACHMENTS
                stats["outlook"] = process_outlook(arch, idb, att_root, log, args.rerender)
                stats["outlook"]["undecodable"] = idb.undecodable
            arch.commit()
        arch.write_index()
        ok = True
    except Exception:
        log("오류:\n" + traceback.format_exc())
    finally:
        arch.record_run(started, mode, ok, stats)
        arch.close()
        if not args.snapshot and not args.keep_snapshot and not snap.cleanup():
            log(f"경고: 임시 스냅샷 삭제 실패 — 다음 실행 때 다시 시도: {snap.root}")
    log(f"{'완료' if ok else '실패'}: {root / 'index.md'}")
    return 0 if ok else 1


def cmd_status(args) -> int:
    root = pathlib.Path(args.archive)
    if not (root / "archive.db").exists():
        print(f"보관소 없음: {root}")
        return 1
    arch = A.Archive(root)
    for src, n, c in arch.db.execute("SELECT c.source, COUNT(DISTINCT c.conv_id), "
                                     "(SELECT COUNT(*) FROM items i WHERE i.source=c.source) "
                                     "FROM conversations c GROUP BY c.source"):
        print(f"{src}: 대화 {n}개, 메시지 {c}건")
    na = arch.db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM attachments").fetchone()
    print(f"첨부: {na[0]}개, {R.human_size(na[1])}")
    for started, finished, mode, ok, stats in arch.db.execute(
            "SELECT started, finished, mode, ok, stats FROM runs ORDER BY id DESC LIMIT 5"):
        print(f"- {started} {mode} {'OK' if ok else 'FAIL'}")
    arch.close()
    return 0


def _keys(snap):
    out = {}
    if snap.leveldb(S.TEAMS).exists():
        with IdbSnapshot(snap.leveldb(S.TEAMS), snap.blob(S.TEAMS)) as idb:
            d = teams.extract(idb)
            out["teams"] = ({(c, mid): (str(m.get("version")), teams.item_time(m))
                             for c, ms in d.messages.items() for mid, m in ms.items()}, idb.undecodable)
    if snap.leveldb(S.OUTLOOK).exists():
        with IdbSnapshot(snap.leveldb(S.OUTLOOK), snap.blob(S.OUTLOOK)) as idb:
            d = outlook.extract(idb)
            out["outlook"] = ({iid: (outlook.item_version(m), outlook.item_time(m))
                               for ms in d.messages.values() for iid, m in ms.items()}, idb.undecodable)
    return out


def cmd_verify_hot(args) -> int:
    """핫 카피 직후 안전 모드 스냅샷을 떠서 비교한다. 핫 카피에만 있는 항목이 없고,
    핫 카피 시작 전에 도착한 항목이 빠지지 않았으면 통과."""
    log = Logger(None)
    apps = [S.TEAMS, S.OUTLOOK]
    t0 = A.now_iso()
    hot = S.take("hot", apps, log)
    try:
        safe = S.take("safe", apps, log, args.force)
    except BaseException:
        hot.cleanup()
        raise
    try:
        kh, ks = _keys(hot), _keys(safe)
        ok = True
        for src in ("teams", "outlook"):
            if src not in kh or src not in ks:
                continue
            (h, hu), (s, su) = kh[src], ks[src]
            hot_only = [k for k in h if k not in s]
            missing = [k for k in s if k not in h]
            missing_old = [k for k in missing if _before(s[k][1], t0)]
            ver_diff = [k for k in h if k in s and h[k][0] != s[k][0]]
            log(f"{src}: hot {len(h)} / safe {len(s)} | hot에만 {len(hot_only)} | safe에만 {len(missing)}"
                f"(그중 핫카피 시작 전 도착 {len(missing_old)}) | 버전 차이 {len(ver_diff)} | 해독불가 hot {hu} / safe {su}")
            ok &= not hot_only and not missing_old
        log("핫 카피 검증: " + ("통과" if ok else "불일치 — 핫 카피를 쓰지 말 것"))
        return 0 if ok else 2
    finally:
        hot.cleanup()
        safe.cleanup()


def _before(t, t0_iso) -> bool:
    if isinstance(t, (int, float)):
        dt = R.ms_to_dt(t)
    else:
        dt = R.iso_to_dt(t)
    t0 = R.iso_to_dt(t0_iso)
    return bool(dt and t0 and dt < t0)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    p = argparse.ArgumentParser(prog="m365bak", description="Teams/Outlook 로컬 캐시 누적 백업")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="스냅샷 → 추출 → 보관소 병합")
    r.add_argument("--mode", choices=("safe", "hot"), default="safe")
    r.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    r.add_argument("--only", choices=("teams", "outlook"))
    r.add_argument("--snapshot", help="이미 떠 둔 스냅샷 폴더 사용(앱 제어 안 함, 삭제 안 함)")
    r.add_argument("--keep-snapshot", action="store_true")
    r.add_argument("--force", action="store_true", help="통화 중 감지를 무시하고 안전 모드 실행")
    r.add_argument("--rerender", action="store_true", help="모든 대화의 thread.json/transcript.md 재생성")
    s = sub.add_parser("status")
    s.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    v = sub.add_parser("verify-hot", help="핫 카피와 안전 모드 스냅샷 비교")
    v.add_argument("--force", action="store_true")
    args = p.parse_args(argv)
    return {"run": cmd_run, "status": cmd_status, "verify-hot": cmd_verify_hot}[args.cmd](args)
