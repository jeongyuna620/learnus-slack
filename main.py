import os
import re
import sys
import requests
from datetime import datetime, timedelta
import pytz

KST = pytz.timezone("Asia/Seoul")
LEARNUS_URL = "https://ys.learnus.org"

MODULE_EMOJI = {
    "assign": "📝",
    "quiz":   "📊",
    "vod":    "🎥",
    "zoom":   "💻",
    "data":   "📋",
    "forum":  "💬",
}
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


# ──────────────────────────────────────────────
# 1단계: 모바일 앱 REST API 토큰 발급
# ──────────────────────────────────────────────

def get_ws_token(username: str, password: str) -> str:
    resp = requests.post(
        f"{LEARNUS_URL}/login/token.php",
        data={
            "username": username,
            "password": password,
            "service": "moodle_mobile_app",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "token" not in data:
        raise RuntimeError(f"토큰 발급 실패: {data.get('error', data)}")
    return data["token"]


def ws(token: str, function: str, **params):
    """Moodle REST API 호출."""
    resp = requests.get(
        f"{LEARNUS_URL}/webservice/rest/server.php",
        params={"wstoken": token, "wsfunction": function,
                "moodlewsrestformat": "json", **params},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "exception" in data:
        raise RuntimeError(f"[{function}] {data.get('message') or data.get('errorcode')}")
    return data


# ──────────────────────────────────────────────
# 2단계: 이벤트 수집
# ──────────────────────────────────────────────

def get_upcoming_events(token: str, userid: int) -> list:
    now = datetime.now(KST)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_from = int(today_start.timestamp())
    time_to   = int((today_start + timedelta(days=8)).timestamp())

    # 방법 A: 캘린더 이벤트 (과제·퀴즈·동영상 등 마감 전체)
    try:
        data = ws(token, "core_calendar_get_action_events_by_timesort",
                  timesortfrom=time_from, timesortto=time_to, limitnum=100)
        events = data.get("events", [])
        print(f"캘린더 이벤트 {len(events)}개 수신")
        if events:
            return events
    except RuntimeError as e:
        print(f"캘린더 API 실패: {e}")

    # 방법 B: 수강 과목별 과제 직접 조회
    print("과제 API로 전환")
    try:
        courses = ws(token, "core_enrol_get_users_courses", userid=userid)
        print(f"수강 과목 {len(courses)}개")
    except RuntimeError as e:
        print(f"과목 조회 실패: {e}")
        return []

    all_events = []
    course_map = {c["id"]: c["fullname"] for c in courses}

    try:
        assign_data = ws(token, "mod_assign_get_assignments",
                         **{f"courseids[{i}]": cid for i, cid in enumerate(course_map)})
        for course in assign_data.get("courses", []):
            cname = course_map.get(course["id"], "")
            for a in course.get("assignments", []):
                due = a.get("duedate", 0)
                if time_from <= due <= time_to:
                    all_events.append({
                        "name": a["name"],
                        "timesort": due,
                        "modulename": "assign",
                        "url": f"{LEARNUS_URL}/mod/assign/view.php?id={a['cmid']}",
                        "course": {"fullname": cname},
                    })
    except RuntimeError as e:
        print(f"과제 조회 실패: {e}")

    print(f"과제 이벤트 {len(all_events)}개 수신")
    return all_events


# ──────────────────────────────────────────────
# 3단계: Slack 전송
# ──────────────────────────────────────────────

def build_slack_blocks(events_by_days: dict, today) -> list:
    blocks = [{
        "type": "header",
        "text": {"type": "plain_text",
                 "text": f"📚 LearnUs 마감 알림  {today.strftime('%m/%d')}",
                 "emoji": True},
    }]

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
                block["accessory"] = {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "바로가기", "emoji": True},
                    "url": e["url"],
                }
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
        print("환경변수 누락: LEARNUS_USERNAME / LEARNUS_PASSWORD / SLACK_WEBHOOK_URL",
              file=sys.stderr)
        sys.exit(1)

    now   = datetime.now(KST)
    today = now.date()
    print(f"실행 시각: {now.strftime('%Y-%m-%d %H:%M KST')}")

    # 토큰 발급
    token = get_ws_token(username, password)
    print("토큰 발급 성공")

    # 사용자 ID 조회
    site_info = ws(token, "core_webservice_get_site_info")
    userid = site_info["userid"]
    print(f"사용자 ID: {userid}")

    # 이벤트 조회
    events = get_upcoming_events(token, userid)

    print(f"\n[전체 이벤트 - {len(events)}개]")
    for e in events:
        dl = datetime.fromtimestamp(e["timesort"], tz=KST)
        diff = (dl.date() - today).days
        print(f"  D{diff:+d} | {dl.strftime('%m/%d %H:%M')} | "
              f"{e.get('modulename','?'):8s} | {e['name']}")

    # 오전(~18시): 오늘·내일·3일 후 / 오후(18시~): 오늘만
    days_to_check = [0, 1, 3] if now.hour < 18 else [0]
    events_by_days: dict[int, list] = {d: [] for d in days_to_check}
    for e in events:
        dl = datetime.fromtimestamp(e["timesort"], tz=KST)
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
