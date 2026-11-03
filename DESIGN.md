# 설계안 — Teams/Outlook 로컬 백업 (`m365bak`)

근거: [FINDINGS.md](FINDINGS.md). 공식 Graph API와 COM은 쓰지 않는다. 네트워크와 인증도 사용하지 않는다.
앱이 로컬에 저장한 IndexedDB 복사본만 읽는다.

## 1. 파이프라인

```
① 스냅샷   두 앱 종료 → IndexedDB(leveldb+blob)와 Olk 첨부를 임시폴더로 복사 → 두 앱 재실행 (~15초)
② 파싱     ccl_chromium_reader로 화이트리스트 store만 읽기. 키별 최신 버전 + 구버전에서 빠진 메시지 복구
③ 병합     archive.db(상태)와 비교해 신규·변경분만 반영. 캐시에서 사라진 것은 지우지 않음(누적 보관)
④ 출력     변경된 대화만 messages.jsonl / conversation.json / transcript.html 재생성, index.html 갱신
⑤ 정리     임시 스냅샷 삭제(finally). 화이트리스트 밖 데이터는 디스크에 남기지 않음
```

## 2. 읽는 범위(화이트리스트)

- Teams: `replychain-manager`/`replychains`, `replychains-2`, `conversation-manager`/`conversations`, `profiles`/`profiles`(이름·메일·UPN·직함·부서 필드만)
- Outlook: `owa-offline-data-*`/`conversations`, `conversationNodes`, `messageBodies`, `messages`, `folders`, `owa-attachment-data-*`/`attachments`, `Olk/Attachments/ooa-*`
- **읽지 않는 것**: `auth*` DB, `syncstates`(syncToken), Local Storage, Cookies, Login Data. 토큰과 비밀번호는 저장하지 않는다.

## 3. 출력 구조

결정(2026-10-29): 전사본은 HTML 대신 **나중에 LLM이 처리하기 좋은 Markdown + JSON**으로 만든다.

```
%USERPROFILE%\M365-Archive\
  index.md / index.json            전체 대화 목록(최근순)
  archive.db                       상태·색인(SQLite, 이 프로그램이 소유)
  teams\<최초날짜>_<대화명>_<id8>\
      conversation.json            대화 메타 원본(최신본)
      raw.jsonl                    메시지 원본 1줄 = 1건, 전 필드 보존, 시간순
      history.jsonl                편집·삭제 전 버전
      thread.json                  정규화본: [{id, time, sender, sender_email, text(md), reply_to, attachments, reactions, edited, deleted}]
      transcript.md                YAML front-matter(대화 메타) + 시간순 Markdown 전사본
  outlook\<최초날짜>_<제목>_<id8>\
      conversation.json            ConversationType + conversationNodes 원본
      raw.jsonl                    messageBodies 원본(메일 1건 = 1줄)
      history.jsonl
      thread.json
      transcript.md                본문은 UniqueBody(새로 쓴 부분) 우선 → 인용 반복 제거
      attachments\<메일id8>\<원래파일명>
```

- 폴더명은 처음 만들 때 고정하고, `archive.db`에 대화 ID와 매핑한다. 대화명이 바뀌어도 폴더가 중복되지 않는다.
- 파일은 `tmp`에 쓴 뒤 rename하는 원자적 쓰기를 쓴다.

## 4. 증분 갱신

`archive.db`의 테이블:
- `items(source, conv_id, item_id, version, sha256, first_seen, last_seen, deleted_at)`
- `conversations(source, conv_id, folder, title, last_time, count)`
- `attachments(item_id, att_id, sha256, path, size)`
- `runs(...)`

| 경우 | 처리 |
|---|---|
| 처음 보는 키 | 추가 |
| Teams `version`이 더 큼 / Outlook `ChangeKey`나 `LastModifiedTime`이 바뀜 | 최신본으로 교체하고 이전 본은 history에 보존 |
| 같은 버전·같은 해시 | skip |
| 캐시에서 사라짐 | 유지(삭제하지 않음) |
| 원본에서 삭제 표시(`deletetime`/`deletionInfo`) | 기존 내용은 유지하고 `deleted_at`만 표시 |
| Outlook `IsTruncated=true` | 잘림을 표시하고, 나중에 전체 본문이 오면 교체 |

- Outlook에서 같은 메일이 폴더 이동으로 ItemId가 바뀌는 경우는 `InternetMessageId`로 보조 중복 제거한다.

## 5. 첨부

- **Outlook**: 디렉터리 `sha256(attachmentId)` 또는 `sha256(base64url(attachmentId))`로 매칭하고, 안 되면 `(fileName, size)`로 맞춘다. 원래 파일명으로 복사하고 내용 sha256으로 중복을 제거한다. 로컬 캐시에 없는 첨부는 전사본에 "로컬 캐시에 없음(이름, 크기)"으로 표시한다.
- **Outlook 이미지**: `owa-image-data-<guid>/images`에 `{attachmentId, image: Blob, thumbnailType}`이 있다. 실측으로 `thumbnailType 0`은 원본(평균 150KB), `2`는 썸네일(평균 5KB)이었다. 같은 첨부의 가장 큰 사본을 저장하고, 썸네일이면 `thumb=1`로 기록해 원본이 오면 교체한다.
- **Teams 이미지**: 메시지 HTML의 `<img src=…/objects/<id>/views/imgo>`가 가리키는 AMS object id를 WebView2 HTTP 캐시(블록 파일 형식)의 키 `…/objects/<id>/views/<view>`와 맞춘다. view는 `imgpsh_fullsize*` → `imgo` → 썸네일 순으로 고른다. 스냅샷 때는 `index`와 `data_0~3`(약 77MB)만 복사하고, 본문이 든 `f_*` 파일은 필요한 것만 처리 시점에 공유 모드로 복사한다. 시그니처가 이미지인 것만 저장한다.
- **Teams 파일**: `HKCU\Software\SyncEngines\Providers\OneDrive`의 `UrlNamespace → MountPoint` 매핑으로 파일 URL을 로컬 경로로 바꾼다. 경로가 있으면 복사한다. 이 PC에 동기화된 라이브러리(내 OneDrive 등)만 해당한다.
- 남의 OneDrive나 팀 사이트 파일은 링크만 기록한다(인증·API가 필요해 제약 위반).
- 2026-10-29 적용 결과: 보관 첨부가 30개(9.8MB)에서 418개(178MB)로 늘었다. 내역은 Outlook 이미지 149, Teams 이미지 143, Teams 파일 15다.

## 6. 전사본(Markdown)과 정규화 JSON

- 본문 HTML은 Markdown으로 변환한다(`html2text`). 링크는 보존하고, 원격 이미지는 alt 텍스트만 남긴다.
- Teams: 날짜 구분 헤더, `**발신자** · 시각(KST)`, 채널은 답글 체인으로 묶는다. 반응·편집됨·삭제됨 표시, 시스템 이벤트는 `> [시스템]` 줄, 파일은 링크 목록으로 넣는다.
- Outlook: 메일마다 From/To/Cc/날짜 헤더와 본문을 넣는다. 본문은 UniqueBody를 우선하고 없으면 NormalizedBody를 쓴다. 첨부는 상대경로 링크로 단다. 본문이 로컬에 없는 메일(conversationNodes에만 있음)도 "본문 미캐시"로 목록에 남긴다.

## 7. 실행

- `m365bak run`: 기본은 안전 모드(종료 → 복사 → 재실행)
- `--mode hot`: 핫 카피 모드. 공유 플래그(READ|WRITE|DELETE)로 열어 복사하고, 복사 전후 CURRENT와 .ldb 집합이 같아야 채택한다.
  **2026-10-29 `verify-hot` 통과**: 핫 카피와 안전 모드 결과가 Teams 5,325 = 5,325, Outlook 222 = 222로 같았고, 차이·버전 차이·해독 불가가 모두 0이었다.
- `m365bak status`: 누적 건수와 마지막 실행 결과를 보여준다.
- 스케줄: 작업 스케줄러 `M365 Local Backup`. **매시간 + 로그온 3분 후, 핫 카피 모드**로 실행한다(`scripts/install-task.ps1`).
  처음에는 2~3시간 간격을 권장했지만, 실측에서 Outlook이 1시간 안에 본문 4건을 캐시에서 지웠고 같은 시간 Teams도 6건을 비웠다. 그래서 1시간으로 정했다.
- 안전 모드는 Teams 통화·회의 중이면 건너뛴다(종료 코드 3). 판정은 Windows 마이크·카메라 사용 기록
  (`CapabilityAccessManager\ConsentStore\{microphone,webcam}\MSTeams_8wekyb3d8bbwe`의 `LastUsedTimeStop=0`)으로 한다.
- 과거 스냅샷을 다시 넣어도 안전하다. 레코드는 Teams `version`, Outlook `LastModifiedTime`이 더 오래되면 반영하지 않는다(메타도 같다).

## 8. 구현

- Python 3.12 + `ccl_chromium_reader` 하나만 의존한다(venv). 모듈 구성:
  - `snapshot.py`
  - `teams.py`
  - `outlook.py`
  - `store.py` (archive.db)
  - `render.py`
  - `cli.py`
- 테스트: 이번 스냅샷으로 골든 테스트를 만든다. 건수 4,903+395 / 221과 대조하고, 두 번째 실행에서 변경이 0건인지 확인한다.
