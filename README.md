# m365bak — Teams/Outlook 로컬 백업

새 Teams와 새 Outlook(olk)이 노트북에 저장해 둔 **IndexedDB 캐시의 복사본**을 읽어,
대화별 파일로 `%USERPROFILE%\M365-Archive`에 **누적** 보관한다.
네트워크·인증·Graph API·COM은 쓰지 않는다. 분석 근거는 [FINDINGS.md](FINDINGS.md), 설계는 [DESIGN.md](DESIGN.md)에 있다.

## 사용

```powershell
cd <클론한 폴더>\m365-local-backup
.\.venv\Scripts\python.exe -m m365bak run               # 안전 모드: 앱 종료 → 복사 → 재실행(통화 중이면 건너뜀)
.\.venv\Scripts\python.exe -m m365bak run --mode hot    # 핫 카피: 앱을 켠 채 복사(검증 통과, 스케줄이 이 모드)
.\.venv\Scripts\python.exe -m m365bak status            # 누적 건수, 최근 실행
.\.venv\Scripts\python.exe -m m365bak run --rerender    # 전사본 형식을 바꾼 뒤 전체 재생성
.\.venv\Scripts\python.exe -m m365bak verify-hot        # 핫 카피 ↔ 안전 모드 결과 대조(앱을 잠깐 닫음)
```

자동 실행은 작업 스케줄러 `M365 Local Backup`이 맡는다(매시간 + 로그온 3분 후, 핫 카피).
- 등록: `scripts\install-task.ps1 [-IntervalHours 2]`
- 해제: `scripts\uninstall-task.ps1`
- 로그: `M365-Archive\logs\m365bak.log`(건수만 기록하고 본문은 남기지 않음)

## 결과물

```
M365-Archive\
  index.md / index.json      전체 대화 목록(최근순)
  people.json                Teams 프로필(이름·메일·직함)
  archive.db                 증분 상태(버전·해시만, 본문 없음)
  teams\<날짜>_<대화명>_<id8>\     raw.jsonl · history.jsonl · conversation.json · thread.json · transcript.md
  outlook\<날짜>_<제목>_<id8>\     위와 같음 + attachments\
```

- `raw.jsonl`: 앱이 저장한 원본 레코드. 필드를 모두 보존한다.
- `thread.json`: LLM 처리용 정규화본. 발신자, 시각, 본문 Markdown, 첨부, 반응, 편집·삭제 여부가 들어 있다.
- `transcript.md`: YAML front-matter와 시간순 전사본. Outlook은 인용 반복을 빼고 새로 쓴 부분(UniqueBody)만 넣는다.
- 편집·삭제 전 버전은 `history.jsonl`에 남는다. 앱 캐시에서 사라진 메시지도 보관소에서는 지우지 않는다.

## 한계

- **로컬 캐시에 있는 것만** 백업된다. 앱이 최근 동기화하거나 열어 본 범위만 남고, Outlook은 주기적으로 캐시를 비운다.
  그래서 자주 돌려 쌓는 방식이다. 처음 실행하기 전의 과거분은 캐시에 남아 있던 만큼만 들어온다.
- 첨부를 가져오는 곳(모두 로컬, 네트워크 호출 없음):
  - **Outlook 파일 첨부**: `Olk\Attachments`. 앱이 내려받아 둔 원본이다(열어 보거나 미리 본 첨부).
  - **Outlook 본문 속 이미지·이미지 첨부**: IndexedDB `owa-image-data`. 원본이 없으면 썸네일을 저장하고 "썸네일만 캐시됨"으로 표시한다. 나중에 원본이 오면 교체한다.
  - **Teams 메시지 속 이미지**: WebView2 HTTP 캐시. 화면에서 본 적 있는 이미지만 있다.
  - **Teams 파일 중 내 OneDrive에 있는 것**: OneDrive 동기화 폴더에서 복사한다. 온라인 전용 파일은 읽는 순간 OneDrive 앱이 내려받는다.
- 위 어디에도 없는 첨부는 전사본에 "로컬 캐시에 없음/로컬 사본 없음"으로 표시하고 이름·크기·링크만 남긴다.
  다른 사람 OneDrive나 팀 사이트에 있는 Teams 파일이 여기에 해당한다(받으려면 인증 필요 → 제외).
  Outlook에서 첨부를 한 번 열어 두면 다음 실행 때 들어온다.
- 앱 업데이트로 IndexedDB 구조가 바뀌면 추출이 0건이 될 수 있다. `status`의 건수 변화와 로그를 확인한다.

## 설치(새 PC)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```
