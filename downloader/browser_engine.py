import re
import time
from urllib.parse import urljoin, urlparse, parse_qs, unquote

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


def extract_m3u8_from_page(url: str, wait_seconds: int = 10) -> dict:
    """Playwright로 페이지를 열어 m3u8 URL과 페이지 제목을 추출한다.

    두 가지 방법을 동시에 사용:
    1. 네트워크 요청 가로채기 (intercept)
    2. HTML/JS 소스에서 정규식 매칭

    Returns:
        {"m3u8_urls": [...], "title": str, "headers": dict}
    """
    m3u8_urls = []
    video_urls = []
    captured_headers = {}

    print(f"[browser] 페이지 로딩: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        page = context.new_page()

        def on_response(response):
            req_url = response.url
            req_headers = response.request.headers
            if ".m3u8" in req_url or "m3u8" in req_url:
                m3u8_urls.append(req_url)
                captured_headers.update({
                    "Referer": req_headers.get("referer", url),
                    "Origin": req_headers.get("origin", ""),
                    "User-Agent": req_headers.get("user-agent", ""),
                })
            # .mp4/.flv 직접 파일만 (HLS .ts 세그먼트는 제외)
            if any(ext in req_url.split("?")[0] for ext in [".mp4", ".flv", ".mkv"]):
                video_urls.append(req_url)
                # 동영상 요청의 실제 헤더를 캡처 (Referer가 핵심)
                captured_headers.update({
                    "Referer": req_headers.get("referer", url),
                    "Origin": req_headers.get("origin", ""),
                    "User-Agent": req_headers.get("user-agent", ""),
                })

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"[browser] 페이지 로딩 경고: {e}")

        # 동적 콘텐츠 로딩 대기
        time.sleep(wait_seconds)

        title = _safe_title(page)

        # HTML 소스에서도 m3u8 URL 검색
        html_m3u8 = _extract_m3u8_from_html(page, url)
        m3u8_urls.extend(html_m3u8)

        # iframe 내부도 검사
        iframe_m3u8 = _extract_from_iframes(page, url)
        m3u8_urls.extend(iframe_m3u8)

        browser.close()

    # 중복 제거하면서 순서 유지
    seen = set()
    unique_urls = []
    for u in m3u8_urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    if unique_urls:
        print(f"[browser] m3u8 URL {len(unique_urls)}개 발견")
        for i, u in enumerate(unique_urls):
            print(f"  [{i + 1}] {u[:120]}...")

    # video_urls 정리: 중복 제거 + CDN 직접 URL 우선
    clean_video_urls = _clean_video_urls(video_urls)
    if clean_video_urls:
        print(f"[browser] 직접 동영상 URL {len(clean_video_urls)}개 발견")
        for i, u in enumerate(clean_video_urls):
            print(f"  [{i + 1}] {u[:120]}")

    if not unique_urls and not clean_video_urls:
        print("[browser] 동영상 URL을 찾지 못했습니다.")

    return {
        "m3u8_urls": unique_urls,
        "video_urls": clean_video_urls,
        "title": title,
        "headers": captured_headers,
    }


def _safe_title(page) -> str:
    """페이지 제목을 안전하게 추출한다."""
    try:
        title = page.title() or "video"
        title = re.sub(r'[\\/:*?"<>|]', '_', title).strip()
        return title if title else "video"
    except Exception:
        return "video"


def _extract_m3u8_from_html(page, base_url: str) -> list[str]:
    """HTML 소스와 스크립트에서 m3u8 URL을 정규식으로 추출한다."""
    urls = []
    try:
        content = page.content()

        # 정규식으로 m3u8 URL 패턴 매칭
        patterns = [
            r'https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*',
            r'//[^\s\'"<>]+\.m3u8[^\s\'"<>]*',
            r'/[^\s\'"<>]+\.m3u8[^\s\'"<>]*',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, content)
            for match in matches:
                if match.startswith("//"):
                    match = "https:" + match
                elif match.startswith("/"):
                    match = urljoin(base_url, match)
                urls.append(match)

        # <video> / <source> 태그에서 src 추출
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup.find_all(["video", "source"]):
            src = tag.get("src", "")
            if src:
                full_url = urljoin(base_url, src)
                urls.append(full_url)

    except Exception as e:
        print(f"[browser] HTML 파싱 경고: {e}")

    return urls


def _clean_video_urls(urls: list[str]) -> list[str]:
    """동영상 URL을 정리한다.

    - 프록시 URL에서 실제 CDN URL 추출
    - 중복 제거
    - CDN 직접 URL 우선 정렬
    """
    extracted = []

    for raw_url in urls:
        # 프록시 URL에서 실제 URL 추출 (예: chaktt.php?url=https://cdn.com/video.mp4)
        parsed = urlparse(raw_url)
        query = parse_qs(parsed.query)

        # url= 파라미터가 있으면 추출
        if "url" in query:
            inner = query["url"][0]
            # 내부 URL에 붙은 ?srt 등 잘라내기
            if "?srt" in inner:
                inner = inner.split("?srt")[0]
            extracted.append(inner)
        else:
            extracted.append(raw_url)

    # 중복 제거 (순서 유지)
    seen = set()
    unique = []
    for u in extracted:
        normalized = unquote(u).split("?")[0]  # 쿼리스트링 무시 비교
        if normalized not in seen:
            seen.add(normalized)
            unique.append(u)

    # CDN URL 우선 (b-cdn.net, cloudfront, cdn 포함 URL)
    cdn_keywords = ["cdn", "cloudfront", "b-cdn", "akamai", "fastly"]
    unique.sort(key=lambda u: 0 if any(k in u for k in cdn_keywords) else 1)

    return unique


def _extract_from_iframes(page, base_url: str) -> list[str]:
    """iframe 내부의 동영상 소스를 추출한다."""
    urls = []
    try:
        frames = page.frames
        for frame in frames:
            if frame == page.main_frame:
                continue
            try:
                content = frame.content()
                matches = re.findall(
                    r'https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*', content
                )
                urls.extend(matches)

                # frame 내 video/source 태그
                soup = BeautifulSoup(content, "html.parser")
                for tag in soup.find_all(["video", "source"]):
                    src = tag.get("src", "")
                    if src and (".m3u8" in src or ".mp4" in src):
                        urls.append(urljoin(frame.url or base_url, src))
            except Exception:
                continue
    except Exception:
        pass

    return urls
