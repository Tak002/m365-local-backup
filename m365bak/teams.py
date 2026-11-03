"""새 Teams IndexedDB → 대화·메시지 추출과 정규화(thread.json / transcript.md).

DB 이름 형식: Teams:<manager>:react-web-client:<tenantId>:<userObjectId>:<locale>
- replychain-manager / replychains*  : [conversationId, replyChainId] → {messageMap: {msgId: 메시지}}
- conversation-manager / conversations : thread ID → 대화 메타
- profiles / profiles                : MRI(8:orgid:<objectId>) → 프로필
"""
import dataclasses
import json
import re

from . import jsonable as J
from . import render as R

PROFILE_FIELDS = ("mri", "displayName", "email", "userPrincipalName", "givenName", "surname",
                  "jobTitle", "department", "companyName", "userType", "objectId")
# 해시에서 제외할 휘발성 필드(대화의 마지막 메시지 여부처럼 다른 메시지 도착만으로 바뀌는 값)
VOLATILE = ("isConversationLastMessage", "isConversationLastMessageSanitized", "sendPipelineStatus",
            "sendPipelineOAMState", "clientLieResolvedTime")
_ORGID = re.compile(r"8:orgid:[0-9a-f-]{36}")


def _manager(db_name: str) -> str | None:
    parts = db_name.split(":")
    return parts[1] if len(parts) > 2 and parts[0] == "Teams" else None


def _int(v) -> int:
    try:
        return int(float(J.as_text(v) or 0))
    except ValueError:
        return 0


@dataclasses.dataclass
class TeamsData:
    me: str | None
    conversations: dict      # conv_id → 메타(jsonable)
    messages: dict           # conv_id → {msg_id → 메시지(jsonable)}
    people: dict             # mri → 프로필 요약
    stats: dict


def extract(snap) -> TeamsData:
    convs = {}
    for _, db in snap.databases(lambda n: _manager(n) == "conversation-manager"):
        for key, value, seq, _rec in snap.latest(db, "conversations").values():
            if isinstance(value, dict) and value.get("id"):
                cid = J.as_text(value["id"])
                if cid not in convs or seq > convs[cid][0]:
                    convs[cid] = (seq, value)

    best = {}   # (cid, mid) → (version, seq, msg)
    chain_versions = 0
    for _, db in snap.databases(lambda n: _manager(n) == "replychain-manager"):
        for store in [s for s in db.object_store_names if s and s.startswith("replychains")]:
            for v in snap.versions(db, store):
                mm = v.value.get("messageMap") if isinstance(v.value, dict) else None
                if not isinstance(mm, dict):
                    continue
                chain_versions += 1
                for m in mm.values():
                    if not isinstance(m, dict):
                        continue
                    cid, mid = J.as_text(m.get("conversationId")), J.as_text(m.get("id"))
                    if not cid or not mid:
                        continue
                    rank = (_int(m.get("version")), v.seq)
                    cur = best.get((cid, mid))
                    if cur is None or rank > cur[:2]:
                        best[(cid, mid)] = (*rank, m)

    people = {}
    for _, db in snap.databases(lambda n: _manager(n) == "profiles"):
        for key, value, seq, _rec in snap.latest(db, "profiles").values():
            if isinstance(value, dict) and value.get("mri"):
                p = {f: J.to_jsonable(value.get(f)) for f in PROFILE_FIELDS if J.as_text(value.get(f))}
                people[p["mri"]] = p

    messages, me = {}, None
    for (cid, mid), (_ver, _seq, m) in best.items():
        raw = J.to_jsonable(m)
        messages.setdefault(cid, {})[mid] = raw
        if me is None and raw.get("isSentByCurrentUser") and raw.get("creator"):
            me = raw["creator"]
    stats = {"conversations_meta": len(convs), "conversations_with_messages": len(messages),
             "messages": len(best), "replychain_versions": chain_versions}
    return TeamsData(me, {c: J.to_jsonable(v) for c, (_s, v) in convs.items()}, messages, people, stats)


# ---------------------------------------------------------------- 정규화

def item_id(raw) -> str:
    return str(raw.get("id"))


def item_version(raw) -> str:
    return str(raw.get("version") or "")


def item_order(raw) -> int:
    return _int(raw.get("version"))


def meta_order(conv) -> str:
    return f"{_int((conv or {}).get('version')):020d}"


def item_time(raw) -> float:
    return float(raw.get("originalArrivalTime") or raw.get("id") or 0)


def is_deleted(raw) -> bool:
    p = raw.get("properties") or {}
    return bool(p.get("deletetime") or p.get("hardDeleteTime") or raw.get("deletionInfo"))


def prefer_old(old, new) -> bool:
    """새 버전이 삭제본이고 예전 본에 내용이 있으면 예전 본을 원본으로 유지한다."""
    return is_deleted(new) and not is_deleted(old) and bool(J.as_text(old.get("content")).strip())


def _name(mri, people, fallback=None):
    p = people.get(mri) or {}
    return p.get("displayName") or fallback or mri


SYSTEM_THREADS = {"48:notes": "메모(나에게)", "48:notifications": "알림(활동 피드)", "48:calllogs": "통화 기록",
                  "48:mentions": "멘션", "48:drafts": "임시 보관", "48:saved": "저장한 메시지"}


def title(conv, msgs, people, me) -> str:
    cid = (conv or {}).get("id") or next((m.get("conversationId") for m in msgs.values()), "") or ""
    if cid in SYSTEM_THREADS:
        return SYSTEM_THREADS[cid]
    tp = (conv or {}).get("threadProperties") or {}
    if J.as_text(tp.get("topic")).strip():
        return J.as_text(tp["topic"]).strip()
    # 주제 없는 채팅: 나를 뺀 상대 이름(chatTitle.longTitle에는 내 이름도 들어 있다)
    names = [_name(m["id"], people) for m in (conv or {}).get("members") or [] if m.get("id") and m["id"] != me]
    if not names:
        my_name = (people.get(me) or {}).get("displayName")
        long_title = J.as_text(((conv or {}).get("chatTitle") or {}).get("longTitle"))
        names = [n.strip() for n in long_title.split(", ") if n.strip() and n.strip() != my_name]
    if not names:
        names = sorted({m.get("imDisplayName") for m in msgs.values()
                        if m.get("imDisplayName") and m.get("creator") != me})
    if len(names) > 6:
        return ", ".join(names[:6]) + f" 외 {len(names) - 6}명"
    return ", ".join(names) or cid


_SYS_LABEL = {
    "ThreadActivity/AddMember": "멤버 추가",
    "ThreadActivity/DeleteMember": "멤버 제거",
    "ThreadActivity/MemberJoined": "참가",
    "ThreadActivity/MemberLeft": "나감",
    "ThreadActivity/TopicUpdate": "대화 이름 변경",
    "ThreadActivity/FileAdd": "파일 추가",
    "Event/Call": "통화",
    "RichText/Media_CallRecording": "통화 녹화",
    "RichText/Media_CallTranscript": "통화 전사본",
    "RichText/Media_CallLogRecording": "통화 녹화",
    "RichText/Media_CallLogTranscript": "통화 전사본",
}


def _kind(mtype: str) -> str:
    if mtype in ("Text", "RichText", "RichText/Html") or (mtype.startswith("RichText") and "Media_" not in mtype):
        return "message"
    if mtype.startswith("Event/Call") or "Media_Call" in mtype:
        return "call"
    return "system"


def _describe_system(mtype, content, people) -> str:
    label = _SYS_LABEL.get(mtype, mtype or "이벤트")
    detail = ""
    if mtype == "ThreadActivity/TopicUpdate":
        m = re.search(r"<value>(.*?)</value>", content, re.S)
        detail = R.plain_to_md(m.group(1)) if m else ""
    elif mtype.startswith("ThreadActivity/"):
        who = list(dict.fromkeys(_ORGID.findall(content)))
        names = [_name(w, people) for w in who]
        if not any(n != w for n, w in zip(names, who)):
            names = re.findall(r'"friendlyname"\s*:\s*"([^"]+)"', content) or names
        detail = ", ".join(names)
    elif mtype == "Event/Call":
        d = re.search(r"<duration>([\d.]+)</duration>", content)
        ended = 'type="ended"' in content
        if d:
            s = int(float(d.group(1)))
            detail = f"종료, {s // 60}분 {s % 60}초"
        else:
            detail = "종료" if ended else "시작"
    elif "Media_Call" in mtype:
        m = re.search(r"<(?:Title|OriginalName)[^>]*>(.*?)</", content, re.S)
        detail = R.plain_to_md(m.group(1)) if m else ""
    return f"{label}: {detail}" if detail else label


def _files(props) -> list:
    raw = props.get("files")
    if not raw or raw == "[]":
        return []
    try:
        files = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    out = []
    for f in files if isinstance(files, list) else []:
        if not isinstance(f, dict):
            continue
        info = f.get("fileInfo") or {}
        url = f.get("objectUrl") or info.get("fileUrl") or info.get("shareUrl")
        out.append({"id": "file:" + str(f.get("id") or f.get("itemid") or url),
                    "name": f.get("fileName") or f.get("title") or "file",
                    "url": url, "type": f.get("fileType")})
    return out


def files(raw) -> list:
    """메시지의 첨부 파일 목록(att id, 이름, URL)."""
    return _files(raw.get("properties") or {})


def _reactions(props, people) -> list:
    out = []
    for e in props.get("emotions") or []:
        if isinstance(e, dict) and e.get("users"):
            users = [_name(u.get("mri"), people) for u in e["users"] if isinstance(u, dict)]
            out.append({"reaction": e.get("key"), "count": len(users), "users": users})
    return out


def normalize(conv_id, conv, msgs: dict, people, me, annotations: dict, attachment_paths: dict | None = None) -> dict:
    """attachment_paths: (msg_id, att_id) → {"path", "thumbnail"}. att_id는 file:<id> 또는 img:<AMS object id>."""
    from .media import message_images
    paths = attachment_paths or {}
    out = []
    for mid, m in sorted(msgs.items(), key=lambda kv: item_time(kv[1])):
        props = m.get("properties") or {}
        mtype = m.get("messageType") or ""
        kind = _kind(mtype)
        content = J.as_text(m.get("content"))
        if kind == "message":
            text = R.plain_to_md(content) if mtype == "Text" else R.html_to_md(content)
        else:
            text = _describe_system(mtype, content, people)
        mri = m.get("creator")
        sender = {"name": m.get("imDisplayName") or _name(mri, people), "mri": mri,
                  "email": (people.get(mri) or {}).get("email")}
        ann = annotations.get(mid) or {}
        parent = m.get("parentMessageId")
        out.append({
            "id": mid,
            "time": R.iso(R.ms_to_dt(item_time(m))),
            "kind": kind,
            "type": mtype,
            "sender": sender,
            "from_me": bool(m.get("isSentByCurrentUser")) or (me is not None and mri == me),
            "subject": props.get("subject") or None,
            "text": text,
            "reply_to": parent if parent and parent != mid else None,
            "attachments": [{**f, "path": (paths.get((mid, f["id"])) or {}).get("path")} for f in _files(props)],
            "images": [{"object_id": oid, "path": (paths.get((mid, "img:" + oid)) or {}).get("path"),
                        "thumbnail": bool((paths.get((mid, "img:" + oid)) or {}).get("thumbnail"))}
                       for oid in (message_images(content) if kind == "message" else [])],
            "reactions": _reactions(props, people),
            "edited": R.iso(R.ms_to_dt(props.get("edittime"))) if props.get("edittime") else None,
            "deleted": bool(ann.get("deleted_at")) or is_deleted(m),
            "importance": props.get("importance") or None,
        })
    conv = conv or {}
    tp = conv.get("threadProperties") or {}
    members = []
    for mem in conv.get("members") or []:
        mri = mem.get("id")
        if mri:
            members.append({"mri": mri, "name": _name(mri, people), "email": (people.get(mri) or {}).get("email"),
                            "role": mem.get("role")})
    times = [m["time"] for m in out if m["time"]]
    return {
        "source": "teams",
        "conversation_id": conv_id,
        "title": title(conv, msgs, people, me),
        "type": conv.get("type") or tp.get("threadType"),
        "team_id": conv.get("teamId"),
        "created": R.iso(R.ms_to_dt(tp.get("createdat"))) if tp.get("createdat") else None,
        "members": members,
        "first_message": min(times) if times else None,
        "last_message": max(times) if times else None,
        "message_count": sum(1 for m in out if m["kind"] == "message"),
        "messages": out,
    }


def transcript(thread: dict, exported_at: str) -> str:
    fm = R.front_matter({
        "source": "teams", "conversation_id": thread["conversation_id"], "title": thread["title"],
        "type": thread["type"], "members": [m["name"] for m in thread["members"]],
        "first_message": thread["first_message"], "last_message": thread["last_message"],
        "message_count": thread["message_count"], "exported_at": exported_at,
    })
    lines = [fm, f"# {thread['title']}", ""]
    msgs = thread["messages"]
    threaded = any(m["reply_to"] for m in msgs)
    if threaded:
        chains, order = {}, []
        for m in msgs:
            root = m["reply_to"] or m["id"]
            if root not in chains:
                chains[root] = []
                order.append(root)
            chains[root].append(m)
        for root in order:
            chain = chains[root]
            head = chain[0]
            topic = head["subject"] or (head["text"].splitlines() or [""])[0][:60]
            lines += [f"## [스레드] {head['time'][:16].replace('T', ' ')} · {head['sender']['name']}: {topic}", ""]
            for m in chain:
                lines += _message_md(m, full_time=True)
    else:
        day = None
        for m in msgs:
            d = m["time"][:10] if m["time"] else "?"
            if d != day:
                day = d
                dt = R.iso_to_dt(m["time"])
                lines += [f"## {R.day_label(dt) if dt else d}", ""]
            lines += _message_md(m, full_time=False)
    return "\n".join(lines).rstrip() + "\n"


def _message_md(m, full_time: bool) -> list:
    t = (m["time"] or "")
    t = t[:16].replace("T", " ") if full_time else t[11:16]
    if m["kind"] != "message":
        return [f"> [{t}] {m['sender']['name']} — {m['text']}" if m["kind"] == "call" else f"> [{t}] [시스템] {m['text']}", ""]
    flags = []
    if m["edited"]:
        flags.append("편집됨")
    if m["deleted"]:
        flags.append("삭제됨")
    if m["importance"] and m["importance"] not in ("normal", ""):
        flags.append(f"중요도:{m['importance']}")
    head = f"**{m['sender']['name']}** · {t}" + (f" [{', '.join(flags)}]" if flags else "")
    out = [head]
    if m["subject"]:
        out.append(f"**제목:** {m['subject']}")
    out.append(m["text"] or "(내용 없음)")
    for f in m["attachments"]:
        if f.get("path"):
            out.append(f"[첨부] [{f['name']}]({f['path'].replace(' ', '%20')})"
                       + (f" · [원본 링크]({f['url']})" if f.get("url") else ""))
        else:
            out.append(f"[첨부] [{f['name']}]({f['url']}) (로컬 사본 없음)" if f.get("url") else f"[첨부] {f['name']}")
    missing = 0
    for img in m.get("images") or []:
        if img["path"]:
            out.append(f"[이미지] [{img['path'].rsplit('/', 1)[-1]}]({img['path'].replace(' ', '%20')})"
                       + (" (썸네일)" if img["thumbnail"] else ""))
        else:
            missing += 1
    if missing:
        out.append(f"[이미지] {missing}개(로컬 캐시에 없음)")
    if m["reactions"]:
        out.append("[반응] " + "; ".join(f"{r['reaction']}×{r['count']} ({', '.join(r['users'])})" for r in m["reactions"]))
    return out + [""]
