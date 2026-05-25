import os
import re
import sys
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import pytz

KST = pytz.timezone("Asia/Seoul")
LEARNUS_URL = "https://ys.learnus.org"

MODULE_EMOJI = {
    "assign": "📝", "quiz": "📊", "vod": "🎥",
    "zoom": "💻", "data": "📋", "forum": "💬",
}
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


# ──────────────────────────────────────────────
# 세션 구성 (저장된 쿠키 사용)
# ──────────────────────────────────────────────

def build_session(moodle_session: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    })
    session.cookies.set("MoodleSession", moodle_session, domain="ys.learnus.org")
    return session


def check_session(session: requests.Session) -> bool:
    """세션이 유효한지 확인 (login 페이지로 리다이렉트되면 만료)."""
    resp = session.get(f"{LEARNUS_URL}/my/", timeout=30)
    print(f"세션 확인 → 최종 URL: {resp.url}")
    if "login" in resp.url.lower():
        return False
    return True


# ──────────────────────────────────────────────
# sesskey 추출
# ──────────────────────────────────────────────

def get_sesskey(session: requests.Session) -> str:
    resp = session.get(f"{LEARNUS_URL}/my/", timeout=30)
    match = re.search(r'"sesskey"\s*:\s*"([a-zA-Z0-9]+)"', resp.text)
    if match:
        return match.group(1)
    soup = BeautifulSoup(resp.text, "html.parser")
    el = soup.find("input", {"name": "sesskey"})
    if el:
        return el["value"]
    raise RuntimeError("sesskey를 찾을 수 없음")


# ──────────────────────────────────────────────
# 이벤트 조회
# ──────────────────────────────────────────────

def get_upcoming_events(session: requests.Session, sesskey: str) -> list:
    now = datetime.now(KST)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    dt_from = today_start
    dt_to   = today_start + timedelta(days=8)
    time_from = int(dt_from.timestamp())
    time_to   = int(dt_to.timestamp())

    ajax_events: list = []

    # 방법 1: AJAX 캘린더 API (과제/퀴즈 등)
    try:
        payload = [{"index": 0,
                    "methodname": "core_calendar_get_action_events_by_timesort",
                    "args": {"timesortfrom": time_from, "timesortto": time_to,
                             "limitnum": 50}}]
        resp = session.post(
            f"{LEARNUS_URL}/lib/ajax/service.php",
            params={"sesskey": sesskey},
            json=payload,
            headers={"Referer": f"{LEARNUS_URL}/my/",
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=30,
        )
        data = resp.json()
        result = data[0]
        if not result.get("error"):
            ajax_events = result["data"]["events"]
            print(f"AJAX 이벤트 {len(ajax_events)}개")
        else:
            exc = result.get("exception", {})
            print(f"AJAX 실패: {exc.get('message') or exc.get('errorcode')}")
            # AJAX 실패 시 스크래핑으로 과제/퀴즈 수집
            ajax_events = _scrape_course_events(session, dt_from, dt_to)
    except Exception as e:
        print(f"AJAX 예외: {e}")
        ajax_events = _scrape_course_events(session, dt_from, dt_to)

    # 방법 2: 강의 페이지 스크래핑 — 동영상(VOD) 마감일 추가
    courses = _get_enrolled_courses(session, sesskey, ajax_events)
    completed = _get_completed_cmids(session, sesskey, list(courses.keys()))
    vod_events = _get_vod_events(session, courses, dt_from, dt_to,
                                  completed) if courses else []

    # AJAX 이벤트 URL 키 집합 (중복 방지)
    ajax_urls = {e.get("url", "") for e in ajax_events if e.get("url")}
    unique_vod = [e for e in vod_events if e.get("url", "") not in ajax_urls]

    all_events = ajax_events + unique_vod
    all_events.sort(key=lambda e: e["timesort"])
    return all_events


_FAKE_COURSE_NAMES = {"grades overview", "moodle", "home", "calendar", "site"}

def _discover_courses_from_calendar(session: requests.Session,
                                    sesskey: str) -> dict[str, str]:
    """학기 전체 기간(오늘 기준 ±6개월)의 캘린더 이벤트에서 과목 ID 수집."""
    now = datetime.now(KST)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    t_from = int((today - timedelta(days=90)).timestamp())
    t_to   = int((today + timedelta(days=180)).timestamp())
    try:
        payload = [{"index": 0,
                    "methodname": "core_calendar_get_action_events_by_timesort",
                    "args": {"timesortfrom": t_from, "timesortto": t_to,
                             "limitnum": 50}}]
        resp = session.post(
            f"{LEARNUS_URL}/lib/ajax/service.php",
            params={"sesskey": sesskey},
            json=payload,
            headers={"Referer": f"{LEARNUS_URL}/my/",
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=30,
        )
        data = resp.json()
        result = data[0]
        if result.get("error"):
            exc = result.get("exception", {})
            print(f"학기 캘린더 실패: {exc.get('message') or exc.get('errorcode')}")
            return {}
        courses: dict[str, str] = {}
        for ev in result["data"]["events"]:
            c = ev.get("course", {})
            cid = str(c.get("id", ""))
            cname = c.get("fullname", "")
            if cid and cid.isdigit() and int(cid) > 10 and cname:
                courses[cid] = cname
        print(f"학기 캘린더 이벤트 {len(result['data']['events'])}개 → 과목 {len(courses)}개 발견")
        return courses
    except Exception as e:
        print(f"학기 캘린더 예외: {e}")
        return {}


def _get_courses_from_grades(session: requests.Session,
                             sesskey: str) -> dict[str, str]:
    """gradereport_overview_get_course_grades AJAX로 수강 과목 수집."""
    try:
        payload = [{"index": 0,
                    "methodname": "gradereport_overview_get_course_grades",
                    "args": {"userid": 0}}]
        resp = session.post(
            f"{LEARNUS_URL}/lib/ajax/service.php",
            params={"sesskey": sesskey},
            json=payload,
            headers={"Referer": f"{LEARNUS_URL}/my/",
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=30,
        )
        data = resp.json()
        if not isinstance(data, list) or data[0].get("error"):
            err = (data[0].get("exception", {}) if isinstance(data, list) and data
                   else data)
            print(f"성적 AJAX 실패: {err}")
            return {}
        grades = data[0].get("data", {}).get("grades", [])
        courses = {str(g["courseid"]): g.get("coursefullname", f"Course {g['courseid']}")
                   for g in grades if g.get("courseid") and int(g["courseid"]) > 10}
        print(f"성적 AJAX → 과목 {len(courses)}개: {list(courses.values())[:8]}")
        return courses
    except Exception as e:
        print(f"성적 AJAX 예외: {e}")
        return {}


def _get_enrolled_courses(session: requests.Session, sesskey: str,
                          ajax_events: list | None = None) -> dict[str, str]:
    """수강 과목 목록 반환 {course_id: fullname}."""
    # ── 방법 A: 학기 전체 캘린더로 과목 ID 수집 ─────────────────
    courses = _discover_courses_from_calendar(session, sesskey)

    # ── 방법 A2: 성적 개요 AJAX (캘린더에 없는 과목 보완) ─────────
    for cid, cname in _get_courses_from_grades(session, sesskey).items():
        courses.setdefault(cid, cname)

    # ── 방법 B: HTML 스크래핑 보완 ───────────────────────────────
    for cid, cname in _get_course_ids(session).items():
        if cid not in courses and cname.lower() not in _FAKE_COURSE_NAMES:
            courses[cid] = cname

    # ── 방법 C: 직전 AJAX 이벤트에서 보완 ───────────────────────
    if ajax_events:
        for ev in ajax_events:
            c = ev.get("course", {})
            cid = str(c.get("id", ""))
            cname = c.get("fullname", "")
            if cid and cid.isdigit() and int(cid) > 10 and cname:
                courses.setdefault(cid, cname)

    print(f"수강 과목 합계 {len(courses)}개: {list(courses.values())[:8]}")
    return courses


def _get_course_ids(session: requests.Session) -> dict[str, str]:
    """여러 페이지에서 course/view.php 링크를 수집해 {course_id: name} 반환."""
    courses: dict[str, str] = {}
    pages = [
        f"{LEARNUS_URL}/grade/report/overview/index.php",  # 성적 개요 (서버사이드)
        f"{LEARNUS_URL}/user/profile.php",                 # 내 프로필 (수강 과목 목록)
        f"{LEARNUS_URL}/grade/overview/index.php",
        f"{LEARNUS_URL}/my/",
    ]
    for url in pages:
        try:
            resp = session.get(url, timeout=30)
            if "login" in resp.url.lower():
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            before = len(courses)
            for a in soup.find_all("a", href=re.compile(r"course/view\.php")):
                m = re.search(r"id=(\d+)", a["href"])
                if m and int(m.group(1)) > 10:
                    name = a.get_text(strip=True)
                    if name:
                        courses[m.group(1)] = name
            # grade/report/user 링크: 텍스트가 실제 과목명
            for a in soup.find_all("a", href=re.compile(r"grade/report/user")):
                m = re.search(r"\bid=(\d+)", a["href"])
                if m and int(m.group(1)) > 10:
                    name = a.get_text(strip=True)
                    if name and name.lower() not in _FAKE_COURSE_NAMES:
                        courses.setdefault(m.group(1), name)
            # course/user.php?mode=grade&id=X 링크 (LearnUS 성적 개요 형식)
            for a in soup.find_all("a", href=re.compile(r"course/user\.php")):
                if "mode=grade" not in a.get("href", ""):
                    continue
                m = re.search(r"\bid=(\d+)", a["href"])
                if m and int(m.group(1)) > 10:
                    name = a.get_text(strip=True)
                    if name and name.lower() not in _FAKE_COURSE_NAMES:
                        courses.setdefault(m.group(1), name)
            added = len(courses) - before
        except Exception as e:
            print(f"  과목 스크래핑 실패 [{url}]: {e}")
    return courses


def _extract_deadline_from_li(li) -> "datetime | None":
    """li 요소에서 마감일 추출 (속성 → 텍스트 순)."""
    for el in li.find_all(True):
        for attr in ("data-completionexpected", "data-timedue",
                     "data-duedate", "data-date", "data-timestamp"):
            val = el.get(attr)
            if val and re.fullmatch(r"\d{8,}", str(val)):
                return datetime.fromtimestamp(int(val), tz=KST)
    # 텍스트 파싱 (span/div/small/p 전체)
    for el in li.find_all(["span", "div", "small", "p", "td"]):
        dl = _parse_deadline(el.get_text(strip=True))
        if dl:
            return dl
    return None


def _extract_vod_deadline(text: str,
                           time_from: datetime,
                           time_to: datetime) -> "tuple[datetime | None, str]":
    """LearnUS VOD 텍스트에서 정규 마감일만 파싱.

    형식: YYYY-MM-DD HH:MM:SS ~ YYYY-MM-DD HH:MM:SS
    → 범위 끝(~) 날짜를 정규 마감으로 사용.
    지각 기한은 의도적으로 무시 (이미 시청한 영상의 지각 기한 오알림 방지).
    """
    m = re.search(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*~\s*"
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", text)
    if m:
        try:
            dl = KST.localize(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            if time_from <= dl <= time_to:
                return dl, ""
        except ValueError:
            pass
    return None, ""


def _get_completed_cmids(session: requests.Session,
                         sesskey: str,
                         course_ids: list[str]) -> set[int]:
    """core_completion_get_activities_completion_status로 완료된 활동 CMID 수집.
    API 실패 시 빈 셋 반환 (필터링 없이 전체 표시)."""
    completed: set[int] = set()
    for cid in course_ids:
        try:
            payload = [{"index": 0,
                        "methodname": "core_completion_get_activities_completion_status",
                        "args": {"courseid": int(cid)}}]
            resp = session.post(
                f"{LEARNUS_URL}/lib/ajax/service.php",
                params={"sesskey": sesskey},
                json=payload,
                headers={"Referer": f"{LEARNUS_URL}/my/",
                         "X-Requested-With": "XMLHttpRequest"},
                timeout=30,
            )
            data = resp.json()
            if not isinstance(data, list) or data[0].get("error"):
                break  # API 미지원 → 루프 중단
            for stat in data[0].get("data", {}).get("statuses", []):
                if stat.get("state", 0) >= 1:  # 1=완료, 2=통과
                    completed.add(stat["cmid"])
        except Exception:
            break
    if completed:
        print(f"완료된 활동 {len(completed)}개 (VOD 알림 제외)")
    return completed


def _get_vod_events(session: requests.Session,
                    courses: dict[str, str],
                    time_from: datetime, time_to: datetime,
                    completed_cmids: "set[int] | None" = None) -> list:
    """강의 페이지(modtype_vod)에서 동영상 마감일 수집."""
    events: list = []
    seen_urls: set = set()

    for cid, cname in courses.items():
        found = 0
        try:
            resp = session.get(f"{LEARNUS_URL}/course/view.php",
                               params={"id": cid}, timeout=20)
            if resp.status_code != 200 or "login" in resp.url.lower():
                continue
            soup = BeautifulSoup(resp.text, "html.parser")

            for li in soup.find_all("li", class_=re.compile(r"modtype_vod")):
                name_a = li.find("a", href=re.compile(r"/mod/"))
                if not name_a:
                    continue
                url = name_a.get("href", "")
                if url in seen_urls:
                    continue
                # 이미 시청 완료된 VOD 제외
                if completed_cmids:
                    m_cmid = re.search(r"\bid=(\d+)", url)
                    if m_cmid and int(m_cmid.group(1)) in completed_cmids:
                        continue

                text = li.get_text(separator="|", strip=True)
                dl, suffix = _extract_vod_deadline(text, time_from, time_to)
                if not dl:
                    continue

                seen_urls.add(url)
                events.append({
                    "name": name_a.get_text(strip=True) + suffix,
                    "timesort": int(dl.timestamp()),
                    "modulename": "vod",
                    "url": url,
                    "course": {"fullname": cname},
                })
                found += 1

        except Exception as e:
            print(f"  [{cname}] VOD 수집 오류: {e}")

        if found:
            print(f"  [{cname}] 동영상 {found}개")

    print(f"동영상 이벤트 합계 {len(events)}개")
    return events


def _parse_deadline(text: str) -> datetime | None:
    text = text.strip()
    if not text or text in ("-", "마감일 없음", "No due date"):
        return None
    if re.fullmatch(r"\d{10,}", text):
        return datetime.fromtimestamp(int(text), tz=KST)
    m = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일"
                  r"(?:[^0-9]*)?(오전|오후)?\s*(\d{1,2}):(\d{2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ampm, h, mi = m.group(4), int(m.group(5)), int(m.group(6))
        if ampm == "오후" and h != 12: h += 12
        elif ampm == "오전" and h == 12: h = 0
        try:
            return KST.localize(datetime(y, mo, d, h, mi))
        except ValueError:
            pass
    en = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
          "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
          "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
          "sep":9,"oct":10,"nov":11,"dec":12}
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}),?\s+(\d{1,2}):(\d{2})(?:\s*(AM|PM))?",
                  text, re.IGNORECASE)
    if m:
        d, mon_s, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        h, mi, ampm = int(m.group(4)), int(m.group(5)), (m.group(6) or "").upper()
        mo = en.get(mon_s)
        if mo:
            if ampm == "PM" and h != 12: h += 12
            elif ampm == "AM" and h == 12: h = 0
            try:
                return KST.localize(datetime(y, mo, d, h, mi))
            except ValueError:
                pass
    return None


def _scrape_course_events(session: requests.Session,
                          time_from: datetime, time_to: datetime) -> list:
    courses = _get_course_ids(session)
    if not courses:
        return []
    events: list = []
    for cid, cname in courses.items():
        for mod in ["assign", "quiz"]:
            try:
                resp = session.get(f"{LEARNUS_URL}/mod/{mod}/index.php",
                                   params={"id": cid}, timeout=20)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                for row in soup.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) < 2:
                        continue
                    name_a = cells[0].find("a")
                    if not name_a:
                        continue
                    ts_el = (row.find(attrs={"data-timedue": True})
                             or row.find(attrs={"data-timestamp": True}))
                    if ts_el:
                        dl = datetime.fromtimestamp(
                            int(ts_el.get("data-timedue") or ts_el.get("data-timestamp")), tz=KST)
                    else:
                        dl = None
                        for cell in cells[1:]:
                            dl = _parse_deadline(cell.get_text(strip=True))
                            if dl:
                                break
                    if not dl:
                        continue
                    dl = dl if dl.tzinfo else KST.localize(dl)
                    if time_from <= dl <= time_to:
                        events.append({
                            "name": name_a.get_text(strip=True),
                            "timesort": int(dl.timestamp()),
                            "modulename": mod,
                            "url": name_a.get("href", ""),
                            "course": {"fullname": cname},
                        })
            except Exception as e:
                print(f"  [{cname}/{mod}] 오류: {e}")
    print(f"스크래핑 이벤트 {len(events)}개")
    return events


# ──────────────────────────────────────────────
# Slack 전송
# ──────────────────────────────────────────────

def send_session_expired_alert(webhook_url: str) -> None:
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "⚠️ LearnUs 세션 만료", "emoji": True}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": "MoodleSession 쿠키가 만료되었어요.\n"
                          "LearnUs에 로그인 후 쿠키를 갱신해주세요.\n\n"
                          "*갱신 방법:* F12 → Application → Cookies → `MoodleSession` 값 복사 "
                          "→ GitHub Secret `LEARNUS_SESSION` 업데이트"}},
    ]
    requests.post(webhook_url, json={"blocks": blocks}, timeout=10)


def build_slack_blocks(events_by_days: dict, today) -> list:
    blocks = [{"type": "header",
               "text": {"type": "plain_text",
                        "text": f"📚 LearnUs 마감 알림  {today.strftime('%m/%d')}",
                        "emoji": True}}]
    for days_left in sorted(events_by_days.keys()):
        evts = events_by_days[days_left]
        if not evts:
            continue
        label = {0: "🔥 *오늘 마감!*", 1: "🚨 *내일 마감!*"}.get(
            days_left, f"⚠️ *{days_left}일 후 마감*")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": label}})
        for e in evts:
            dl = datetime.fromtimestamp(e["timesort"], tz=KST)
            wd = WEEKDAY_KO[dl.weekday()]
            emoji = MODULE_EMOJI.get(e.get("modulename", ""), "📌")
            course = e.get("course", {}).get("fullname", "")
            lines = [f"{emoji} *{e['name']}*"]
            if course:
                lines.append(f"    과목: {course}")
            lines.append(f"    마감: {dl.strftime('%m/%d')}({wd}) {dl.strftime('%H:%M')}")
            block: dict = {"type": "section",
                           "text": {"type": "mrkdwn", "text": "\n".join(lines)}}
            if e.get("url"):
                block["accessory"] = {"type": "button",
                                      "text": {"type": "plain_text", "text": "바로가기"},
                                      "url": e["url"]}
            blocks.append(block)
        blocks.append({"type": "divider"})
    return blocks


def send_slack(webhook_url: str, blocks: list) -> None:
    requests.post(webhook_url, json={"blocks": blocks}, timeout=10).raise_for_status()


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def main() -> None:
    moodle_session = os.environ.get("LEARNUS_SESSION")
    webhook_url    = os.environ.get("SLACK_WEBHOOK_URL")

    if not all([moodle_session, webhook_url]):
        print("환경변수 누락: LEARNUS_SESSION / SLACK_WEBHOOK_URL", file=sys.stderr)
        sys.exit(1)

    now   = datetime.now(KST)
    today = now.date()
    print(f"실행 시각: {now.strftime('%Y-%m-%d %H:%M KST')}")

    session = build_session(moodle_session)

    if not check_session(session):
        print("세션 만료 — Slack 알림 전송")
        send_session_expired_alert(webhook_url)
        sys.exit(0)

    print("세션 유효")
    sesskey = get_sesskey(session)
    print(f"sesskey 획득")

    events = get_upcoming_events(session, sesskey)

    print(f"\n[전체 이벤트 - {len(events)}개]")
    for i, e in enumerate(events):
        dl   = datetime.fromtimestamp(e["timesort"], tz=KST)
        diff = (dl.date() - today).days
        print(f"  D{diff:+d} | {dl.strftime('%m/%d %H:%M')} | "
              f"{e.get('modulename','?'):8s} | {e['name']}")
        if i == 0:  # 첫 번째 이벤트의 전체 키 출력 (디버그)
            print(f"  [debug] 이벤트 키: {list(e.keys())}")
            print(f"  [debug] activityname={e.get('activityname')} | "
                  f"description={str(e.get('description',''))[:60]}")

    days_to_check = [0, 1, 3] if now.hour < 18 else [0]
    events_by_days: dict[int, list] = {d: [] for d in days_to_check}
    for e in events:
        dl   = datetime.fromtimestamp(e["timesort"], tz=KST)
        diff = (dl.date() - today).days
        if diff in events_by_days:
            events_by_days[diff].append(e)

    total = sum(len(v) for v in events_by_days.values())
    if total == 0:
        print("\n알림 대상 없음 — 생략")
        return

    send_slack(webhook_url, build_slack_blocks(events_by_days, today))
    print(f"\nSlack 알림 전송 완료 ({total}건)")


if __name__ == "__main__":
    main()
