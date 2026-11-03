"""IndexedDB(LevelDB) 스냅샷 읽기.

주의: ccl_chromium_reader의 iterate_records(live_only=...)는 인자를 무시하고 LevelDB에 남은 모든 버전을
돌려준다(삭제 마커는 value=None). 그래서 여기서 키별로 ldb_seq_no를 비교해 최신본을 고른다.
"""
import dataclasses
import pathlib
import typing

from ccl_chromium_reader import ccl_chromium_indexeddb as ccl_idb


@dataclasses.dataclass
class Version:
    raw_key: bytes         # 인코딩된 IndexedDB 키(비교용)
    key: typing.Any        # 디코딩된 IndexedDB 키
    value: typing.Any      # 디코딩된 값(삭제 마커면 None)
    seq: int               # LevelDB sequence number(클수록 최신)
    live: bool


class IdbSnapshot:
    def __init__(self, leveldb_dir: pathlib.Path, blob_dir: pathlib.Path):
        self._w = ccl_idb.WrappedIndexDB(str(leveldb_dir), str(blob_dir) if blob_dir.exists() else None)
        self.undecodable = 0

    def close(self):
        """LevelDB 파일 핸들을 닫는다. 닫지 않으면 Windows에서 임시 스냅샷 폴더가 지워지지 않는다."""
        self._w._raw_db._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def database_names(self) -> list[str]:
        return [d.name for d in self._w.database_ids]

    def databases(self, pred: typing.Callable[[str], bool]):
        for dbid in self._w.database_ids:
            if pred(dbid.name):
                yield dbid.name, self._w[dbid.dbid_no]

    def versions(self, db, store: str) -> typing.Iterator[Version]:
        """store의 모든 레코드 버전. 외부 blob이 없거나 역직렬화가 안 되는 레코드는 건너뛴다."""
        if store not in db:
            return

        def bad(_key, _data):
            self.undecodable += 1

        for rec in db[store].iterate_records(bad_deserializer_data_handler=bad):
            yield Version(rec.key.raw_key, rec.key.value, rec.value, rec.ldb_seq_no, rec.is_live)

    def latest_records(self, db, store: str) -> dict:
        """키별 최신(값이 있는) ccl 레코드. 외부 Blob(이미지 등)을 읽어야 할 때 쓴다."""
        best: dict = {}
        if store not in db:
            return best

        def bad(_key, _data):
            self.undecodable += 1

        for rec in db[store].iterate_records(bad_deserializer_data_handler=bad):
            if rec.value is None:
                continue
            cur = best.get(rec.key.raw_key)
            if cur is None or rec.ldb_seq_no > cur.ldb_seq_no:
                best[rec.key.raw_key] = rec
        return best

    def latest(self, db, store: str, include_superseded: bool = True) -> dict:
        """키별 최신 값. include_superseded=True면 최신이 삭제 마커여도 그 전 버전 값을 쓴다
        (앱이 캐시에서 비운 항목을 compaction 전에 복구). 반환: raw_key → (key, value, seq, recovered)."""
        best: dict = {}
        newest_seq: dict = {}
        for v in self.versions(db, store):
            rk = v.raw_key
            if v.seq > newest_seq.get(rk, -1):
                newest_seq[rk] = v.seq
            if v.value is None:
                continue
            cur = best.get(rk)
            if cur is None or v.seq > cur[2]:
                best[rk] = (v.key, v.value, v.seq)
        out = {}
        for rk, (key, value, seq) in best.items():
            recovered = seq < newest_seq[rk]  # 더 새로운 삭제 마커가 있었다
            if recovered and not include_superseded:
                continue
            out[rk] = (key, value, seq, recovered)
        return out
