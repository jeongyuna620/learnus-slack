import os
import re
import sys
import time
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
# Selenium으로 로그인 → MoodleSession 쿠키 획득
# ──────────────────────────────────────────────

def get_cookies_via_browser(username: str, password: str) -> dict:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")

    # Selenium Manager가 자동으로 드라이버 관리 (4.6+)
    driver = webdriver.Chrome(options=options)

    try:
        print(f"브라우저 로그인 시작: {LEARNUS_URL}/login.php")
        driver.get(f"{LEARNUS_URL}/login.php")

        wait = WebDriverWait(driver, 20)

        # 아이디 입력
        user_el = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "input[name='username'], input[id='username']")))
        user_el.clear()
        user_el.send_keys(username)

        # 비밀번호 입력
        pass_el = driver.find_element(
            By.CSS_SELECTOR, "input[name='password'], input[type='password']")
        pass_el.clear()
        pass_el.send_keys(password)

        # 로그인 버튼
        submit_el = driver.find_element(
            By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
        submit_el.click()

        # 로그인 완료 대기 (login URL에서 벗어날 때까지)
        wait.until(lambda d: "login" not in d.current_url.lower())
        time.sleep(2)

        print(f"로그인 후 URL: {driver.current_url}")

        # 쿠키 수집
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        print(f"획득한 쿠키: {list(cookies.keys())}")

        if "MoodleSession" not in cookies:
            raise RuntimeError("MoodleSession 쿠키 없음 — 로그인 실패")

        return cookies

    finally:
        driver.quit()


def build_session(cookies: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    })
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="ys.learnus.org")
    return session


# ──────────────────────────────────────────────
# sesskey 추출
# ──────────────────────────────────────────────

def get_sesskey(session: requests.Session) -> str:
    resp = session.get(f"{LEARNUS_URL}/my/", timeout=30)
    resp.raise_for_status()
    print(f"대시보드 URL: {resp.url}")

    match = re.search(r'"sesskey"\s*:\s*"([a-zA-Z0-9]+)"', resp.text)
    if match:
        return match.group(1)

    soup = BeautifulSoup(resp.text, "html.parser")
    el = soup.find("input", {"name": "sesskey"})
    if el:
        return el["value"]

    raise RuntimeError("sesskey를 찾을 수 없음")


# ──────────────────────────────────────────────
# 이벤트 조회 (AJAX → 과목 페이지 스크래핑 순)
# ──────────────────────────────────────────────

def get_upcoming_events(session: requests.Session, sesskey: str) -> list:
    now = datetime.now(KST)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_from = int(today_start.timestamp())
    time_to   = int((today_start + timedelta(days=8)).timestamp())

    # 방법 1: AJAX 캘린더 API
    try:
        payload = [{"index": 0,
                    "methodname": "core_calendar_get_action_events_by_timesort",
                    "args": {"timesortfrom": time_from, "timesortto": time_to,
                             "limitnum": 100}}]
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
            events = result["data"]["events"]
            print(f"AJAX 이벤트 {len(events)}개")
            return events
        exc = result.get("exception", {})
        print(f"AJAX 실패: {exc.get('message') or exc.get('errorcode')}")
    except Exception as e:
        print(f"AJAX 예외: {e}")

    # 방법 2: 과목 페이지 스크래핑
    return _scrape_course_events(session, today_start,
                                 today_start + timedelta(days=8))


def _get_course_ids(session: requests.Session) -> dict[str, str]:
    """course_id → course_name 딕셔너리 반환."""
    courses: dict[str, str] = {}

    for url, params in [
        (f"{LEARNUS_URL}/grade/overview/index.php", {}),
        (f"{LEARNUS_URL}/my/", {}),
    ]:
        try:
            resp = session.get(url, params=params, timeout=30)
            print(f"과목 수집 [{url}] → {resp.url}, 상태={resp.status_code}")
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=re.compile(r"course/view\.php")):
                m = re.search(r"id=(\d+)", a["href"])
                if m and int(m.group(1)) > 10:
                    courses[m.group(1)] = a.get_text(strip=True)
        except Exception as e:
            print(f"  실패: {e}")

    print(f"수강 과목 {len(courses)}개: {list(courses.items())[:5]}")
    return courses


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

    en_months = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
                 "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
                 "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
                 "sep":9,"oct":10,"nov":11,"dec":12}
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}),?\s+(\d{1,2}):(\d{2})(?:\s*(AM|PM))?",
                  text, re.IGNORECASE)
    if m:
        d, mon_s, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        h, mi, ampm = int(m.group(4)), int(m.group(5)), (m.group(6) or "").upper()
        mo = en_months.get(mon_s)
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
        print("수강 과목을 찾지 못했습니다.")
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
                        deadline = datetime.fromtimestamp(
                            int(ts_el.get("data-timedue") or ts_el.get("data-timestamp")), tz=KST)
                    else:
                        deadline = None
                        for cell in cells[1:]:
                            deadline = _parse_deadline(cell.get_text(strip=True))
                            if deadline:
                                break
                    if not deadline:
                        continue
                    dl = deadline if deadline.tzinfo else KST.localize(deadline)
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
    username    = os.environ.get("LEARNUS_USERNAME")
    password    = os.environ.get("LEARNUS_PASSWORD")
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not all([username, password, webhook_url]):
        print("환경변수 누락", file=sys.stderr)
        sys.exit(1)

    now   = datetime.now(KST)
    today = now.date()
    print(f"실행 시각: {now.strftime('%Y-%m-%d %H:%M KST')}")

    cookies = get_cookies_via_browser(username, password)
    session = build_session(cookies)
    print("세션 구성 완료")

    sesskey = get_sesskey(session)
    print(f"sesskey 획득: {sesskey[:8]}...")

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
