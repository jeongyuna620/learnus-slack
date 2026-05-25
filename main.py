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
    "quiz":   "📊",
    "vod":    "🎥",
    "zoom":   "💻",
    "data":   "📋",
    "forum":  "💬",
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
        ),
        "Accept-Language": "ko-KR,ko;q=0.9",
    })
    resp = session.get(f"{LEARNUS_URL}/login/index.php", timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    token_el = soup.find("input", {"name": "logintoken"})
    logintoken = token_el["value"] if token_el else ""

    resp = session.post(
        f"{LEARNUS_URL}/login/index.php",
        data={"username": username, "password": password,
              "logintoken": logintoken, "anchor": ""},
        timeout=30,
    )
    resp.raise_for_status()
    if "loginerrormessage" in resp.text:
        raise RuntimeError("로그인 실패: 아이디/비밀번호 확인")
    return session


# ──────────────────────────────────────────────
# 수강 과목 ID 수집
# ──────────────────────────────────────────────

def get_course_ids(session: requests.Session) -> list[str]:
    ids: set[str] = set()

    # 방법 1: 성적 개요 페이지 (서버사이드 렌더링, 항상 존재)
    try:
        resp = session.get(f"{LEARNUS_URL}/grade/overview/index.php", timeout=30)
        html = resp.text
        # /grade/report/user/index.php?id=X 또는 /course/view.php?id=X
        for m in re.finditer(r'["\'](?:[^"\']*)/(?:grade/report/user/index|course/view)\.php[^"\']*[?&]id=(\d+)', html):
            ids.add(m.group(1))
        print(f"성적 페이지에서 {len(ids)}개 과목 발견")
    except Exception as e:
        print(f"성적 페이지 실패: {e}")

    # 방법 2: 내 강좌 페이지
    if not ids:
        try:
            resp = session.get(f"{LEARNUS_URL}/course/index.php",
                               params={"mycourses": 1}, timeout=30)
            for m in re.finditer(r'course/view\.php[^"\']*[?&]id=(\d+)', resp.text):
                ids.add(m.group(1))
            print(f"강좌 목록 페이지에서 {len(ids)}개 과목 발견")
        except Exception as e:
            print(f"강좌 목록 실패: {e}")

    # 방법 3: 프로필 페이지
    if not ids:
        try:
            resp = session.get(f"{LEARNUS_URL}/user/profile.php", timeout=30)
            for m in re.finditer(r'course/view\.php[^"\']*[?&]id=(\d+)', resp.text):
                ids.add(m.group(1))
            print(f"프로필 페이지에서 {len(ids)}개 과목 발견")
        except Exception as e:
            print(f"프로필 페이지 실패: {e}")

    # id가 너무 작으면(시스템 과목) 제외
    ids = {i for i in ids if int(i) > 10}
    result = sorted(ids)
    print(f"최종 수강 과목: {result}")
    return result


# ──────────────────────────────────────────────
# 과목별 과제/퀴즈 마감일 수집
# ──────────────────────────────────────────────

_KO_MONTHS = {"1월":1,"2월":2,"3월":3,"4월":4,"5월":5,"6월":6,
              "7월":7,"8월":8,"9월":9,"10월":10,"11월":11,"12월":12}
_EN_MONTHS = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
              "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
              "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
              "sep":9,"oct":10,"nov":11,"dec":12}


def parse_deadline(text: str) -> datetime | None:
    text = text.strip()
    if not text or text in ("-", "마감일 없음", "No due date"):
        return None

    # Unix timestamp가 그대로 노출된 경우
    if re.fullmatch(r"\d{10,}", text):
        return datetime.fromtimestamp(int(text), tz=KST)

    # 한국어: "2026년 5월 25일 오후 11:59"
    m = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일"
                  r"(?:[^0-9]*)?(오전|오후)?\s*(\d{1,2}):(\d{2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ampm, h, mi = m.group(4), int(m.group(5)), int(m.group(6))
        if ampm == "오후" and h != 12:
            h += 12
        elif ampm == "오전" and h == 12:
            h = 0
        try:
            return KST.localize(datetime(y, mo, d, h, mi))
        except ValueError:
            pass

    # 영어: "25 May 2026, 11:59 PM"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}),?\s+(\d{1,2}):(\d{2})(?:\s*(AM|PM))?",
                  text, re.IGNORECASE)
    if m:
        d, mon_s, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        h, mi, ampm = int(m.group(4)), int(m.group(5)), (m.group(6) or "").upper()
        mo = _EN_MONTHS.get(mon_s)
        if mo:
            if ampm == "PM" and h != 12:
                h += 12
            elif ampm == "AM" and h == 12:
                h = 0
            try:
                return KST.localize(datetime(y, mo, d, h, mi))
            except ValueError:
                pass
    return None


def get_module_events(session: requests.Session, course_id: str,
                      module: str, course_name: str,
                      time_from: datetime, time_to: datetime) -> list:
    try:
        resp = session.get(f"{LEARNUS_URL}/mod/{module}/index.php",
                           params={"id": course_id}, timeout=20)
        if resp.status_code != 200:
            return []
    except Exception as e:
        print(f"  [{module}/{course_id}] 요청 실패: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    events = []

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        name_a = cells[0].find("a")
        if not name_a:
            continue

        name    = name_a.get_text(strip=True)
        act_url = name_a.get("href", "")

        # data-timedue / data-timestamp 속성 우선
        ts_el = (row.find(attrs={"data-timedue": True})
                 or row.find(attrs={"data-timestamp": True}))
        if ts_el:
            deadline = datetime.fromtimestamp(
                int(ts_el.get("data-timedue") or ts_el.get("data-timestamp")), tz=KST)
        else:
            deadline = None
            for cell in cells[1:]:
                deadline = parse_deadline(cell.get_text(strip=True))
                if deadline:
                    break

        if not deadline:
            continue

        dl_aware = deadline if deadline.tzinfo else KST.localize(deadline)
        if time_from <= dl_aware <= time_to:
            events.append({
                "name": name,
                "timesort": int(dl_aware.timestamp()),
                "modulename": module,
                "url": act_url,
                "course": {"fullname": course_name},
            })
    return events


def get_upcoming_events(session: requests.Session) -> list:
    now = datetime.now(KST)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_from = today_start
    time_to   = today_start + timedelta(days=8)

    course_ids = get_course_ids(session)
    if not course_ids:
        print("수강 과목을 찾지 못했습니다.")
        return []

    # 과목 이름 수집 (성적 페이지)
    course_names: dict[str, str] = {}
    try:
        resp = session.get(f"{LEARNUS_URL}/grade/overview/index.php", timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=re.compile(r"id=\d+")):
            m = re.search(r"id=(\d+)", a["href"])
            if m and m.group(1) in course_ids:
                course_names[m.group(1)] = a.get_text(strip=True)
    except Exception:
        pass

    all_events: list = []
    for cid in course_ids:
        cname = course_names.get(cid, f"과목 {cid}")
        for mod in ["assign", "quiz"]:
            evts = get_module_events(session, cid, mod, cname, time_from, time_to)
            if evts:
                print(f"  [{cname}] {mod}: {len(evts)}개")
            all_events.extend(evts)

    return all_events


# ──────────────────────────────────────────────
# Slack 전송
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
        print("환경변수 누락", file=sys.stderr)
        sys.exit(1)

    now   = datetime.now(KST)
    today = now.date()
    print(f"실행 시각: {now.strftime('%Y-%m-%d %H:%M KST')}")

    session = login(username, password)
    print("로그인 성공")

    events = get_upcoming_events(session)

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
