"""세션 기반 동영상 URL 추출 (tvmon 등 암호화된 플레이어 지원).

흐름:
1. 페이지 HTML에서 data-session1 추출
2. /api/create_session.php 호출 → player_url 획득
3. 플레이어 페이지에서 3단계 AES 복호화 → HLS URL 획득
"""

import base64
import json
import re
import subprocess
from html import unescape
from urllib.parse import urljoin

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_EMPTY = {"video_urls": [], "title": "video", "headers": {}, "subtitle_url": ""}


def extract_video_from_session(url: str) -> dict:
    """세션 기반 사이트에서 동영상 URL을 추출한다."""
    print(f"[session] 페이지 분석: {url}")

    sess = requests.Session()
    headers = {"User-Agent": UA}

    try:
        resp = sess.get(url, headers=headers, verify=False, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[session] 페이지 요청 실패: {e}")
        return _EMPTY

    html = resp.text

    # data-session1 추출
    session_data = _extract_session_data(html)
    if not session_data:
        print("[session] data-session1을 찾지 못했습니다.")
        return _EMPTY

    title = _extract_title(html, session_data)
    base_domain = _get_base_url(url)

    # create_session API 호출
    create_result = _create_session(sess, base_domain, url, session_data)
    if not create_result:
        return {**_EMPTY, "title": title}

    player_url = (
        f"{create_result['player_url']}"
        f"?t={create_result['t']}&sig={create_result['sig']}"
    )
    print(f"[session] 플레이어 URL 획득")

    # 플레이어 페이지에서 HLS URL 추출
    hls_url = _extract_hls_from_player(sess, player_url, url)
    if not hls_url:
        return {**_EMPTY, "title": title}

    download_headers = {"Referer": player_url, "User-Agent": UA}

    print(f"[session] 제목: {title}")
    print(f"[session] HLS: {hls_url[:120]}")

    return {
        "video_urls": [hls_url],
        "title": title,
        "headers": download_headers,
        "subtitle_url": session_data.get("srt", ""),
        "is_hls": True,
    }


def _extract_session_data(html: str) -> dict | None:
    match = re.search(r'data-session1="([^"]+)"', html)
    if not match:
        return None
    try:
        return json.loads(unescape(match.group(1)))
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_title(html: str, session_data: dict) -> str:
    # session data의 t 필드에 제목이 있음
    title = session_data.get("t", "")
    if not title:
        match = re.search(r"<title>(.*?)</title>", html)
        if match:
            title = match.group(1).strip()
            title = re.split(r"\s*[-|]\s*(?:무료|티비)", title)[0].strip()
            title = re.sub(r"\s*다시보기\s*$", "", title).strip()
    title = re.sub(r'[\\/:*?"<>|]', "_", title).strip(". ") if title else "video"
    return title or "video"


def _get_base_url(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _create_session(
    sess: requests.Session, base_domain: str, page_url: str, session_data: dict
) -> dict | None:
    try:
        resp = sess.post(
            f"{base_domain}/api/create_session.php",
            json=session_data,
            headers={
                "User-Agent": UA,
                "Referer": page_url,
                "Content-Type": "application/json",
            },
            verify=False,
            timeout=15,
        )
        data = resp.json()
        if data.get("success") and data.get("player_url"):
            return data
        print(f"[session] 세션 생성 실패: {data}")
    except Exception as e:
        print(f"[session] 세션 생성 오류: {e}")
    return None


def _extract_hls_from_player(
    sess: requests.Session, player_url: str, page_url: str
) -> str:
    """플레이어 페이지를 가져와서 3단계 복호화로 HLS URL을 추출한다."""
    player_domain = "/".join(player_url.split("/")[:3])

    try:
        resp = sess.get(
            player_url,
            headers={"User-Agent": UA, "Referer": page_url},
            verify=False,
            timeout=15,
        )
        player_html = resp.text
    except Exception as e:
        print(f"[session] 플레이어 페이지 요청 실패: {e}")
        return ""

    # --- Lv2: 암호화된 블록 복호화 ---
    decrypted_js = _decrypt_player_blocks(sess, player_html, player_domain, player_url)
    if not decrypted_js:
        return ""

    # --- Lv3: 최종 HLS URL 복호화 ---
    return _decrypt_hls_url(sess, decrypted_js, player_domain, player_url)


def _decrypt_player_blocks(
    sess: requests.Session,
    player_html: str,
    player_domain: str,
    player_url: str,
) -> str:
    """Lv2: 플레이어 HTML의 암호화된 블록들을 복호화한다."""
    # 키 데이터 추출 (변수명이 매번 바뀜)
    k_match = re.search(
        r'window\.(_\w+)\s*=\s*\{k:"([^"]+)",\s*t:"([^"]+)"', player_html
    )
    if not k_match:
        print("[session] 플레이어 키 데이터를 찾지 못했습니다.")
        return ""

    var_name = k_match.group(1)
    k1 = k_match.group(2)
    token = k_match.group(3)

    # 암호화된 블록 추출
    blocks = re.findall(
        rf"{re.escape(var_name)}\.b\.push\(\{{c:\"([^\"]+)\",v:\"([^\"]+)\"\}}\)",
        player_html,
    )
    if not blocks:
        print("[session] 암호화 블록을 찾지 못했습니다.")
        return ""

    # key-share API로 k2 획득
    try:
        resp = sess.post(
            f"{player_domain}/api/key-share.php",
            json={"token": token},
            headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Referer": player_url,
            },
            verify=False,
            timeout=10,
        )
        k2 = resp.json().get("k2", "")
    except Exception as e:
        print(f"[session] key-share 요청 실패: {e}")
        return ""

    if not k2:
        print("[session] k2를 받지 못했습니다.")
        return ""

    # AES 키 = k1 XOR k2
    aes_key = _xor_hex(k1, k2)

    # 가장 큰 블록 (메인 플레이어 코드) 복호화
    largest_block = ""
    for ct_b64, iv_hex in blocks:
        decrypted = _aes_decrypt_b64(ct_b64, aes_key, iv_hex)
        if decrypted and len(decrypted) > len(largest_block):
            largest_block = decrypted

    if not largest_block:
        print("[session] 블록 복호화에 실패했습니다.")

    return largest_block


def _decrypt_hls_url(
    sess: requests.Session,
    decrypted_js: str,
    player_domain: str,
    player_url: str,
) -> str:
    """Lv3: 복호화된 JS에서 최종 HLS URL을 추출한다."""
    # LV3_WRAPPED_KEY 추출
    lwk_match = re.search(r"LV3_WRAPPED_KEY\s*=\s*'([^']+)'", decrypted_js)
    if not lwk_match:
        # Lv3가 없으면 직접 URL이 있을 수 있음
        url_match = re.search(
            r"hls_url\s*=\s*[\"']([^\"']+\.m3u8[^\"']*)", decrypted_js
        )
        if url_match:
            return url_match.group(1)
        print("[session] LV3_WRAPPED_KEY를 찾지 못했습니다.")
        return ""

    lv3_wrapped_key = lwk_match.group(1)

    # wrap_key nonce 추출
    wk_match = re.search(r"fetch\('/wrap_key\.php\?n=([^']+)'\)", decrypted_js)
    if not wk_match:
        print("[session] wrap_key nonce를 찾지 못했습니다.")
        return ""
    wk_nonce = wk_match.group(1)

    # 암호화된 HLS URL 추출
    hls_match = re.search(r'myLv3\("([^"]+)"', decrypted_js)
    if not hls_match:
        print("[session] 암호화된 HLS URL을 찾지 못했습니다.")
        return ""
    hls_cipher_hex = hls_match.group(1)

    # wrap_key API 호출
    try:
        resp = sess.get(
            f"{player_domain}/wrap_key.php?n={wk_nonce}",
            headers={"User-Agent": UA, "Referer": player_url},
            verify=False,
            timeout=10,
        )
        wk_data = resp.json()
    except Exception as e:
        print(f"[session] wrap_key 요청 실패: {e}")
        return ""

    wrap_k = wk_data.get("a", "") + wk_data.get("b", "")
    wrap_v = wk_data.get("c", "") + wk_data.get("d", "")

    if not wrap_k or not wrap_v:
        print("[session] wrap_key 데이터가 불완전합니다.")
        return ""

    # Lv3 Step 1: wrapped key 복호화 → dynKey(16B) + dynIv(16B)
    unwrapped = _aes_decrypt_hex_raw(lv3_wrapped_key, wrap_k, wrap_v)
    if not unwrapped or len(unwrapped) < 32:
        print("[session] wrapped key 복호화 실패")
        return ""

    dyn_key = unwrapped[:16].hex()
    dyn_iv = unwrapped[16:32].hex()

    # Lv3 Step 2: HLS URL 복호화
    hls_url_bytes = _aes_decrypt_hex_raw(hls_cipher_hex, dyn_key, dyn_iv)
    if not hls_url_bytes:
        print("[session] HLS URL 복호화 실패")
        return ""

    return hls_url_bytes.decode("utf-8", errors="replace").strip()


# --- AES 유틸 ---


def _xor_hex(a: str, b: str) -> str:
    length = min(len(a), len(b))
    return "".join(
        format(int(a[i : i + 2], 16) ^ int(b[i : i + 2], 16), "02x")
        for i in range(0, length, 2)
    )


def _aes_decrypt_b64(ct_b64: str, key_hex: str, iv_hex: str) -> str:
    """Base64 암호문을 AES-128-CBC로 복호화하여 문자열을 반환한다."""
    try:
        ct_bytes = base64.b64decode(ct_b64)
        result = subprocess.run(
            [
                "openssl", "enc", "-aes-128-cbc", "-d", "-nosalt",
                "-K", key_hex, "-iv", iv_hex,
            ],
            input=ct_bytes,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""


def _aes_decrypt_hex_raw(cipher_hex: str, key_hex: str, iv_hex: str) -> bytes:
    """Hex 암호문을 AES-128-CBC로 복호화하여 바이트를 반환한다."""
    try:
        ct_bytes = bytes.fromhex(cipher_hex)
        result = subprocess.run(
            [
                "openssl", "enc", "-aes-128-cbc", "-d", "-nosalt",
                "-K", key_hex, "-iv", iv_hex,
            ],
            input=ct_bytes,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return b""
