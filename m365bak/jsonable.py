"""ccl_chromium_reader가 돌려주는 V8 역직렬화 값을 JSON으로 저장 가능한 값으로 바꾼다."""
import base64
import datetime
import enum
import hashlib
import json
import math

from ccl_chromium_reader.serialization_formats import ccl_v8_value_deserializer as v8


def _is_undefined(v) -> bool:
    return isinstance(v, v8._Undefined)


def to_jsonable(v):
    """JS의 JSON.stringify와 비슷하게: undefined인 키는 빼고, set은 list로, 정수 float는 int로 바꾼다."""
    if v is None or isinstance(v, (bool, str, int)):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return int(v) if v.is_integer() and abs(v) < 2**63 else v
    if isinstance(v, bytes):
        # V8 one-byte 문자열(Latin-1)이 bytes로 온다. latin-1은 무손실 왕복이 가능하다.
        return v.decode("latin-1")
    if isinstance(v, dict):
        return {str(k): to_jsonable(x) for k, x in v.items() if not _is_undefined(x)}
    if isinstance(v, (list, tuple)):
        return [None if _is_undefined(x) else to_jsonable(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted((to_jsonable(x) for x in v), key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(v, datetime.datetime):
        return v.isoformat()
    if isinstance(v, enum.Enum):
        return v.name
    if _is_undefined(v):
        return None
    if isinstance(v, (bytearray, memoryview)):
        return {"$bytes_b64": base64.b64encode(bytes(v)).decode("ascii")}
    return {"$unsupported": type(v).__name__}


def as_text(v) -> str:
    """문자열 필드(bytes로 올 수 있음)를 str로."""
    if v is None or _is_undefined(v):
        return ""
    if isinstance(v, bytes):
        return v.decode("latin-1")
    return str(v)


def canonical_hash(obj, exclude=()) -> str:
    """정렬된 JSON의 sha256. exclude에 있는 최상위 키는 해시에서 뺀다(자주 바뀌는 휘발성 필드)."""
    if exclude and isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k not in exclude}
    data = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()
