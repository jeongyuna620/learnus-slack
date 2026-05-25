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
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
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
        raise RuntimeError("로그인 실패: 아이디/비밀번호를 확인하세요")

    return session


# ──────────────────────────────────────────────
# 수강 과목 목록 수집
# ──────────────────────────────────────────────

def get_course_ids(session: requests.Session) -> list[str]:
    ids: set[str] = set()

    # 시도할 페이지 목록
    pages = [
        f"{LEARNUS_URL}/my/",
        f"{LEARNUS_URL}/course/index.php",
    ]

    for page_url in pages:
        resp = session.get(page_url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 페이지 내 모든 링크에서 course id 추출
        all_hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
        course_hrefs = [h for h in all_hrefs if "course" in h and "id=" in h]

        # 디버그: 링크 샘플 출력
        print(f"[{page_url}] 전체 링크 {len(all_hrefs)}개, course 관련 {len(course_hrefs)}개")
        for h in course_hrefs[:10]:
            print(f"  {h}")

        for href in all_hrefs:
            # /course/view.php?id=123 또는 유사 패턴
            m = re.search(r"/course/view\.php[^\"']*[?&]id=(\d+)", href)
            if m:
                ids.add(m.group(1))

    print(f"수강 과목 {len(ids)}개 발견: {sorted(ids)}")
    return sorted(ids)


# ──────────────────────────────────────────────
# 날짜 파싱 헬퍼
# ──────────────────────────────────────────────

_KO_MONTHS = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
    "7": 7, "8": 8, "9": 9, "10": 10, "11": 11, "12": 12,
}
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_deadline(text: str) -> datetime | None:
    """마감일 텍스트를 KST datetime으로 변환."""
    text = text.strip()
    if not text or text == "-":
        return None

    # 1) data-timestamp 같은 숫자가 텍스트로 노출된 경우
    if re.fullmatch(r"\d{10,}", text):
        return datetime.fromtimestamp(int(text), tz=KST)

    # 2) 한국어: "2026년 5월 25일 (일) 오후 11:59"
    m = re.search(
        r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일"
        r"(?:[^\d]*)?(오전|오후)?\s*(\d{1,2}):(\d{2})",
        text,
    )
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

    # 3) 영어: "25 May 2026, 11:59 PM" / "Monday, 25 May 2026, 11:59 PM"
    m = re.search(
        r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}),?\s+(\d{1,2}):(\d{2})(?:\s*(AM|PM))?",
        text, re.IGNORECASE,
    )
    if m:
        d, mon_s, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        h, mi = int(m.group(4)), int(m.group(5))
        ampm = (m.group(6) or "").upper()
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


# ──────────────────────────────────────────────
# 과목별 과제/퀴즈 마감일 수집
# ──────────────────────────────────────────────

def get_module_events(session: requests.Session, course_id: str,
                      module: str, course_name: str,
                      time_from: datetime, time_to: datetime) -> list:
    """
    /mod/<module>/index.php?id=COURSE_ID 페이지에서
    마감일이 있는 활동을 파싱한다.
    """
    url = f"{LEARNUS_URL}/mod/{module}/index.php"
    try:
        resp = session.get(url, params={"id": course_id}, timeout=20)
        if resp.status_code != 200:
            return []
    except Exception as e:
        print(f"  [{module}] {course_id} 요청 실패: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    events = []

    # 테이블의 모든 행 순회
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        # 이름 셀 (첫 번째 <a> 링크)
        name_a = cells[0].find("a")
        if not name_a:
            continue
        name = name_a.get_text(strip=True)
        act_url = name_a.get("href", "")

        # 타임스탬프 속성이 있으면 우선 사용
        ts_el = row.find(attrs={"data-timedue": True}) \
               or row.find(attrs={"data-timestamp": True})
        if ts_el:
            ts = int(ts_el.get("data-timedue") or ts_el.get("data-timestamp"))
            deadline = datetime.fromtimestamp(ts, tz=KST)
        else:
            # 날짜가 들어있을 법한 셀 순서대로 파싱 시도
            deadline = None
            for cell in cells[1:]:
                deadline = parse_deadline(cell.get_text(strip=True))
                if deadline:
                    break

        if not deadline:
            continue

        # 조회 범위 내인지 확인
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
    time_to = today_start + timedelta(days=8)

    course_ids = get_course_ids(session)
    if not course_ids:
        print("수강 과목을 찾지 못했습니다.")
        return []

    # 과목 이름 수집 (대시보드 링크 텍스트)
    resp = session.get(f"{LEARNUS_URL}/my/", timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    course_names: dict[str, str] = {}
    for a in soup.find_all("a", href=re.compile(r"/course/view\.php\?id=\d+")):
        m = re.search(r"id=(\d+)", a["href"])
        if m:
            course_names[m.group(1)] = a.get_text(strip=True)

    all_events: list = []
    modules = ["assign", "quiz"]

    for cid in course_ids:
        cname = course_names.get(cid, f"course {cid}")
        for mod in modules:
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
        "text": {
            "type": "plain_text",
            "text": f"📚 LearnUs 마감 알림  {today.strftime('%m/%d')}",
            "emoji": True,
        },
    }]

    for days_left in sorted(events_by_days.keys()):
        evts = events_by_days[days_left]
        if not evts:
            continue

        if days_left == 0:
            label = "🔥 *오늘 마감!*"
        elif days_left == 1:
            label = "🚨 *내일 마감!*"
        else:
            label = f"⚠️ *{days_left}일 후 마감*"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": label}})

        for event in evts:
            dl = datetime.fromtimestamp(event["timesort"], tz=KST)
            wd = WEEKDAY_KO[dl.weekday()]
            emoji = MODULE_EMOJI.get(event.get("modulename", ""), "📌")
            course = event.get("course", {}).get("fullname", "")

            lines = [f"{emoji} *{event['name']}*"]
            if course:
                lines.append(f"    과목: {course}")
            lines.append(f"    마감: {dl.strftime('%m/%d')}({wd}) {dl.strftime('%H:%M')}")

            block: dict = {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
            if event.get("url"):
                block["accessory"] = {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "바로가기", "emoji": True},
                    "url": event["url"],
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
    username    = os.environ.get("LEARNUS_USERNAME")
    password    = os.environ.get("LEARNUS_PASSWORD")
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    if not all([username, password, webhook_url]):
        print("환경변수 누락: LEARNUS_USERNAME / LEARNUS_PASSWORD / SLACK_WEBHOOK_URL", file=sys.stderr)
        sys.exit(1)

    now   = datetime.now(KST)
    today = now.date()
    print(f"실행 시각: {now.strftime('%Y-%m-%d %H:%M KST')}")

    session = login(username, password)
    print("로그인 성공")

    events = get_upcoming_events(session)

    print(f"\n[전체 이벤트 - {len(events)}개]")
    for e in events:
        dl = datetime.fromtimestamp(e["timesort"], tz=KST)
        diff = (dl.date() - today).days
        print(f"  D{diff:+d} | {dl.strftime('%m/%d %H:%M')} | {e.get('modulename','?'):8s} | {e['name']}")

    # 오전(~18시): 오늘·내일·3일 후 / 오후(18시~): 오늘만
    days_to_check = [0, 1, 3] if now.hour < 18 else [0]
    events_by_days: dict[int, list] = {d: [] for d in days_to_check}

    for event in events:
        dl = datetime.fromtimestamp(event["timesort"], tz=KST)
        diff = (dl.date() - today).days
        if diff in events_by_days:
            events_by_days[diff].append(event)

    total = sum(len(v) for v in events_by_days.values())
    if total == 0:
        print("\n알림 대상 없음 — 생략")
        return

    blocks = build_slack_blocks(events_by_days, today)
    send_slack(webhook_url, blocks)
    print(f"\nSlack 알림 전송 완료 ({total}건)")


if __name__ == "__main__":
    main()
