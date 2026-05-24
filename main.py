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


def get_upcoming_events(session: requests.Session, sesskey: str) -> list:
    now = datetime.now(KST)
    payload = [
        {
            "index": 0,
            "methodname": "core_calendar_get_action_events_by_timesort",
            "args": {
                "timesortfrom": int(now.timestamp()),
                "timesortto": int((now + timedelta(days=7)).timestamp()),
                "limitnum": 100,
            },
        }
    ]

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

        if not result.get("error"):
            return result["data"]["events"]

        exc = result.get("exception", {})
        err_msg = exc.get("message") or exc.get("errorcode") or "unknown"
        print(f"AJAX API 실패 ({err_msg}), HTML 스크래핑으로 전환")
    except Exception as e:
        print(f"AJAX 요청 실패: {e}, HTML 스크래핑으로 전환")

    return _scrape_upcoming_events(session)


def _scrape_upcoming_events(session: requests.Session) -> list:
    resp = session.get(
        f"{LEARNUS_URL}/calendar/view.php",
        params={"view": "upcoming"},
        timeout=30,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    events = []

    for div in soup.find_all("div", class_="event"):
        starttime = div.get("data-event-starttime") or div.get("data-starttime")
        if not starttime:
            continue

        name_el = div.find("h3", class_="name") or div.find(class_="name")
        if not name_el:
            continue

        module = div.get("data-event-modulename") or div.get("data-modulename") or ""
        url_el = name_el.find("a") or div.find("a")

        course_el = div.find(class_="course-name") or div.find(attrs={"data-course-name": True})
        course_name = ""
        if course_el:
            course_name = course_el.get_text(strip=True) or course_el.get("data-course-name", "")

        events.append({
            "name": name_el.get_text(strip=True),
            "timesort": int(starttime),
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

        label = "🚨 *내일 마감!*" if days_left == 1 else f"⚠️ *{days_left}일 후 마감*"
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
    events = get_upcoming_events(session, sesskey)
    print(f"이벤트 {len(events)}개 조회")

    events_by_days: dict[int, list] = {1: [], 3: []}
    for event in events:
        deadline = datetime.fromtimestamp(event["timesort"], tz=KST)
        days_left = (deadline.date() - today).days
        if days_left in events_by_days:
            events_by_days[days_left].append(event)

    total = sum(len(v) for v in events_by_days.values())

    if total == 0:
        print("1일/3일 후 마감 항목 없음 — 알림 생략")
        return

    blocks = build_slack_blocks(events_by_days, today)
    send_slack(webhook_url, blocks)
    print(f"Slack 알림 전송 완료 ({total}건)")


if __name__ == "__main__":
    main()
