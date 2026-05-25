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
    "mod_assign": "📝",
    "mod_quiz": "📊",
}

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


# ──────────────────────────────────────────────
# 로그인
# ──────────────────────────────────────────────

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

    raise RuntimeError("sesskey를 찾을 수 없습니다.")


# ──────────────────────────────────────────────
# 이벤트 조회 — 3단계 폴백
# ──────────────────────────────────────────────

def get_upcoming_events(session: requests.Session, sesskey: str) -> list:
    # 방법 1: AJAX (Referer 헤더 포함)
    print("방법 1: AJAX API 시도")
    events = _get_events_via_ajax(session, sesskey)
    if events is not None:
        print(f"  → {len(events)}개 이벤트 수신")
        return events

    # 방법 2: iCal 내보내기 (JS 불필요, 가장 안정적)
    print("방법 2: iCal 내보내기 시도")
    events = _get_events_via_ical(session)
    if events is not None:
        print(f"  → {len(events)}개 이벤트 수신")
        return events

    print("모든 방법 실패 — 이벤트 없음")
    return []


def _get_events_via_ajax(session: requests.Session, sesskey: str) -> list | None:
    now = datetime.now(KST)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    payload = [{
        "index": 0,
        "methodname": "core_calendar_get_action_events_by_timesort",
        "args": {
            "timesortfrom": int(today_start.timestamp()),
            "timesortto": int((today_start + timedelta(days=8)).timestamp()),
            "limitnum": 100,
        },
    }]
    try:
        resp = session.post(
            f"{LEARNUS_URL}/lib/ajax/service.php",
            params={"sesskey": sesskey},
            json=payload,
            headers={
                "Referer": f"{LEARNUS_URL}/my/",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data[0]
        if result.get("error"):
            exc = result.get("exception", {})
            msg = exc.get("message") or exc.get("errorcode") or str(result.get("error"))
            print(f"  AJAX 오류: {msg}")
            return None
        return result["data"]["events"]
    except Exception as e:
        print(f"  AJAX 예외: {e}")
        return None


def _get_events_via_ical(session: requests.Session) -> list | None:
    try:
        # export form에서 userid / authtoken 추출
        resp = session.get(f"{LEARNUS_URL}/calendar/export.php", timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        form = soup.find("form", action=re.compile(r"export_execute")) or soup.find("form")
        if not form:
            print("  iCal: export form 없음")
            return None

        form_data: dict = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                form_data[name] = inp.get("value", "")
        for sel in form.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            selected = sel.find("option", selected=True)
            form_data[name] = selected["value"] if selected else ""

        # 액션 이벤트(과제·퀴즈 등) + 최근/예정 기간
        if "preset_what" in form_data:
            form_data["preset_what"] = "actionevents"
        if "preset_time" in form_data:
            form_data["preset_time"] = "recentupcoming"

        action = form.get("action", f"{LEARNUS_URL}/calendar/export_execute.php")
        if not action.startswith("http"):
            action = LEARNUS_URL + action

        resp = session.post(action, data=form_data, timeout=30)
        resp.raise_for_status()

        body = resp.text
        if not body.strip().startswith("BEGIN:VCALENDAR"):
            print(f"  iCal: 예상치 못한 응답 (앞부분: {body[:80]!r})")
            return None

        return _parse_ical(body)
    except Exception as e:
        print(f"  iCal 예외: {e}")
        return None


def _parse_ical(ical_text: str) -> list:
    """VCALENDAR 텍스트를 파싱해 이벤트 리스트 반환."""
    # 긴 줄 이어붙이기 (RFC 5545 line folding)
    unfolded = re.sub(r"\r?\n[ \t]", "", ical_text)

    events: list = []
    current: dict = {}
    in_event = False

    for line in unfolded.splitlines():
        if line == "BEGIN:VEVENT":
            in_event = True
            current = {}
        elif line == "END:VEVENT":
            in_event = False
            if "timesort" in current and "name" in current:
                events.append(current)
        elif in_event and ":" in line:
            key_raw, _, val = line.partition(":")
            key = key_raw.split(";")[0].upper()

            if key == "DTSTART":
                val = val.strip()
                try:
                    if val.endswith("Z"):
                        dt = datetime.strptime(val, "%Y%m%dT%H%M%SZ")
                        dt = pytz.utc.localize(dt).astimezone(KST)
                    elif "T" in val:
                        dt = KST.localize(datetime.strptime(val[:15], "%Y%m%dT%H%M%S"))
                    else:
                        dt = KST.localize(datetime.strptime(val[:8], "%Y%m%d"))
                    current["timesort"] = int(dt.timestamp())
                except Exception as e:
                    print(f"  DTSTART 파싱 오류: {e} (값: {val})")

            elif key == "SUMMARY":
                # "Assignment: 제목" → "제목" 으로 정리
                name = val.strip()
                name = re.sub(r"^(Assignment|Quiz|Attendance|Forum)\s*:\s*", "", name, flags=re.IGNORECASE)
                current["name"] = name

            elif key == "URL":
                current["url"] = val.strip()

            elif key == "CATEGORIES":
                module = val.strip().lower().replace("mod_", "")
                current["modulename"] = module

            elif key == "DESCRIPTION":
                desc = val.replace("\\n", "\n").replace("\\,", ",").strip()
                current.setdefault("course", {"fullname": desc.split("\n")[0]})

    return events


# ──────────────────────────────────────────────
# Slack 전송
# ──────────────────────────────────────────────

def build_slack_blocks(events_by_days: dict, today) -> list:
    blocks = [{
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f"📚 LearnUs 마감 알림  {today.strftime('%m/%d')}",
            "emoji": True,
        },
    }]

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


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def main() -> None:
    username = os.environ.get("LEARNUS_USERNAME")
    password = os.environ.get("LEARNUS_PASSWORD")
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    if not all([username, password, webhook_url]):
        print("환경변수 누락: LEARNUS_USERNAME, LEARNUS_PASSWORD, SLACK_WEBHOOK_URL", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(KST)
    today = now.date()
    print(f"실행 시각: {now.strftime('%Y-%m-%d %H:%M KST')}")

    session = login(username, password)
    print("로그인 성공")

    sesskey = get_sesskey(session)
    print("sesskey 획득")

    events = get_upcoming_events(session, sesskey)

    # 전체 이벤트 디버그 출력
    print(f"\n[전체 이벤트 - {len(events)}개]")
    for e in events:
        dl = datetime.fromtimestamp(e["timesort"], tz=KST)
        days_left = (dl.date() - today).days
        print(f"  D{days_left:+d} | {dl.strftime('%m/%d %H:%M')} | {e.get('modulename','?'):10s} | {e['name']}")

    # 오전(~18시): 오늘·내일·3일 후 / 오후(18시~): 오늘만
    days_to_check = [0, 1, 3] if now.hour < 18 else [0]
    events_by_days: dict[int, list] = {d: [] for d in days_to_check}
    for event in events:
        dl = datetime.fromtimestamp(event["timesort"], tz=KST)
        days_left = (dl.date() - today).days
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
