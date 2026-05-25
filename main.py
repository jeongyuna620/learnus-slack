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
    courses = _get_enrolled_courses(session, sesskey)
    vod_events = _get_vod_events(session, courses, dt_from, dt_to) if courses else []

    # AJAX 이벤트 URL 키 집합 (중복 방지)
    ajax_urls = {e.get("url", "") for e in ajax_events if e.get("url")}
    unique_vod = [e for e in vod_events if e.get("url", "") not in ajax_urls]

    all_events = ajax_events + unique_vod
    all_events.sort(key=lambda e: e["timesort"])
    return all_events


def _get_enrolled_courses(session: requests.Session, sesskey: str) -> dict[str, str]:
    """수강 과목 목록 반환 {course_id: fullname}.
    AJAX API 우선, 실패 시 HTML 스크래핑으로 폴백."""
    # 방법 A: Moodle AJAX — core_course_get_enrolled_courses_by_timeline_classification
    try:
        payload = [{"index": 0,
                    "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
                    "args": {"offset": 0, "limit": 0, "classification": "all",
                             "sort": "fullname", "customfieldname": "", "customfieldvalue": ""}}]
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
            courses = {str(c["id"]): c["fullname"]
                       for c in result["data"]["courses"]}
            print(f"수강 과목 {len(courses)}개 (AJAX): {list(courses.values())[:5]}")
            if courses:
                return courses
        exc = result.get("exception", {})
        print(f"수강 과목 AJAX 실패: {exc.get('message') or exc.get('errorcode')}")
    except Exception as e:
        print(f"수강 과목 AJAX 예외: {e}")

    # 방법 B: HTML 스크래핑 폴백
    return _get_course_ids(session)


def _get_course_ids(session: requests.Session) -> dict[str, str]:
    courses: dict[str, str] = {}
    for url in [
        f"{LEARNUS_URL}/grade/overview/index.php",
        f"{LEARNUS_URL}/my/",
        f"{LEARNUS_URL}/course/index.php",
    ]:
        try:
            resp = session.get(url, timeout=30)
            if "login" in resp.url.lower():
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=re.compile(r"course/view\.php")):
                m = re.search(r"id=(\d+)", a["href"])
                if m and int(m.group(1)) > 10:
                    courses[m.group(1)] = a.get_text(strip=True)
            # grade report 링크에서도 추출
            for a in soup.find_all("a", href=re.compile(r"grade/report")):
                m = re.search(r"id=(\d+)", a["href"])
                if m and int(m.group(1)) > 10:
                    name = a.get_text(strip=True)
                    if name:
                        courses[m.group(1)] = name
        except Exception as e:
            print(f"과목 수집 실패 [{url}]: {e}")
    print(f"수강 과목 {len(courses)}개: {list(courses.values())[:5]}")
    return courses


def _get_vod_events(session: requests.Session,
                    courses: dict[str, str],
                    time_from: datetime, time_to: datetime) -> list:
    """강의 페이지에서 동영상(vod/url 등) 마감일 수집."""
    events: list = []
    for cid, cname in courses.items():
        try:
            resp = session.get(f"{LEARNUS_URL}/course/view.php",
                               params={"id": cid}, timeout=20)
            if resp.status_code != 200 or "login" in resp.url.lower():
                continue
            soup = BeautifulSoup(resp.text, "html.parser")

            # 활동 목록에서 vod / 동영상 관련 요소 탐색
            for li in soup.find_all("li", class_=re.compile(r"modtype_")):
                mod_class = " ".join(li.get("class", []))
                # vod, url, resource 계열만
                if not any(k in mod_class for k in ("vod", "url", "resource", "ubboard")):
                    continue

                name_a = li.find("a", href=re.compile(r"/mod/"))
                if not name_a:
                    continue
                name = name_a.get_text(strip=True)

                # completionexpected / data-date 속성에서 마감일 추출
                deadline = None
                for el in li.find_all(True):
                    for attr in ("data-completionexpected", "data-timedue",
                                 "data-date", "data-timestamp"):
                        val = el.get(attr)
                        if val and re.fullmatch(r"\d{8,}", val):
                            deadline = datetime.fromtimestamp(int(val), tz=KST)
                            break
                    if deadline:
                        break

                # 텍스트에서도 날짜 파싱 시도
                if not deadline:
                    for span in li.find_all(["span", "div", "small"]):
                        txt = span.get_text(strip=True)
                        deadline = _parse_deadline(txt)
                        if deadline:
                            break

                if not deadline:
                    continue

                dl = deadline if deadline.tzinfo else KST.localize(deadline)
                if time_from <= dl <= time_to:
                    events.append({
                        "name": name,
                        "timesort": int(dl.timestamp()),
                        "modulename": "vod",
                        "url": name_a.get("href", ""),
                        "course": {"fullname": cname},
                    })
        except Exception as e:
            print(f"  [vod/{cid}] 오류: {e}")

    print(f"동영상 이벤트 {len(events)}개")
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
    for e in events:
        dl   = datetime.fromtimestamp(e["timesort"], tz=KST)
        diff = (dl.date() - today).days
        print(f"  D{diff:+d} | {dl.strftime('%m/%d %H:%M')} | "
              f"{e.get('modulename','?'):8s} | {e['name']}")

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
