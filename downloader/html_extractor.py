import json
import re
from urllib.parse import urljoin

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def extract_video_from_html(url: str) -> dict:
    """HTML을 직접 파싱하여 동영상 URL, 제목, 메타 정보를 추출한다.

    MacCMS 기반 사이트의 player_aaaa 변수를 분석한다.

    Returns:
        {"video_urls": [...], "title": str, "headers": dict, "subtitle_url": str}
    """
    print(f"[html] 페이지 분석: {url}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    try:
        resp = requests.get(url, headers=headers, verify=False, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[html] 페이지 요청 실패: {e}")
        return {"video_urls": [], "title": "video", "headers": {}, "subtitle_url": ""}

    html = resp.text
    title = _extract_title(html)
    player_data = _extract_player_data(html)

    if not player_data:
        print("[html] player_aaaa 데이터를 찾지 못했습니다.")
        return {"video_urls": [], "title": title, "headers": {}, "subtitle_url": ""}

    raw_url = player_data.get("url", "")
    vod_name = player_data.get("vod_data", {}).get("vod_name", "")
    # 페이지 title이 generic이면 vod_name을 fallback으로 사용
    if vod_name and title == "video":
        title = vod_name

    video_url, subtitle_url = _parse_video_url(raw_url)

    if not video_url:
        print("[html] 동영상 URL을 추출하지 못했습니다.")
        return {"video_urls": [], "title": title, "headers": {}, "subtitle_url": ""}

    # Referer 헤더 설정 (CDN Referer 검증 통과용)
    download_headers = {
        "Referer": url,
        "User-Agent": headers["User-Agent"],
    }

    print(f"[html] 제목: {title}")
    print(f"[html] 동영상: {video_url[:120]}")
    if subtitle_url:
        print(f"[html] 자막: {subtitle_url[:120]}")

    return {
        "video_urls": [video_url],
        "title": title,
        "headers": download_headers,
        "subtitle_url": subtitle_url,
    }


def _extract_title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html)
    if match:
        title = match.group(1).strip()
        # " - 사이트명" 부분 제거, "다시보기" 등 불필요한 접미사 제거
        title = re.split(r"\s*[-|]\s*(?:무료|티비)", title)[0].strip()
        title = re.sub(r"\s*다시보기\s*$", "", title).strip()
        title = re.sub(r'[\\/:*?"<>|]', '_', title)
        return title if title else "video"
    return "video"


def _extract_player_data(html: str) -> dict | None:
    """HTML에서 player_aaaa JSON 데이터를 추출한다."""
    match = re.search(r"var\s+player_aaaa\s*=\s*(\{.*?\})\s*</script>", html, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _parse_video_url(raw_url: str) -> tuple[str, str]:
    """player_aaaa의 url 필드에서 동영상 URL과 자막 URL을 분리한다.

    형식: "https://cdn.com/video.mp4?srthttps://srt.com/sub.srt"
    """
    subtitle_url = ""

    if "?srt" in raw_url:
        parts = raw_url.split("?srt", 1)
        video_url = parts[0]
        subtitle_url = parts[1] if len(parts) > 1 else ""
    else:
        video_url = raw_url

    return video_url.strip(), subtitle_url.strip()
