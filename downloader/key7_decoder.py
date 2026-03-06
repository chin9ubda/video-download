"""key7 JSON 7-layer WASM 디코더.

key7 엔드포인트가 16바이트 키 대신 JSON(7개 변환 레이어)을 반환할 때,
level3.js의 WASM(decode_level7)을 Playwright로 실행하여 실제 AES 키를 복원한다.
"""

import json
import os
import time

_LEVEL3_JS_PATH = os.path.join(os.path.dirname(__file__), "level3.js")
_PLAYER_DOMAIN = "https://player.bunny-frame.online"


def decode_key7_json(key_data: dict) -> bytes | None:
    """key7 JSON에서 16바이트 AES 키를 추출한다.

    Args:
        key_data: key7 엔드포인트에서 받은 JSON (layers, encrypted_key 등)

    Returns:
        16바이트 AES 키 또는 None
    """
    if not os.path.exists(_LEVEL3_JS_PATH):
        print("[key7] level3.js 파일이 없습니다.")
        return None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[key7] playwright가 설치되어 있지 않습니다.")
        return None

    with open(_LEVEL3_JS_PATH, "r") as f:
        level3_code = f.read()

    # WASM exports와 decode 함수를 전역에 노출하는 패치
    patched = level3_code.replace(
        "r=A.instance.exports,L=null,f=null,b=!0",
        "r=A.instance.exports,L=null,f=null,b=!0,window.__wasmReady=!0",
    )
    patched = patched.replace(
        "function O(A){",
        "window.__decodeKeyFn=O;function O(A){",
    )

    key_json_str = json.dumps(key_data)
    key_hex = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()

        def handle_route(route):
            if "/decode_test" in route.request.url:
                html = (
                    "<!DOCTYPE html><html><head></head><body>"
                    "<script>window.__wasmReady=false;</script>"
                    f"<script>{patched}</script>"
                    "</body></html>"
                )
                route.fulfill(status=200, content_type="text/html", body=html)
            else:
                route.continue_()

        page.route("**/*", handle_route)

        try:
            page.goto(f"{_PLAYER_DOMAIN}/decode_test", timeout=15000)
        except Exception as e:
            print(f"[key7] 페이지 로드 오류: {e}")
            browser.close()
            return None

        # WASM 초기화 대기 (최대 30초)
        for _ in range(30):
            time.sleep(1)
            if page.evaluate("() => window.__wasmReady === true"):
                break
        else:
            print("[key7] WASM 초기화 타임아웃")
            browser.close()
            return None

        key_hex = page.evaluate(
            """(keyJsonStr) => {
            try {
                const keyData = JSON.parse(keyJsonStr);
                if (typeof window.__decodeKeyFn === 'function') {
                    const r = window.__decodeKeyFn(keyData);
                    if (r) {
                        const arr = r instanceof Uint8Array ? r : new Uint8Array(r);
                        return Array.from(arr)
                            .map(b => b.toString(16).padStart(2, '0'))
                            .join('');
                    }
                }
            } catch(e) {}
            return null;
        }""",
            key_json_str,
        )

        browser.close()

    if key_hex and len(key_hex) == 32:
        print(f"[key7] AES 키 추출 성공")
        return bytes.fromhex(key_hex)

    print("[key7] 키 디코딩 실패")
    return None
