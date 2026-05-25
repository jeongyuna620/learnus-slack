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
    "assign": "📝",
    "quiz": "📊",
    "vod": "🎥",
    "zoom": "💻",
    "attendance": "✅",
    "forum": "💬",
    "data": "📋",
}

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def login(username: str, password: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    })

    resp = session.get(f"{LEARNUS_URL}/login/index.php", timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    token_el = soup.find("input", {"name": "logintoken"})
    logintoken = token_el["value"] if token_el else ""

    resp = session.post(
        f"{LEARNUS_URL}/login/index.php",
        data={
            "username": username,
            "password": password,
            "logintoken": logintoken,
            "anchor": "",
        },
        timeout=30,
    )
    resp.raise_for_status()

    if "loginerrormessage" in resp.text:
        raise RuntimeError("로그인 실패: 아이디/비밀번호를 확인하세요")

    return session


def get_sesskey(session: requests.Session) -> str:
    resp = session.get(f"{LEARNUS_URL}/my/", timeout=30)
    resp.raise_for_status()

    match = re.search(r'"sesskey"\s*:\s*"([a-zA-Z0-9]+)"', resp.text)
    if match:
        return match.group(1)

    soup = BeautifulSoup(resp.text, "html.parser")
    el = soup.find("input", {"name": "sesskey"})
    if el:
        return el["value"]

    raise RuntimeError("sesskey를 찾을 수 없습니다. 로그인 상태를 확인하세요.")


def _call_ajax(session: requests.Session, sesskey: str, methodname: str, args: dict) -> dict | None:
    """Moodle AJAX 호출. 실패 시 None 반환."""
    payload = [{"index": 0, "methodname": methodname, "args": args}]
    try:
        resp = session.post(
            f"{LEARNUS_URL}/lib/ajax/service.php",
            params={"sesskey": sesskey},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data[0]
        if result.get("error"):
            exc = result.get("exception", {})
            msg = exc.get("message") or exc.get("errorcode") or str(result.get("error"))
            print(f"  AJAX [{methodname}] 실패: {msg}")
            return None
        return result.get("data")
    except Exception as e:
        print(f"  AJAX [{methodname}] 예외: {e}")
        return None


def get_upcoming_events(session: requests.Session, sesskey: str) -> list:
    now = datetime.now(KST)
    # 오늘 자정(00:00 KST)부터 7일 후까지 — 오늘 마감 항목 누락 방지
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_from = int(today_start.timestamp())
    time_to = int((today_start + timedelta(days=8)).timestamp())

    # 방법 1: core_calendar_get_action_events_by_timesort
    print("방법 1: core_calendar_get_action_events_by_timesort 시도")
    data = _call_ajax(session, sesskey, "core_calendar_get_action_events_by_timesort", {
        "timesortfrom": time_from,
        "timesortto": time_to,
        "limitnum": 100,
    })
    if data is not None:
        events = data.get("events", [])
        print(f"  → {len(events)}개 이벤트 수신")
        return events

    # 방법 2: block_myoverview_get_action_events_by_timesort
    print("방법 2: block_myoverview_get_action_events_by_timesort 시도")
    data = _call_ajax(session, sesskey, "block_myoverview_get_action_events_by_timesort", {
        "timesortfrom": time_from,
        "timesortto": time_to,
        "limitnum": 100,
    })
    if data is not None:
        events = data.get("events", [])
        print(f"  → {len(events)}개 이벤트 수신")
        return events

    # 방법 3: HTML 스크래핑
    print("방법 3: HTML 스크래핑 시도")
    events = _scrape_upcoming_events(session)
    print(f"  → {len(events)}개 이벤트 수신")
    return events


def _scrape_upcoming_events(session: requests.Session) -> list:
    resp = session.get(
        f"{LEARNUS_URL}/calendar/view.php",
        params={"view": "upcoming"},
        timeout=30,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    events = []

    # Moodle 3.x / 4.x / LearnUs 커스텀 테마 모두 커버
    event_divs = (
        soup.find_all("div", class_="event")
        or soup.find_all("div", attrs={"data-event-id": True})
    )
    print(f"  HTML에서 .event 요소 {len(event_divs)}개 발견")

    for div in event_divs:
        # 타임스탬프
        starttime = (
            div.get("data-event-starttime")
            or div.get("data-starttime")
            or div.get("data-event-time")
        )
        if not starttime:
            # 자식 요소에서 찾기
            ts_el = div.find(attrs={"data-event-starttime": True}) or div.find(attrs={"data-starttime": True})
            if ts_el:
                starttime = ts_el.get("data-event-starttime") or ts_el.get("data-starttime")
        if not starttime:
            continue

        # 이벤트 이름
        name_el = (
            div.find("h3", class_="name")
            or div.find(class_="name")
            or div.find(class_="event-name")
            or div.find("a", class_="event-name")
        )
        if not name_el:
            continue

        module = (
            div.get("data-event-modulename")
            or div.get("data-modulename")
            or div.get("data-event-component", "").replace("mod_", "")
            or ""
        )

        url_el = name_el.find("a") if name_el.name != "a" else name_el
        if not url_el:
            url_el = div.find("a", href=re.compile(r"/mod/"))

        course_name = ""
        course_el = (
            div.find(class_="course-name")
            or div.find(class_="col-11 event-name-container")
            or div.find(attrs={"data-course-name": True})
        )
        if course_el:
            course_name = course_el.get("data-course-name") or course_el.get_text(strip=True)

        try:
            ts = int(starttime)
        except ValueError:
            continue

        events.append({
            "name": name_el.get_text(strip=True),
            "timesort": ts,
            "modulename": module,
            "url": url_el["href"] if url_el and url_el.get("href") else None,
            "course": {"fullname": course_name},
        })

    return events


def build_slack_blocks(events_by_days: dict, today) -> list:
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📚 LearnUs 마감 알림  {today.strftime('%m/%d')}",
                "emoji": True,
            },
        }
    ]

    for days_left in sorted(events_by_days.keys()):
        events = events_by_days[days_left]
        if not events:
            continue

        if days_left == 0:
            label = "🔥 *오늘 마감!*"
        elif days_left == 1:
            label = "🚨 *내일 마감!*"
        else:
            label = f"⚠️ *{days_left}일 후 마감*"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": label}})

        for event in events:
            deadline = datetime.fromtimestamp(event["timesort"], tz=KST)
            wd = WEEKDAY_KO[deadline.weekday()]
            emoji = MODULE_EMOJI.get(event.get("modulename", ""), "📌")
            course = event.get("course", {}).get("fullname", "")

            lines = [f"{emoji} *{event['name']}*"]
            if course:
                lines.append(f"    과목: {course}")
            lines.append(f"    마감: {deadline.strftime('%m/%d')}({wd}) {deadline.strftime('%H:%M')}")

            block: dict = {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
            url = event.get("url")
            if url:
                block["accessory"] = {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "바로가기", "emoji": True},
                    "url": url,
                }
            blocks.append(block)

        blocks.append({"type": "divider"})

    return blocks


def send_slack(webhook_url: str, blocks: list) -> None:
    resp = requests.post(webhook_url, json={"blocks": blocks}, timeout=10)
    resp.raise_for_status()


def main() -> None:
    username = os.environ.get("LEARNUS_USERNAME")
    password = os.environ.get("LEARNUS_PASSWORD")
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    if not all([username, password, webhook_url]):
        print(
            "환경변수 누락: LEARNUS_USERNAME, LEARNUS_PASSWORD, SLACK_WEBHOOK_URL 을 설정하세요",
            file=sys.stderr,
        )
        sys.exit(1)

    now = datetime.now(KST)
    today = now.date()
    print(f"실행 시각: {now.strftime('%Y-%m-%d %H:%M KST')}")

    session = login(username, password)
    print("로그인 성공")

    sesskey = get_sesskey(session)
    print("sesskey 획득")

    events = get_upcoming_events(session, sesskey)

    # 디버그: 조회된 전체 이벤트 출력
    print(f"\n[전체 이벤트 목록 - {len(events)}개]")
    for e in events:
        dl = datetime.fromtimestamp(e["timesort"], tz=KST)
        days_left = (dl.date() - today).days
        print(f"  D{days_left:+d} | {dl.strftime('%m/%d %H:%M')} | {e.get('modulename','?'):8s} | {e['name']}")

    # 오전: 오늘·내일·3일 후 / 오후: 오늘만
    days_to_check = [0, 1, 3] if now.hour < 18 else [0]
    events_by_days: dict[int, list] = {d: [] for d in days_to_check}
    for event in events:
        deadline = datetime.fromtimestamp(event["timesort"], tz=KST)
        days_left = (deadline.date() - today).days
        if days_left in events_by_days:
            events_by_days[days_left].append(event)

    total = sum(len(v) for v in events_by_days.values())

    if total == 0:
        print("\n알림 대상 없음 — 생략")
        return

    blocks = build_slack_blocks(events_by_days, today)
    send_slack(webhook_url, blocks)
    print(f"\nSlack 알림 전송 완료 ({total}건)")


if __name__ == "__main__":
    main()
