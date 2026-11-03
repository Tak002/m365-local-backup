"""누적 보관소(M365-Archive): 상태 DB(archive.db), 대화 폴더, 증분 병합, 첨부 복사, 색인.

원칙: 추가·갱신만 한다. 앱 캐시에서 사라진 항목은 지우지 않는다(append-only union).
"""
import dataclasses
import datetime
import hashlib
import json
import pathlib
import shutil
import sqlite3

from . import jsonable as J
from . import render as R

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations(
  source TEXT, conv_id TEXT, folder TEXT, title TEXT, first_time TEXT, last_time TEXT,
  count INTEGER, meta_sha TEXT, updated_at TEXT, PRIMARY KEY(source, conv_id));
CREATE UNIQUE INDEX IF NOT EXISTS conversations_folder ON conversations(folder);
CREATE TABLE IF NOT EXISTS items(
  source TEXT, conv_id TEXT, item_id TEXT, version TEXT, sha TEXT,
  first_seen TEXT, last_seen TEXT, deleted_at TEXT, PRIMARY KEY(source, conv_id, item_id));
CREATE TABLE IF NOT EXISTS attachments(
  source TEXT, item_id TEXT, att_id TEXT, file_name TEXT, size INTEGER, sha256 TEXT, rel_path TEXT,
  copied_at TEXT, folder TEXT, PRIMARY KEY(source, item_id, att_id));
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, mode TEXT, ok INTEGER, stats TEXT);
"""


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclasses.dataclass
class Adapter:
    """소스별 레코드 규칙."""
    item_id: callable
    item_version: callable
    item_time: callable
    prefer_old: callable
    volatile: tuple
    order: callable        # 레코드의 신구 비교 키(클수록 최신). 더 오래된 사본은 반영하지 않는다.


@dataclasses.dataclass
class MergeResult:
    added: int = 0
    updated: int = 0
    kept_old: int = 0
    unchanged: int = 0
    older_skipped: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.kept_old)


class Archive:
    def __init__(self, root: pathlib.Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "archive.db")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(conversations)")}
        if "meta_order" not in cols:
            self.db.execute("ALTER TABLE conversations ADD COLUMN meta_order TEXT")
        if "thumb" not in {r[1] for r in self.db.execute("PRAGMA table_info(attachments)")}:
            self.db.execute("ALTER TABLE attachments ADD COLUMN thumb INTEGER DEFAULT 0")
        self.now = now_iso()

    # ------------------------------------------------------------ 대화 폴더
    def folder(self, source: str, conv_id: str, title: str, first_time: str | None) -> pathlib.Path:
        row = self.db.execute("SELECT folder FROM conversations WHERE source=? AND conv_id=?",
                              (source, conv_id)).fetchone()
        if row:
            return self.root / row[0]
        date = (first_time or self.now)[:10]
        rel = f"{source}/{date}_{R.safe_name(title)}_{hashlib.sha1(conv_id.encode()).hexdigest()[:8]}"
        self.db.execute("INSERT INTO conversations(source, conv_id, folder, title, first_time, updated_at) "
                        "VALUES(?,?,?,?,?,?)", (source, conv_id, rel, title, first_time, self.now))
        return self.root / rel

    def update_conversation(self, source, conv_id, title, first_time, last_time, count):
        self.db.execute("UPDATE conversations SET title=?, first_time=?, last_time=?, count=?, updated_at=? "
                        "WHERE source=? AND conv_id=?",
                        (title, first_time, last_time, count, self.now, source, conv_id))

    def write_meta(self, source, conv_id, folder: pathlib.Path, meta, order_fn) -> bool:
        """order_fn(meta) → 신구 비교 문자열. 이미 더 새로운 메타가 있으면 덮어쓰지 않는다."""
        if meta is None:
            return False
        sha = J.canonical_hash(meta)
        order = order_fn(meta)
        row = self.db.execute("SELECT meta_sha, meta_order FROM conversations WHERE source=? AND conv_id=?",
                              (source, conv_id)).fetchone()
        path = folder / "conversation.json"
        if row and path.exists():
            if row[0] == sha:
                return False
            stored = row[1] if row[1] is not None else order_fn(json.loads(path.read_text(encoding="utf-8")))
            if stored > order:
                return False
        R.write_atomic(folder / "conversation.json", R.dump_json(meta))
        self.db.execute("UPDATE conversations SET meta_sha=?, meta_order=? WHERE source=? AND conv_id=?",
                        (sha, order, source, conv_id))
        return True

    # ------------------------------------------------------------ 메시지 병합
    def merge(self, source, conv_id, folder: pathlib.Path, incoming: dict, ad: Adapter) -> MergeResult:
        res = MergeResult()
        rows = {r[0]: r[1:] for r in self.db.execute(
            "SELECT item_id, version, sha FROM items WHERE source=? AND conv_id=?", (source, conv_id))}
        pending = []
        for iid, raw in incoming.items():
            sha = J.canonical_hash(raw, ad.volatile)
            row = rows.get(iid)
            if row and row[1] == sha:
                res.unchanged += 1
                continue
            pending.append((iid, raw, sha, row))
        raw_path = folder / "raw.jsonl"
        if not pending and raw_path.exists():
            self.db.execute("UPDATE items SET last_seen=? WHERE source=? AND conv_id=?", (self.now, source, conv_id))
            return res

        existing = {ad.item_id(r): r for r in load_jsonl(raw_path)}
        history = []
        for iid, raw, sha, row in pending:
            old = existing.get(iid)
            deleted_at = None
            if old is not None and ad.order(raw) < ad.order(old):
                res.older_skipped += 1  # 과거 스냅샷을 다시 넣는 경우 등
                continue
            if old is not None and ad.prefer_old(old, raw):
                history.append({"archived_at": self.now, "reason": "superseded_by_worse_copy", "record": raw})
                deleted_at = self.now if source == "teams" else None
                res.kept_old += 1
            else:
                if old is not None and ad.item_version(old) != ad.item_version(raw):
                    history.append({"archived_at": self.now, "reason": "edited", "record": old})
                existing[iid] = raw
                if row is None and old is None:
                    res.added += 1
                else:
                    res.updated += 1
            self.db.execute(
                "INSERT INTO items(source, conv_id, item_id, version, sha, first_seen, last_seen, deleted_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source, conv_id, item_id) DO UPDATE SET "
                "version=excluded.version, sha=excluded.sha, last_seen=excluded.last_seen, "
                "deleted_at=COALESCE(items.deleted_at, excluded.deleted_at)",
                (source, conv_id, iid, ad.item_version(raw), sha, self.now, self.now, deleted_at))
        rows_sorted = sorted(existing.values(), key=ad.item_time)
        R.write_atomic(raw_path, R.dump_jsonl(rows_sorted))
        if history:
            with open(folder / "history.jsonl", "a", encoding="utf-8", newline="\n") as f:
                f.write(R.dump_jsonl(history))
        self.db.execute("UPDATE items SET last_seen=? WHERE source=? AND conv_id=?", (self.now, source, conv_id))
        return res

    def annotations(self, source, conv_id) -> dict:
        return {r[0]: {"deleted_at": r[1]} for r in self.db.execute(
            "SELECT item_id, deleted_at FROM items WHERE source=? AND conv_id=? AND deleted_at IS NOT NULL",
            (source, conv_id))}

    # ------------------------------------------------------------ 첨부
    def attachment_paths(self, source, folder: pathlib.Path) -> dict:
        """(item_id, att_id) → {"path": 대화 폴더 기준 상대경로, "thumbnail": bool}"""
        conv_rel = folder.relative_to(self.root).as_posix()
        return {(iid, att): {"path": rel, "thumbnail": bool(th)} for iid, att, rel, th in self.db.execute(
            "SELECT item_id, att_id, rel_path, COALESCE(thumb, 0) FROM attachments WHERE source=? AND folder=?",
            (source, conv_rel))}

    def has_attachment(self, source, folder: pathlib.Path, item_id, att_id, need_full=False) -> bool:
        row = self.db.execute("SELECT rel_path, COALESCE(thumb, 0) FROM attachments "
                              "WHERE source=? AND item_id=? AND att_id=?", (source, item_id, att_id)).fetchone()
        return bool(row and (folder / row[0]).exists() and not (need_full and row[1]))

    def store_attachment(self, source, folder: pathlib.Path, item_id: str, att_id: str, name: str, *,
                         src: pathlib.Path | None = None, data: bytes | None = None, thumb: bool = False) -> bool:
        """첨부 1건 보관(src 파일 복사 또는 data 바이트 저장). 이미 있으면 건너뛰고,
        썸네일만 있던 것은 원본이 오면 교체한다. 새 파일을 썼으면 True."""
        row = self.db.execute("SELECT rel_path, COALESCE(thumb, 0) FROM attachments "
                              "WHERE source=? AND item_id=? AND att_id=?", (source, item_id, att_id)).fetchone()
        if row and (folder / row[0]).exists() and (thumb or not row[1]):
            return False
        conv_rel = folder.relative_to(self.root).as_posix()
        sha = hashlib.sha256(data).hexdigest() if data is not None else _sha256_file(src)
        same = self.db.execute("SELECT rel_path, size FROM attachments WHERE folder=? AND sha256=?",
                               (conv_rel, sha)).fetchone()
        if same and (folder / same[0]).exists():  # 같은 메일의 다른 폴더 사본 등: 파일 재사용
            self.db.execute("INSERT OR REPLACE INTO attachments VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (source, item_id, att_id, name, same[1], sha, same[0], self.now, conv_rel, int(thumb)))
            return False
        sub = hashlib.sha1(item_id.encode()).hexdigest()[:8]
        dst = folder / "attachments" / sub / R.safe_file_name(name or (src.name if src else "file"))
        n = 1
        while dst.exists():
            dst = dst.with_name(f"{dst.stem.rsplit(' (', 1)[0]} ({n}){dst.suffix}")
            n += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        if data is not None:
            dst.write_bytes(data)
        else:
            shutil.copyfile(src, dst)
        rel = dst.relative_to(folder).as_posix()
        self.db.execute("INSERT OR REPLACE INTO attachments VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (source, item_id, att_id, name, dst.stat().st_size, sha, rel, self.now, conv_rel, int(thumb)))
        if row and row[0] != rel and not self.db.execute(
                "SELECT 1 FROM attachments WHERE folder=? AND rel_path=?", (conv_rel, row[0])).fetchone():
            (folder / row[0]).unlink(missing_ok=True)  # 원본으로 교체된 썸네일 파일
        return True

    # ------------------------------------------------------------ 색인·기록
    def write_people(self, people: dict):
        path = self.root / "people.json"
        merged = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        merged.update(people)
        R.write_atomic(path, R.dump_json(dict(sorted(merged.items()))))

    def write_index(self):
        rows = self.db.execute("SELECT source, title, folder, first_time, last_time, count, conv_id "
                               "FROM conversations WHERE count IS NOT NULL "
                               "ORDER BY last_time DESC").fetchall()
        items = [{"source": s, "title": t, "path": f, "first_message": a, "last_message": b, "message_count": c,
                  "conversation_id": cid} for s, t, f, a, b, c, cid in rows]
        R.write_atomic(self.root / "index.json", R.dump_json(items))
        lines = ["# M365 Archive", "", f"갱신: {self.now} · 대화 {len(items)}개", "",
                 "| 마지막 | 출처 | 대화 | 건수 |", "|---|---|---|---|"]
        for it in items:
            title = it["title"].replace("|", "\\|")
            link = f"{it['path']}/transcript.md".replace(" ", "%20")
            lines.append(f"| {(it['last_message'] or '')[:16].replace('T', ' ')} | {it['source']} | [{title}]({link}) | {it['message_count']} |")
        R.write_atomic(self.root / "index.md", "\n".join(lines) + "\n")

    def record_run(self, started, mode, ok, stats):
        self.db.execute("INSERT INTO runs(started, finished, mode, ok, stats) VALUES(?,?,?,?,?)",
                        (started, now_iso(), mode, int(ok), json.dumps(stats, ensure_ascii=False)))

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()


def _sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_jsonl(path: pathlib.Path) -> list:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
