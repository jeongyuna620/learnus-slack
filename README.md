# 📚 LearnUs Slack 마감 알림 봇

LearnUs(ys.learnus.org)의 **과제 및 동영상 강의 마감일**을 Slack으로 자동 알림해주는 봇입니다.
GitHub Actions로 동작하기 때문에 서버 없이 무료로 사용할 수 있어요.

## 알림 일정

| 시각 | 알림 대상 |
|------|-----------|
| 매일 오전 9시 | D-3, D-1, D-0 마감 과제 + 동영상 |
| 매일 오후 9시 | D-0 마감 (당일 마감 최종 알림) |

> GitHub Actions 특성상 정각 기준으로 최대 1시간까지 지연될 수 있습니다.

---

## 사용 방법

### 1단계 — 레포지토리 Fork

이 레포를 본인 GitHub 계정으로 **Fork** 하세요.

> 우측 상단 `Fork` 버튼 클릭 → `Create fork`

---

### 2단계 — Slack Incoming Webhook URL 발급

1. [api.slack.com/apps](https://api.slack.com/apps) 접속 → **Create New App**
2. **From scratch** 선택 → 앱 이름 입력 → 워크스페이스 선택
3. 좌측 메뉴 **Incoming Webhooks** → 토글 **On**
4. 하단 **Add New Webhook to Workspace** → 알림 받을 채널 선택
5. 생성된 `https://hooks.slack.com/services/...` URL 복사

---

### 3단계 — LearnUs 세션 쿠키 복사

LearnUs에 로그인한 직후 아래 방법으로 세션 값을 복사합니다.

1. [ys.learnus.org](https://ys.learnus.org) 접속 후 로그인
2. 로그인 직후 (대시보드가 완전히 로딩된 상태에서) **F12** 눌러 개발자 도구 열기
3. **Application** 탭 → 좌측 **Cookies** → `https://ys.learnus.org` 클릭
4. `MoodleSession` 항목 찾기 → **Value** 열의 값 복사

> ⚠️ 로그인 직후 바로 복사해야 세션이 유효합니다. 페이지를 여러 번 이동하거나 시간이 지나면 만료될 수 있습니다.

---

### 4단계 — GitHub Secrets 등록

Fork한 레포에서 **Settings → Secrets and variables → Actions → New repository secret**

| Secret 이름 | 값 |
|-------------|-----|
| `LEARNUS_SESSION` | 3단계에서 복사한 MoodleSession 값 |
| `SLACK_WEBHOOK_URL` | 2단계에서 발급한 Webhook URL |

---

### 5단계 — Actions 활성화 확인

1. Fork한 레포의 **Actions** 탭으로 이동
2. `"I understand my workflows..."` 경고가 뜨면 **Enable** 클릭
3. **LearnUs Deadline Notifier** 워크플로우 선택 → **Run workflow** 로 테스트 실행

Slack 채널에 알림이 오면 정상입니다 🎉

---

## 세션 만료 시

LearnUS 세션은 일정 시간이 지나면 만료됩니다.  
세션이 만료되면 Slack으로 아래와 같은 알림이 전송됩니다.

> ⚠️ **LearnUs 세션 만료** — MoodleSession 쿠키가 만료되었어요.

이 알림을 받으면 **3단계 → 4단계**를 반복해서 `LEARNUS_SESSION` Secret을 새 값으로 업데이트하면 됩니다.

> 세션 자동 유지를 위해 **LearnUs Session Keep-Alive** 워크플로우가 2시간마다 ping을 보냅니다.  
> 하지만 LearnUS 서버 정책에 따라 세션이 만료될 수 있으므로 만료 알림을 받으면 수동으로 갱신해주세요.

---

## 워크플로우 구조

```
.github/workflows/
├── notify.yml      # 매일 9시 / 21시 마감 알림 실행
└── keepalive.yml   # 매 2시간 세션 유지 ping
```

---

## 기술 스택

- Python 3.11
- GitHub Actions (무료 플랜으로 동작)
- Slack Incoming Webhooks
- LearnUS Moodle 3.5 (ys.learnus.org)

---

## 주요 기능

- **과제 / 퀴즈** 마감일 자동 수집 (Moodle 캘린더 API)
- **동영상(VOD) 강의** 마감일 수집 (강의 페이지 스크래핑)
- 이미 시청한 동영상은 알림 제외
- 여러 페이지에서 수강 과목 자동 탐지 (최대 5개 이상)
- 세션 만료 시 Slack 경고 알림
