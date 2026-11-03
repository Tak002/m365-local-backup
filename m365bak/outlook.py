"""새 Outlook IndexedDB → 메일 대화 추출과 정규화(thread.json / transcript.md).

DB owa-offline-data-<mailboxGuid>:
- messageBodies     : ItemId → 메일 전체(From/To/Cc, NormalizedBody/UniqueBody, Attachments[])
- conversations     : ConversationId → 대화 목록 행(ConversationTopic, Preview, UniqueSenders)
- conversationNodes : ConversationId → 스레드 구조(InternetMessageId, Items[id, 시각])
- folders           : FolderId → 폴더 이름
DB owa-attachment-data-<mailboxGuid> / attachments : 첨부 캐시 메타
첨부 파일: Olk/Attachments/ooa-<mailboxGuid>/<sha256(attachmentId 또는 base64url)>/<파일>
"""
import dataclasses
import hashlib
import pathlib

from . import jsonable as J
from . import render as R

VOLATILE = ("MetaData", "InstanceKey", "CopilotInboxScore", "CopilotInboxHeadline")


def _is_mailbox_db(name: str, prefix: str) -> bool:
    return name.startswith(prefix) and len(name) > len(prefix)


@dataclasses.dataclass
class OutlookData:
    mailbox_guid: str | None
    mailbox: str | None
    folders: dict          # folderId → {name, type}
    conversations: dict    # conv_id → ConversationType(jsonable)
    nodes: dict            # conv_id → Conversation(conversationNodes, jsonable)
    messages: dict         # conv_id → {item_id → messageBody(jsonable)}
    att_meta: list         # owa-attachment-data 레코드
    stats: dict


def extract(snap) -> OutlookData:
    folders, convs, nodes, messages, att_meta = {}, {}, {}, {}, []
    guid = mailbox = None
    recovered = 0
    for name, db in snap.databases(lambda n: _is_mailbox_db(n, "owa-offline-data-")):
        guid = name[len("owa-offline-data-"):]
        for _k, v, _s, _r in snap.latest(db, "folders").values():
            if isinstance(v, dict) and v.get("id"):
                folders[v["id"]] = {"name": J.as_text(v.get("displayName")),
                                    "type": J.as_text(v.get("distinguishedFolderType")) or None}
                mailbox = mailbox or (v.get("mailboxInfo") or {}).get("mailboxSmtpAddress")
        for _k, v, _s, _r in snap.latest(db, "conversations").values():
            cid = ((v or {}).get("ConversationId") or {}).get("Id") if isinstance(v, dict) else None
            if cid:
                convs[cid] = J.to_jsonable(v)
        for _k, v, _s, _r in snap.latest(db, "conversationNodes").values():
            if isinstance(v, dict) and v.get("id"):
                nodes[J.as_text(v["id"])] = J.to_jsonable(v)
        for _k, v, _s, rec in snap.latest(db, "messageBodies").values():
            if not isinstance(v, dict):
                continue
            iid = (v.get("ItemId") or {}).get("Id")
            cid = (v.get("ConversationId") or {}).get("Id") or f"no-conversation:{iid}"
            if iid:
                messages.setdefault(cid, {})[iid] = J.to_jsonable(v)
                recovered += rec
    for name, db in snap.databases(lambda n: _is_mailbox_db(n, "owa-attachment-data-")):
        for _k, v, _s, _r in snap.latest(db, "attachments").values():
            if isinstance(v, dict):
                att_meta.append(J.to_jsonable(v))
    stats = {"folders": len(folders), "conversations_meta": len(convs), "conversation_nodes": len(nodes),
             "conversations_with_bodies": len(messages), "bodies": sum(len(m) for m in messages.values()),
             "bodies_recovered_from_old_versions": recovered, "attachment_meta": len(att_meta)}
    return OutlookData(guid, mailbox, folders, convs, nodes, messages, att_meta, stats)


# ---------------------------------------------------------------- 첨부 파일 매칭

def _b64url(s: str) -> str:
    return s.replace("+", "-").replace("/", "_")


class AttachmentIndex:
    def __init__(self, root: pathlib.Path, mailbox_guid: str | None):
        self.dirs, self.by_name_size, self.by_size = {}, {}, {}
        base = root / f"ooa-{mailbox_guid}" if mailbox_guid else root
        if not base.exists():
            return
        for d in base.iterdir():
            if not d.is_dir():
                continue
            files = [p for p in d.rglob("*") if p.is_file() and p.stat().st_size > 0]
            if not files:
                continue
            self.dirs[d.name] = files
            for p in files:
                size = p.stat().st_size
                self.by_size.setdefault(size, []).append(p)
                self.by_name_size.setdefault((p.name, size), []).append(p)

    def match(self, att_id: str, name: str, size) -> pathlib.Path | None:
        for key in {att_id, _b64url(att_id), _b64url(att_id).rstrip("=")}:
            h = hashlib.sha256(key.encode()).hexdigest()
            if h in self.dirs:
                return max(self.dirs[h], key=lambda p: p.stat().st_size)
        cands = self.by_name_size.get((name, size)) or []
        if len(cands) == 1:
            return cands[0]
        cands = self.by_size.get(size) or []
        return cands[0] if len(cands) == 1 else None


class ImageStore:
    """owa-image-data-<guid>/images: {attachmentId, image(Blob), thumbnailType, messageBodyId?}.
    본문 속 이미지(서명 로고·붙여 넣은 캡처)와 이미지 첨부의 원본·썸네일이 Blob으로 들어 있다.
    같은 첨부에 여러 크기가 있으면 가장 큰 것을 쓴다."""

    def __init__(self, snap):
        self.by_att: dict[str, list] = {}
        for _name, db in snap.databases(lambda n: _is_mailbox_db(n, "owa-image-data-")):
            for rec in snap.latest_records(db, "images").values():
                v = rec.value
                if isinstance(v, dict) and v.get("attachmentId") and v.get("image") is not None:
                    self.by_att.setdefault(J.as_text(v["attachmentId"]), []).append(rec)

    def best(self, att_id: str) -> tuple[bytes, str | None, bool] | None:
        """(바이트, mime, 썸네일 여부). 썸네일 여부는 같은 첨부의 더 큰 사본이 있었는지가 아니라
        thumbnailType으로 판단한다(0 = 원본)."""
        recs = []
        for key in dict.fromkeys((att_id, _b64url(att_id))):
            recs += self.by_att.get(key, [])
        best = None
        for rec in recs:
            try:
                with rec.get_blob_stream(rec.value["image"]) as f:
                    data = f.read()
                mime = rec.resolve_blob_index(rec.value["image"]).mime_type
            except Exception:
                continue
            if data and (best is None or len(data) > len(best[0])):
                best = (data, mime or None, J.as_text(rec.value.get("thumbnailType")) not in ("0", ""))
        return best


# ---------------------------------------------------------------- 정규화

def is_inline(att) -> bool:
    """본문에 cid:로 박힌 이미지(서명 로고 등). Olk/Attachments 캐시에는 대부분 없다."""
    return bool(att.get("IsInline") or att.get("ContentId"))


def item_id(raw) -> str:
    return (raw.get("ItemId") or {}).get("Id")


def item_version(raw) -> str:
    return (raw.get("ItemId") or {}).get("ChangeKey") or raw.get("LastModifiedTime") or ""


def item_order(raw) -> str:
    return R.iso(R.iso_to_dt(raw.get("LastModifiedTime"))) or ""


def meta_order(meta) -> str:
    conv = (meta or {}).get("conversation") or {}
    return R.iso(R.iso_to_dt(conv.get("LastModifiedTime") or conv.get("LastDeliveryTime"))) or ""


def item_time(raw) -> str:
    dt = R.iso_to_dt(raw.get("DateTimeSent") or raw.get("DateTimeReceived") or raw.get("DateTimeCreated"))
    return R.iso(dt) or ""


def _truncated(raw) -> bool:
    b = raw.get("NormalizedBody") or raw.get("Body") or {}
    return bool(b.get("IsTruncated"))


def prefer_old(old, new) -> bool:
    """새 본문이 잘려 있고 예전 본은 온전하면 예전 본을 유지한다."""
    return _truncated(new) and not _truncated(old)


def is_deleted(raw) -> bool:
    return False


def _person(m) -> dict | None:
    if not isinstance(m, dict):
        return None
    mb = m.get("Mailbox") or m
    if not (mb.get("Name") or mb.get("EmailAddress")):
        return None
    return {"name": mb.get("Name"), "email": mb.get("EmailAddress")}


def _people(lst) -> list:
    return [p for p in (_person(x) for x in (lst or [])) if p]


def _fmt(p) -> str:
    if not p:
        return "?"
    if p.get("name") and p.get("email") and p["name"] != p["email"]:
        return f"{p['name']} <{p['email']}>"
    return p.get("name") or p.get("email")


def _body_md(raw) -> tuple[str, str]:
    for field, label in (("UniqueBody", "unique"), ("NormalizedBody", "normalized"), ("Body", "body")):
        b = raw.get(field)
        if isinstance(b, dict) and J.as_text(b.get("Value")).strip():
            v = J.as_text(b["Value"])
            text = R.html_to_md(v) if (b.get("BodyType") or "HTML").upper() == "HTML" else R.plain_to_md(v)
            if text.strip():
                return text, label
    return J.as_text(raw.get("Preview")), "preview"


def normalize(conv_id, conv, node, msgs: dict, folders: dict, attachment_paths: dict) -> dict:
    """attachment_paths: (item_id, attachment_id) → {"path": 대화 폴더 기준 상대경로, "thumbnail": bool}."""
    # 같은 메일이 여러 폴더(보낸편지함·받은편지함, 폴더 이동)에 있으면 InternetMessageId로 하나로 합친다
    groups = {}
    for iid, m in msgs.items():
        key = m.get("InternetMessageId") or iid
        groups.setdefault(key, []).append(m)
    out = []
    for key, copies in groups.items():
        copies.sort(key=lambda m: (not _truncated(m), m.get("LastModifiedTime") or ""), reverse=True)
        m = copies[0]
        iid = item_id(m)
        text, src = _body_md(m)
        atts = []
        for a in m.get("Attachments") or []:
            aid = ((a.get("AttachmentId") or {}).get("Id")) or ""
            saved = attachment_paths.get((iid, aid))
            for c in copies[1:]:
                saved = saved or attachment_paths.get((item_id(c), aid))
            atts.append({"name": a.get("Name"), "size": a.get("Size"), "content_type": a.get("ContentType"),
                         "inline": is_inline(a), "path": (saved or {}).get("path"),
                         "thumbnail": bool((saved or {}).get("thumbnail"))})
        fset = [folders.get((c.get("ParentFolderId") or {}).get("Id"), {}).get("name") for c in copies]
        cal = None
        if m.get("Start") or m.get("CalendarItemType"):
            cal = {"start": m.get("Start"), "end": m.get("End"),
                   "location": (m.get("Location") or {}).get("DisplayName") if isinstance(m.get("Location"), dict) else m.get("Location")}
        out.append({
            "id": iid,
            "internet_message_id": m.get("InternetMessageId"),
            "sent": R.iso(R.iso_to_dt(m.get("DateTimeSent"))),
            "received": R.iso(R.iso_to_dt(m.get("DateTimeReceived"))),
            "from": _person(m.get("From")) or _person(m.get("Sender")),
            "to": _people(m.get("ToRecipients")),
            "cc": _people(m.get("CcRecipients")),
            "bcc": _people(m.get("BccRecipients")),
            "subject": m.get("Subject"),
            "folders": sorted({f for f in fset if f}),
            "item_class": m.get("ItemClass"),
            "importance": m.get("Importance"),
            "is_read": m.get("IsRead"),
            "text": text,
            "text_source": src,
            "body_truncated": _truncated(m),
            "attachments": atts,
            "calendar": cal,
        })
    out.sort(key=lambda x: x["sent"] or x["received"] or "")

    cached_imids = {m["internet_message_id"] for m in out}
    cached_ids = {iid for c in groups.values() for iid in (item_id(x) for x in c)}
    uncached = []
    for n in (node or {}).get("conversationNodes") or []:
        if n.get("InternetMessageId") in cached_imids:
            continue
        for it in n.get("Items") or []:
            if it.get("id") in cached_ids:
                continue
            uncached.append({"id": it.get("id"), "internet_message_id": n.get("InternetMessageId"),
                             "sent": R.iso(R.iso_to_dt(it.get("DateTimeSent"))),
                             "received": R.iso(R.iso_to_dt(it.get("DateTimeReceived")))})
            break
    uncached.sort(key=lambda x: x["sent"] or "")

    conv = conv or {}
    topic = (conv.get("ConversationTopic") or next((m["subject"] for m in out if m["subject"]), None) or "").strip() \
        or "(제목 없음)"
    participants = {}
    for m in out:
        for p in [m["from"], *m["to"], *m["cc"]]:
            if p and p.get("email"):
                participants.setdefault(p["email"].lower(), p)
    times = [m["sent"] or m["received"] for m in out if m["sent"] or m["received"]]
    times += [u["sent"] for u in uncached if u["sent"]]
    folder_names = sorted({f for m in out for f in m["folders"]} |
                          ({folders.get((conv.get("ParentFolderId") or {}).get("Id"), {}).get("name")} - {None}))
    return {
        "source": "outlook",
        "conversation_id": conv_id,
        "title": topic,
        "folders": folder_names,
        "participants": list(participants.values()),
        "first_message": min(times) if times else None,
        "last_message": max(times) if times else None,
        "message_count": len(out),
        "uncached_count": len(uncached),
        "preview": conv.get("Preview"),
        "messages": out,
        "uncached": uncached,
    }


def transcript(thread: dict, exported_at: str, mailbox: str | None) -> str:
    fm = R.front_matter({
        "source": "outlook", "mailbox": mailbox, "conversation_id": thread["conversation_id"],
        "title": thread["title"], "folders": thread["folders"],
        "participants": [_fmt(p) for p in thread["participants"]],
        "first_message": thread["first_message"], "last_message": thread["last_message"],
        "message_count": thread["message_count"], "uncached_count": thread["uncached_count"],
        "exported_at": exported_at,
    })
    lines = [fm, f"# {thread['title']}", ""]
    for m in thread["messages"]:
        t = (m["sent"] or m["received"] or "?")[:16].replace("T", " ")
        lines.append(f"## {t} · {_fmt(m['from'])}")
        if m["to"]:
            lines.append(f"- 받는 사람: {', '.join(_fmt(p) for p in m['to'])}")
        if m["cc"]:
            lines.append(f"- 참조: {', '.join(_fmt(p) for p in m['cc'])}")
        if m["subject"] and m["subject"] != thread["title"]:
            lines.append(f"- 제목: {m['subject']}")
        if m["folders"]:
            lines.append(f"- 폴더: {', '.join(m['folders'])}")
        if m["calendar"]:
            c = m["calendar"]
            lines.append(f"- 일정: {c.get('start')} ~ {c.get('end')}" + (f" @ {c['location']}" if c.get("location") else ""))
        inline_missing = 0
        for a in m["attachments"]:
            size = R.human_size(a["size"])
            if a["path"]:
                kind = "인라인 이미지" if a["inline"] else "첨부"
                note = ", 썸네일만 캐시됨" if a["thumbnail"] else ""
                lines.append(f"- {kind}: [{a['name']}]({a['path'].replace(' ', '%20')}) ({size}{note})")
            elif a["inline"]:
                inline_missing += 1
            else:
                lines.append(f"- 첨부: {a['name']} ({size}, 로컬 캐시에 없음)")
        if inline_missing:
            lines.append(f"- 인라인 이미지 {inline_missing}개(로컬 캐시에 없음)")
        if m["body_truncated"]:
            lines.append("- [본문 일부만 캐시됨]")
        if m["text_source"] == "preview":
            lines.append("- [본문 미캐시, 미리보기만 있음]")
        lines += ["", m["text"] or "(본문 없음)", "", "---", ""]
    if thread["preview"] and not thread["messages"]:
        lines += ["## 미리보기(가장 최근 메일 일부, 본문 미캐시)", "", thread["preview"], ""]
    if thread["uncached"]:
        lines += ["## 본문이 로컬에 없는 메일", ""]
        for u in thread["uncached"]:
            lines.append(f"- {(u['sent'] or '?')[:16].replace('T', ' ')} (InternetMessageId: {u['internet_message_id']})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
