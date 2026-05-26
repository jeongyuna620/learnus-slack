"""
LearnUS 동영상 자동 재생 스크립트
========================================
사용법 1 - vod.env 파일에 저장 후 그냥 실행:
  python vod_player.py

사용법 2 - 직접 인자 입력:
  python vod_player.py --id 학번 --pw 비밀번호
  python vod_player.py --id 학번 --pw 비밀번호 --course 마케팅애널리틱스
  python vod_player.py --id 학번 --pw 비밀번호 --speed 1.5
"""

import os
import time
import re
import argparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, NoSuchFrameException
)

try:
    from webdriver_manager.chrome import ChromeDriverManager
    _WDM_AVAILABLE = True
except ImportError:
    _WDM_AVAILABLE = False


def _load_env_file(path: str = "vod.env") -> dict:
    """vod.env 파일에서 KEY=VALUE 파싱"""
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

LEARNUS_URL = "https://ys.learnus.org"
WAIT = 10  # 기본 대기 시간(초)


# ──────────────────────────────────────────────
# 드라이버 설정
# ──────────────────────────────────────────────

def create_driver(headless: bool = False) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--mute-audio")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120.0.0.0 Safari/537.36")

    # ChromeDriver 자동 설치 (webdriver-manager 사용 가능 시)
    if _WDM_AVAILABLE:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
    else:
        driver = webdriver.Chrome(options=options)

    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    driver.maximize_window()
    return driver


# ──────────────────────────────────────────────
# 로그인
# ──────────────────────────────────────────────

def login(driver: webdriver.Chrome, username: str, password: str) -> bool:
    print("🔐 로그인 중...")
    driver.get(f"{LEARNUS_URL}/login/index.php")
    time.sleep(2)
    print(f"   로그인 URL: {driver.current_url}")

    def _try_fill(id_sel, pw_sel, btn_sel):
        try:
            WebDriverWait(driver, WAIT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, pw_sel))
            )
            driver.find_element(By.CSS_SELECTOR, id_sel).send_keys(username)
            driver.find_element(By.CSS_SELECTOR, pw_sel).send_keys(password)
            driver.find_element(By.CSS_SELECTOR, btn_sel).click()
            return True
        except NoSuchElementException:
            return False

    # 시도 1: 표준 Moodle 폼 (id="username", id="loginbtn")
    filled = _try_fill("#username", "#password", "#loginbtn")

    # 시도 2: 일반 name 속성 기반
    if not filled:
        filled = _try_fill(
            "input[name='username'], input[name='id']",
            "input[type='password']",
            "input[type='submit'], button[type='submit']",
        )

    if not filled:
        print("   ❌ 로그인 폼을 찾지 못했습니다.")
        return False

    time.sleep(3)

    if "login" in driver.current_url.lower():
        print("   ❌ 로그인 실패 — 아이디/비밀번호를 확인하세요.")
        return False

    print(f"   ✅ 로그인 성공")
    return True


# ──────────────────────────────────────────────
# 수강 과목 수집
# ──────────────────────────────────────────────

def get_courses(driver: webdriver.Chrome) -> dict[str, str]:
    """수강 과목 {course_id: 과목명} 반환"""
    courses: dict[str, str] = {}
    driver.get(f"{LEARNUS_URL}/my/")
    time.sleep(2)

    for link in driver.find_elements(By.CSS_SELECTOR, "a[href*='course/view.php']"):
        href = link.get_attribute("href") or ""
        m = re.search(r"id=(\d+)", href)
        if m and int(m.group(1)) > 10:
            name = link.text.strip()
            if name:
                courses[m.group(1)] = name

    # 성적 개요 페이지에서 보완
    if len(courses) < 3:
        driver.get(f"{LEARNUS_URL}/grade/report/overview/index.php")
        time.sleep(2)
        for link in driver.find_elements(
            By.CSS_SELECTOR, "a[href*='course/user.php']"
        ):
            href = link.get_attribute("href") or ""
            if "mode=grade" not in href:
                continue
            m = re.search(r"\bid=(\d+)", href)
            if m and int(m.group(1)) > 10:
                name = link.text.strip()
                if name:
                    courses.setdefault(m.group(1), name)

    print(f"\n📋 수강 과목 {len(courses)}개: {list(courses.values())}")
    return courses


# ──────────────────────────────────────────────
# VOD 목록 수집
# ──────────────────────────────────────────────

def get_vod_list(driver: webdriver.Chrome,
                 course_id: str, course_name: str) -> list[dict]:
    """미시청 VOD 목록 반환"""
    driver.get(f"{LEARNUS_URL}/course/view.php?id={course_id}")
    time.sleep(2)

    vods = []
    items = driver.find_elements(By.CSS_SELECTOR, "li[class*='modtype_vod']")

    for item in items:
        try:
            link = item.find_element(By.CSS_SELECTOR, "a[href*='/mod/']")
            url  = link.get_attribute("href")
            name = link.text.strip()
            if not url or not name:
                continue

            # 완료 여부 확인 (체크마크 클래스)
            completed = bool(item.find_elements(
                By.CSS_SELECTOR,
                ".completion-icon.complete, "
                ".automatic-completion-conditions .complete, "
                "img[alt*='완료'], img[alt*='Completed']"
            ))

            if completed:
                print(f"  ✅ (완료) {name}")
            else:
                print(f"  📹 {name}")
                vods.append({"name": name, "url": url, "course": course_name})

        except NoSuchElementException:
            continue

    return vods


# ──────────────────────────────────────────────
# 영상 재생
# ──────────────────────────────────────────────

def _find_video_in_frames(driver: webdriver.Chrome) -> bool:
    """모든 iframe을 재귀적으로 탐색해 video 태그 발견 시 해당 프레임에 머묾"""
    # 현재 프레임에서 video 탐색
    videos = driver.find_elements(By.TAG_NAME, "video")
    if videos:
        return True

    # 하위 iframe 탐색
    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for i, frame in enumerate(frames):
        try:
            driver.switch_to.frame(frame)
            time.sleep(1)
            if _find_video_in_frames(driver):
                return True
            driver.switch_to.parent_frame()
        except (NoSuchFrameException, Exception):
            try:
                driver.switch_to.parent_frame()
            except Exception:
                driver.switch_to.default_content()

    return False


def _play_html5_video(driver: webdriver.Chrome, speed: float) -> bool:
    """현재 프레임의 video 태그 재생. 완료 시 True 반환"""
    videos = driver.find_elements(By.TAG_NAME, "video")
    if not videos:
        return False

    video = videos[0]

    # 재생 준비 대기 (최대 10초)
    for _ in range(10):
        ready = driver.execute_script(
            "return arguments[0].readyState >= 1;", video
        )
        if ready:
            break
        time.sleep(1)

    # 재생 시작
    driver.execute_script(f"""
        var v = arguments[0];
        v.muted = true;
        v.playbackRate = {speed};
        v.play().catch(function(){{}});
    """, video)
    time.sleep(1)

    # 영상 길이
    duration = driver.execute_script("return arguments[0].duration;", video) or 0
    if duration and duration == duration:  # NaN 방지
        duration = float(duration)
    else:
        duration = 0

    if duration > 0:
        wait_total = duration / speed + 5
        print(f"  ⏱  길이: {int(duration//60)}분 {int(duration%60)}초 "
              f"| {speed}배속 → 약 {int(wait_total//60)}분 {int(wait_total%60)}초 대기")

        elapsed = 0
        interval = 15
        while elapsed < wait_total:
            time.sleep(min(interval, wait_total - elapsed))
            elapsed += interval

            ended   = driver.execute_script("return arguments[0].ended;", video)
            current = driver.execute_script("return arguments[0].currentTime;", video) or 0
            print(f"  ▶ {int(current)}s / {int(duration)}s", end="\r")

            if ended:
                print(f"\n  ✅ 재생 완료!")
                return True

        print(f"\n  ✅ 재생 완료 (타임아웃)")
    else:
        # 길이 불명 — 10분 대기
        print("  ⏳ 영상 길이 불명 → 10분 대기")
        time.sleep(600)

    return True


def play_vod(driver: webdriver.Chrome, vod: dict, speed: float = 2.0) -> bool:
    """VOD 페이지 이동 후 재생"""
    print(f"\n▶ {vod['course']}  |  {vod['name']}")
    driver.get(vod["url"])
    time.sleep(3)

    driver.switch_to.default_content()

    # 팝업 창 처리
    main_window = driver.current_window_handle
    time.sleep(1)
    all_windows = driver.window_handles
    if len(all_windows) > 1:
        driver.switch_to.window(all_windows[-1])
        time.sleep(2)

    # iframe 탐색 후 video 발견
    found = _find_video_in_frames(driver)

    if not found:
        # 직접 재생 버튼 클릭 시도
        try:
            btn = driver.find_element(
                By.CSS_SELECTOR,
                "button.vjs-big-play-button, .play-button, [class*='play']"
            )
            btn.click()
            time.sleep(2)
            found = _find_video_in_frames(driver)
        except NoSuchElementException:
            pass

    if not found:
        print("  ⚠️  영상 요소를 찾지 못했습니다 — 수동 확인 필요")
        driver.switch_to.default_content()
        if len(driver.window_handles) > 1:
            driver.close()
            driver.switch_to.window(main_window)
        return False

    success = _play_html5_video(driver, speed)

    # 정리
    driver.switch_to.default_content()
    if len(driver.window_handles) > 1:
        driver.close()
        driver.switch_to.window(main_window)

    return success


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def main():
    # vod.env 파일에서 기본값 로드
    env = _load_env_file("vod.env")

    parser = argparse.ArgumentParser(description="LearnUS VOD 자동 재생")
    parser.add_argument("--id",     default=env.get("LEARNUS_ID", ""),  help="LearnUS 아이디 (학번)")
    parser.add_argument("--pw",     default=env.get("LEARNUS_PW", ""),  help="LearnUS 비밀번호")
    parser.add_argument("--course", default=env.get("COURSE", ""),      help="특정 과목명 필터 (일부만 입력 가능)")
    parser.add_argument("--speed",  type=float, default=float(env.get("SPEED", "2.0")),
                        help="재생 속도 (기본 2.0배, 최대 16.0)")
    parser.add_argument("--headless", action="store_true",
                        help="브라우저 창 없이 백그라운드 실행")
    args = parser.parse_args()

    # 필수값 확인
    if not args.id or not args.pw:
        print("❌ 아이디/비밀번호가 없습니다. vod.env 파일을 만들거나 --id --pw 인자를 입력하세요.")
        print("   예: python vod_player.py --id 학번 --pw 비밀번호")
        input("Enter를 눌러 종료...")
        return

    speed = max(0.5, min(args.speed, 16.0))
    print(f"재생 속도: {speed}배")

    driver = create_driver(headless=args.headless)

    try:
        if not login(driver, args.id, args.pw):
            return

        courses = get_courses(driver)
        if not courses:
            print("❌ 수강 과목을 찾지 못했습니다.")
            return

        # 과목 필터
        if args.course:
            courses = {cid: name for cid, name in courses.items()
                       if args.course in name}
            if not courses:
                print(f"❌ '{args.course}' 과목을 찾지 못했습니다.")
                return
            print(f"필터 적용 → {list(courses.values())}")

        total_played = 0
        total_skipped = 0

        for cid, cname in courses.items():
            print(f"\n{'='*50}")
            print(f"📚 {cname}")
            print('='*50)

            vods = get_vod_list(driver, cid, cname)

            if not vods:
                print("  미시청 동영상 없음")
                continue

            print(f"  → 미시청 {len(vods)}개 재생 시작\n")
            for vod in vods:
                ok = play_vod(driver, vod, speed=speed)
                if ok:
                    total_played += 1
                else:
                    total_skipped += 1
                time.sleep(2)

        print(f"\n{'='*50}")
        print(f"🎉 완료!  재생 {total_played}개  |  실패/스킵 {total_skipped}개")

    finally:
        input("\n[Enter] 를 누르면 브라우저를 닫습니다...")
        driver.quit()


if __name__ == "__main__":
    main()
