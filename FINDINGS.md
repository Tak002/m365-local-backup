# 새 Teams / 새 Outlook 로컬 저장소 분석

작성 2026-10-29. 스냅샷 `_snapshot/20261029-140559` 기준(두 앱 종료 후 복사본에서만 읽음).
도구: rea 6.8.0(`inspect-sqlite-database`, `inspect-desktop-application`), `ccl_chromium_reader` 0.3.19(IndexedDB/LevelDB/V8 직렬화 파서).

---

## 0. 결론

- 두 앱 모두 대화 데이터는 **WebView2 프로필의 Chromium IndexedDB**(LevelDB + 외부 blob 디렉터리)에 있다. SQLite가 아니다.
- 새 Teams에 있는 SQLite(WAL)는 **1DS 텔레메트리 큐**다(rea로 스키마 확인). 채팅 데이터는 들어 있지 않다.
- 로컬 저장소는 **부분 캐시**다. 앱이 최근에 동기화했거나 열어 본 범위만 있고, Outlook은 주기적인 Cleanup 작업으로 캐시를 비운다.
  → 백업은 자주 돌려 **누적(append-only) 보관**해야 한다.

## 1. 새 Teams (MSTeams_8wekyb3d8bbwe, 26260.1717.5229.7049)

위치: `%LOCALAPPDATA%\Packages\MSTeams_8wekyb3d8bbwe\LocalCache\Microsoft\MSTeams\EBWebView\WV2Profile_tfw\IndexedDB\`
- `https_teams.microsoft.com_0.indexeddb.leveldb` (16 MB), `…indexeddb.blob` (0.8 MB)
- DB는 106개이고, 이름 형식은 `Teams:<manager>:react-web-client:<tenantId>:<userObjectId>:ko-kr`이다.

| DB(manager) / store | 키 | 내용 | 건수(키별 최신) |
|---|---|---|---|
| `replychain-manager` / `replychains`, `replychains-2` | `[conversationId, replyChainId]` | 답글 체인 1개에 해당. `messageMap{msgId → 메시지}` | 4,849 + 395 체인, 메시지 4,903 + 395건, 대화 32개, 2026-05-11 ~ 10-29 |
| `conversation-manager` / `conversations` | thread ID(`19:…@thread.v2`, `48:notes` 등) | 대화 메타 | 44 |
| `profiles` / `profiles` | MRI `8:orgid:<AAD objectId>` | displayName, email, UPN, 직함, 부서 | 278 |
| `syncstate-manager` / `syncstates` | thread ID | 서버 동기화 커서(syncToken) — **읽지 않음** | 187 |

메시지 객체(`messageMap` 값)의 주요 필드:
- 식별: `id`(= 서버 도착 시각 ms), `version`(편집하면 증가), `sequenceId`, `clientMessageId`, `conversationId`, `parentMessageId`(체인 루트)
- 발신자: `creator`(MRI), `imDisplayName`, `fromDisplayNameInToken`, `isSentByCurrentUser`
- 시각: `originalArrivalTime`, `clientArrivalTime`(epoch ms, float)
- 본문: `content`(HTML 문자열, 일부는 bytes), `messageType`(`RichText/Html`, `Text`, `ThreadActivity/*`, `Event/Call` …), `contentType`
- `properties`: `files`(JSON 문자열, SharePoint/OneDrive 링크), `cards`, `mentions`, `links`, `emotions`(반응), `edittime`, `deletetime`, `subject`, `title`, `importance`
- 삭제: `deletionInfo`, `properties.deletetime`/`hardDeleteTime`

대화 메타의 주요 필드: `id`, `type`, `threadProperties.{topic, threadType, createdat, creator}`, `chatTitle.{longTitle, shortTitle}`, `members[]`, `teamId`(채널일 때), `lastMessageTimeUtc`.

## 2. 새 Outlook (Microsoft.OutlookForWindows, 1.2026.1013.100)

위치: `%LOCALAPPDATA%\Microsoft\Olk\`
- `EBWebView\Default\IndexedDB\https_outlook.office.com_0.indexeddb.leveldb` (11 MB) + `.blob` (50 MB, 큰 값은 외부 파일로 저장됨)
- `Attachments\ooa-<mailboxGuid>\<sha256>\<파일>` (23.7 MB, 첨부 캐시)

DB `owa-offline-data-<mailboxGuid>`:

| store | 키 | 내용 | 건수(키별 최신) |
|---|---|---|---|
| `conversations` | ConversationId | 대화 목록 행: `ConversationTopic`, `LastSender`, `UniqueSenders`, `Preview`, `ItemIds`, `MessageCount`, `ParentFolderId` | 163 |
| `conversationNodes` | ConversationId | 스레드 구조: `conversationNodes[]{InternetMessageId, Items[{id, DateTimeSent/Received}], IsRootNode}` | 144 |
| `messageBodies` | ItemId | **메일 전체**: `Subject`, `From`, `ToRecipients`, `CcRecipients`, `DateTimeSent/Received`, `InternetMessageId`, `NormalizedBody{BodyType, Value(HTML), IsTruncated}`, `UniqueBody`, `Attachments[]` | 221 |
| `messages` | ItemId | 목록 행(ItemRow) | 3 |
| `folders` | FolderId | `displayName`, `distinguishedFolderType`, `totalMessageCount` | 12 |
| `syncState` | 이름 | 동기화 시각과 **Cleanup 작업**(MessageBodies/Attachments/Conversations…) | 31 |
| `settings`/`syncingFolders` | — | 동기화 대상 폴더 목록 | — |

DB `owa-attachment-data-<mailboxGuid>` / `attachments`: `attachmentId`, `fileName`, `fileSize`, `messageBodyId`, `timeCached` (84건)

첨부 파일 매핑 규칙(실측):
- 디렉터리명 = `sha256(attachmentId)` 또는 `sha256(base64url(attachmentId))`
- 나머지(로컬 임시 ID 96자, `pendingSave`)는 `(fileName, fileSize)`로 맞춘다. 크기는 83/84건 일치했다.
- 안쪽 파일명은 원래 파일명이거나 해시다(`e3b0c442…` = sha256("")).

## 3. 파싱 시 주의(실측)

- `ccl_chromium_reader`의 `iterate_records(live_only=…)`는 이 인자를 **무시**하고 LevelDB에 남은 모든 버전을 돌려준다(삭제 마커는 value=None).
  → 키별로 `ldb_seq_no`가 가장 큰 것을 최신본으로 쓴다. compaction 전의 구버전에서 캐시에서 빠진 메시지를 복구할 수 있다(Teams 6건 추가 복구 확인).
- 외부 blob 파일이 없는 레코드가 있다(Outlook messageBodies 7건). `bad_deserializer_data_handler`를 넘기면 건너뛴다. 넘기지 않으면 전체 순회가 예외로 중단된다.
- 시각: Teams는 epoch ms(float), Outlook은 ISO8601(+09:00).
- 콘솔 출력은 UTF-8을 강제해야 한다(기본 cp949에서 한글이 깨짐).

## 4. rea 판정 기록

- `inspect-sqlite-database tfw/telemetry…` → `events(record_id, tenant_token, latency, persistence, timestamp, retry_count, reserved_until, payload BLOB)`, `settings(name, value)`: 텔레메트리 오프라인 큐다.
- `inspect-desktop-application`: Teams 설치본은 네이티브 PE(x64) 구성이다. Outlook 설치본에는 .NET 구성요소(`Microsoft.Internal.Mime.dll`, `Microsoft.Internal.EmailMessage.dll`, `Microsoft.Exchange.Clients.SmimeAX.dll`)가 포함돼 있다. UI와 데이터는 둘 다 WebView2(EBWebView) 프로필에 있다.
