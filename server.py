#!/usr/bin/env python3
"""웹 동영상 다운로더 - 웹 서버"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

import requests as http_requests
import urllib3
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from downloader.html_extractor import extract_video_from_html
from downloader.session_extractor import extract_video_from_session
from downloader.browser_engine import extract_m3u8_from_page

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads"))
FINAL_DIR = os.environ.get("FINAL_DIR", "")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

tasks: dict[str, dict] = {}
downloaded_files: set[str] = set()


@app.route("/")
def index():
    return render_template("index.html")


# --- 분석 API ---

@app.route("/api/analyze", methods=["POST"])
def analyze():
    """페이지를 분석하여 다운로드 가능한 항목 목록을 반환한다."""
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL을 입력해주세요."}), 400

    # 1단계: HTML 파싱
    result = extract_video_from_html(url)

    if not result["video_urls"]:
        # 2단계: 세션 기반 추출
        result = extract_video_from_session(url)

    if not result["video_urls"]:
        # 3단계: Playwright
        result = extract_m3u8_from_page(url, wait_seconds=10)
        result["subtitle_url"] = ""
        if not result.get("video_urls") and not result.get("m3u8_urls"):
            return jsonify({"error": "동영상 URL을 찾지 못했습니다."}), 404
        if result.get("m3u8_urls"):
            result["video_urls"] = result["m3u8_urls"] + result.get("video_urls", [])
        if not result.get("headers"):
            result["headers"] = {"Referer": url}

    title = result.get("title", "video")
    headers = result.get("headers", {})
    if not headers.get("Referer"):
        headers["Referer"] = url

    # 각 동영상의 파일 크기를 HEAD 요청으로 확인
    is_hls = result.get("is_hls", False)
    items = []
    for video_url in result["video_urls"]:
        size = _get_file_size(video_url, headers)
        items.append({
            "title": title,
            "video_url": video_url,
            "size": size,
            "subtitle_url": result.get("subtitle_url", ""),
            "headers": headers,
            "is_hls": is_hls or ".m3u8" in video_url,
        })

    return jsonify({"items": items, "page_title": title})


# --- 다운로드 API ---

@app.route("/api/download", methods=["POST"])
def start_download():
    """선택된 항목의 다운로드를 시작한다."""
    data = request.get_json()
    items = (data or {}).get("items", [])
    if not items:
        return jsonify({"error": "다운로드할 항목을 선택해주세요."}), 400

    task_ids = []
    for item in items:
        task_id = str(uuid.uuid4())[:8]
        tasks[task_id] = {
            "status": "pending",
            "title": item.get("title", "video"),
            "progress": 0,
            "total_size": item.get("size", 0),
            "downloaded": 0,
            "message": "다운로드 대기 중...",
            "filename": "",
            "error": "",
        }
        thread = threading.Thread(
            target=_run_download,
            args=(task_id, item),
            daemon=True,
        )
        thread.start()
        task_ids.append(task_id)

    return jsonify({"task_ids": task_ids})


@app.route("/api/status/<task_id>")
def task_status(task_id):
    def stream():
        last_data = None
        while True:
            task = tasks.get(task_id)
            if not task:
                yield _sse({"error": "작업을 찾을 수 없습니다."})
                break

            data = {
                "status": task["status"],
                "title": task["title"],
                "progress": task["progress"],
                "total_size": task["total_size"],
                "downloaded": task["downloaded"],
                "message": task["message"],
                "filename": task["filename"],
                "error": task["error"],
            }

            if data != last_data:
                yield _sse(data)
                last_data = data.copy()

            if task["status"] in ("done", "error"):
                break

            time.sleep(0.5)

    return Response(stream(), mimetype="text/event-stream")


@app.route("/api/cancel/<task_id>", methods=["POST"])
def cancel_task(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "작업을 찾을 수 없습니다."}), 404
    if task["status"] in ("done", "error", "cancelled"):
        return jsonify({"error": "이미 종료된 작업입니다."}), 400
    task["cancelled"] = True
    return jsonify({"ok": True})


@app.route("/api/files")
def list_files():
    files = []
    for f in sorted(
        downloaded_files,
        key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)) if os.path.exists(os.path.join(DOWNLOAD_DIR, x)) else 0,
        reverse=True,
    ):
        path = os.path.join(DOWNLOAD_DIR, f)
        if os.path.isfile(path):
            files.append({
                "name": f,
                "size": os.path.getsize(path),
                "modified": os.path.getmtime(path),
            })
    return jsonify(files)


@app.route("/api/files/<path:filename>")
def download_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)


# --- 내부 함수 ---

def _get_file_size(url: str, headers: dict) -> int:
    try:
        req_headers = {"User-Agent": UA}
        req_headers.update(headers)
        resp = http_requests.head(url, headers=req_headers, verify=False, timeout=10, allow_redirects=True)
        return int(resp.headers.get("Content-Length", 0))
    except Exception:
        return 0


def _run_download(task_id: str, item: dict):
    task = tasks[task_id]
    task["status"] = "downloading"
    task["message"] = "다운로드 중..."

    video_url = item["video_url"]
    title = item.get("title", "video")
    headers = item.get("headers", {})
    subtitle_url = item.get("subtitle_url", "")

    is_hls = (
        ".m3u8" in video_url
        or "mpegurl" in video_url.lower()
        or item.get("is_hls")
    )
    if is_hls:
        success = _download_hls(task_id, video_url, title, headers)
    else:
        success = _download_file(task_id, video_url, title, headers)
    if success:
        downloaded_files.add(task["filename"])
        sub_filename = _download_subtitle_file(subtitle_url, title)
        if sub_filename:
            downloaded_files.add(sub_filename)
        _move_to_final_dir(task["filename"])
        if sub_filename:
            _move_to_final_dir(sub_filename)
        task["status"] = "done"
        task["message"] = "다운로드 완료!"
        task["progress"] = 100
    else:
        if task.get("status") != "cancelled":
            task["status"] = "error"
            task["error"] = "다운로드에 실패했습니다."


def _download_file(task_id: str, url: str, title: str, headers: dict) -> bool:
    task = tasks[task_id]

    ext = "mp4"
    url_path = url.split("?")[0]
    if "." in url_path.split("/")[-1]:
        ext = url_path.split("/")[-1].rsplit(".", 1)[-1]
        if len(ext) > 5:
            ext = "mp4"

    safe_name = re.sub(r'[\\/:*?"<>|]', '_', title).strip('. ') or "video"
    filename = f"{safe_name}.{ext}"
    output_path = os.path.join(DOWNLOAD_DIR, filename)

    counter = 1
    while os.path.exists(output_path):
        filename = f"{safe_name}_{counter}.{ext}"
        output_path = os.path.join(DOWNLOAD_DIR, filename)
        counter += 1

    task["filename"] = filename

    req_headers = {"User-Agent": UA}
    req_headers.update(headers)

    try:
        resp = http_requests.get(url, headers=req_headers, stream=True, timeout=30, verify=False)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type:
            return False

        content_length = resp.headers.get("Content-Length")
        total_size = int(content_length) if content_length else 0
        task["total_size"] = total_size

        downloaded = 0
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if task.get("cancelled"):
                    task["status"] = "cancelled"
                    task["message"] = "취소됨"
                    f.close()
                    os.remove(output_path)
                    return False
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    task["downloaded"] = downloaded
                    if total_size:
                        task["progress"] = int(downloaded * 100 / total_size)

        actual_size = os.path.getsize(output_path)
        if actual_size < 10000:
            os.remove(output_path)
            return False

        task["downloaded"] = actual_size
        task["progress"] = 100
        return True

    except Exception:
        if os.path.exists(output_path):
            os.remove(output_path)
        return False


def _download_subtitle_file(subtitle_url: str, title: str) -> str | None:
    if not subtitle_url:
        return None
    try:
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', title).strip('. ') or "video"
        ext = subtitle_url.rsplit(".", 1)[-1] if "." in subtitle_url.split("/")[-1] else "srt"
        filename = f"{safe_name}.{ext}"
        output_path = os.path.join(DOWNLOAD_DIR, filename)
        resp = http_requests.get(subtitle_url, verify=False, timeout=10)
        if resp.status_code == 200 and len(resp.content) > 10:
            with open(output_path, "wb") as f:
                f.write(resp.content)
            return filename
    except Exception:
        pass
    return None


def _move_to_final_dir(filename: str):
    """FINAL_DIR이 설정되어 있으면 파일을 이동한다."""
    if not FINAL_DIR:
        return
    src = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.isfile(src):
        return
    try:
        os.makedirs(FINAL_DIR, exist_ok=True)
        dst = os.path.join(FINAL_DIR, filename)
        shutil.copy2(src, dst)
        os.remove(src)
        print(f"[move] {filename} → {FINAL_DIR}")
    except Exception as e:
        print(f"[move] 이동 실패 ({filename}): {e}")


def _download_hls(task_id: str, url: str, title: str, headers: dict) -> bool:
    """HLS(m3u8) 스트림을 세그먼트 단위로 다운로드한다.

    세그먼트가 .gif 등 비표준 확장자이거나 AES-128 암호화된 경우,
    Python으로 직접 다운로드/복호화 후 ffmpeg로 MP4 변환한다.
    """
    task = tasks[task_id]

    safe_name = re.sub(r'[\\/:*?"<>|]', '_', title).strip('. ') or "video"
    filename = f"{safe_name}.mp4"
    output_path = os.path.join(DOWNLOAD_DIR, filename)

    counter = 1
    while os.path.exists(output_path):
        filename = f"{safe_name}_{counter}.mp4"
        output_path = os.path.join(DOWNLOAD_DIR, filename)
        counter += 1

    task["filename"] = filename
    task["message"] = "HLS 플레이리스트 분석 중..."

    req_headers = {"User-Agent": UA}
    req_headers.update(headers)

    # m3u8 다운로드
    try:
        resp = http_requests.get(url, headers=req_headers, verify=False, timeout=15)
        resp.raise_for_status()
        m3u8_text = resp.text
    except Exception:
        task["error"] = "m3u8 플레이리스트를 가져오지 못했습니다."
        return False

    # m3u8 파싱
    segments, key_info = _parse_m3u8(m3u8_text, url)
    if not segments:
        task["error"] = "HLS 세그먼트를 찾지 못했습니다."
        return False

    # 암호화 키 다운로드
    aes_key = None
    aes_iv = None
    if key_info:
        try:
            key_resp = http_requests.get(
                key_info["uri"], headers=req_headers, verify=False, timeout=10
            )
            aes_iv = key_info.get("iv")

            if len(key_resp.content) == 16:
                aes_key = key_resp.content
            else:
                # key7 JSON (7-layer 변환) → WASM 디코더로 처리
                try:
                    key_json = key_resp.json()
                    if key_json.get("total_layers"):
                        from downloader.key7_decoder import decode_key7_json
                        task["message"] = "HLS 키 디코딩 중..."
                        aes_key = decode_key7_json(key_json)
                except (ValueError, KeyError):
                    pass

            if not aes_key:
                task["error"] = "HLS 암호화 키를 디코딩하지 못했습니다."
                return False
        except Exception:
            task["error"] = "HLS 암호화 키를 가져오지 못했습니다."
            return False

    # 세그먼트 다운로드 → TS 파일로 합치기
    import tempfile
    ts_fd, ts_path = tempfile.mkstemp(suffix=".ts")
    os.close(ts_fd)

    task["message"] = "세그먼트 다운로드 중..."
    total = len(segments)
    print(f"[hls] 세그먼트 {total}개 다운로드 시작 (암호화: {'예' if aes_key else '아니오'})")

    try:
        with open(ts_path, "wb") as ts_file:
            for i, seg_url in enumerate(segments):
                if task.get("cancelled"):
                    task["status"] = "cancelled"
                    task["message"] = "취소됨"
                    os.remove(ts_path)
                    return False

                try:
                    seg_resp = http_requests.get(
                        seg_url, headers=req_headers, verify=False, timeout=30
                    )
                    seg_data = seg_resp.content

                    if aes_key:
                        iv = aes_iv if aes_iv else i.to_bytes(16, "big")
                        seg_data = _aes_decrypt_segment(seg_data, aes_key, iv)

                    ts_file.write(seg_data)
                except Exception:
                    continue

                task["progress"] = int((i + 1) * 90 / total)
                task["downloaded"] = os.path.getsize(ts_path)

        # TS → MP4 변환
        if shutil.which("ffmpeg"):
            task["message"] = "MP4 변환 중..."
            print(f"[hls] ffmpeg 변환 시작: {ts_path} → {output_path}")
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", ts_path,
                    "-c", "copy",
                    "-bsf:a", "aac_adtstoasc",
                    "-movflags", "+faststart",
                    output_path,
                ],
                capture_output=True, timeout=1800,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace")[-500:]
                print(f"[hls] ffmpeg 변환 실패 (code={result.returncode}): {stderr}")
                os.remove(ts_path)
                if os.path.exists(output_path):
                    os.remove(output_path)
                return False
            print(f"[hls] ffmpeg 변환 완료")
            os.remove(ts_path)
        else:
            # ffmpeg 없으면 TS 그대로 저장
            filename = filename.replace(".mp4", ".ts")
            output_path_ts = os.path.join(DOWNLOAD_DIR, filename)
            os.rename(ts_path, output_path_ts)
            output_path = output_path_ts
            task["filename"] = filename

        if not os.path.exists(output_path):
            return False

        actual_size = os.path.getsize(output_path)
        if actual_size < 10000:
            os.remove(output_path)
            return False

        task["downloaded"] = actual_size
        task["total_size"] = actual_size
        task["progress"] = 100
        return True

    except Exception:
        for p in [ts_path, output_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return False


def _parse_m3u8(content: str, base_url: str) -> tuple[list[str], dict | None]:
    """m3u8를 파싱하여 세그먼트 URL 리스트와 암호화 키 정보를 반환한다."""
    from urllib.parse import urlparse, urljoin

    parsed_base = urlparse(base_url)
    base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

    segments = []
    key_info = None

    for line in content.split("\n"):
        line = line.strip()

        # 암호화 키 정보
        if line.startswith("#EXT-X-KEY:"):
            match_uri = re.search(r'URI="([^"]+)"', line)
            match_iv = re.search(r'IV=0x([0-9a-fA-F]+)', line)
            if match_uri:
                key_uri = match_uri.group(1)
                if key_uri.startswith("/"):
                    key_uri = base_origin + key_uri
                elif not key_uri.startswith("http"):
                    key_uri = urljoin(base_url, key_uri)
                key_info = {"uri": key_uri}
                if match_iv:
                    key_info["iv"] = bytes.fromhex(match_iv.group(1))

        # 세그먼트 URL
        elif line and not line.startswith("#"):
            if not line.startswith("http"):
                line = urljoin(base_url, line)
            segments.append(line)

    return segments, key_info


def _aes_decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC로 세그먼트를 복호화한다."""
    try:
        result = subprocess.run(
            [
                "openssl", "enc", "-aes-128-cbc", "-d", "-nosalt",
                "-K", key.hex(), "-iv", iv.hex(),
            ],
            input=data, capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return data


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=True)
