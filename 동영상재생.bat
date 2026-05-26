@echo off
chcp 65001 >nul
echo ====================================
echo  LearnUS 동영상 자동 재생
echo ====================================
echo.

:: 패키지 설치 (처음 한 번)
echo [1/2] 필요 패키지 확인 중...
pip install selenium webdriver-manager -q

echo [2/2] 동영상 자동 재생 시작!
echo.
python vod_player.py

pause
