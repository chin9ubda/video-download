#!/usr/bin/env python3
"""웹 동영상 다운로더 - 웹 서버"""

import os
import re
import threading
import time
import uuid

import requests as http_requests
import urllib3
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from downloader.html_extractor import extract_video_from_html
from downloader.browser_engine import extract_m3u8_from_page

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# 진행 중인 작업 저장소
tasks: dict[str, dict] = {}


# --- 웹 페이지 ---

@app.route("/")
def index():
    return render_template("index.html")


# --- API ---

@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL을 입력해주세요."}), 400

    task_id = str(uuid.uuid4())[:8]
    tasks[task_id] = {
        "status": "pending",
        "url": url,
        "title": "",
        "progress": 0,
        "total_size": 0,
        "downloaded": 0,
        "message": "분석 대기 중...",
        "filename": "",
        "error": "",
    }

    thread = threading.Thread(target=_run_download, args=(task_id, url), daemon=True)
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/status/<task_id>")
def task_status(task_id):
    """SSE로 실시간 진행 상태를 스트리밍한다."""
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


@app.route("/api/files")
def list_files():
    files = []
    for f in sorted(os.listdir(DOWNLOAD_DIR), key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True):
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


# --- 다운로드 로직 ---

def _run_download(task_id: str, url: str):
    task = tasks[task_id]
    task["status"] = "analyzing"
    task["message"] = "페이지 분석 중..."

    # 1단계: HTML 파싱
    result = extract_video_from_html(url)

    if not result["video_urls"]:
        # 2단계: Playwright 브라우저
        task["message"] = "브라우저로 동영상 URL 추출 중..."
        result = extract_m3u8_from_page(url, wait_seconds=10)
        result["subtitle_url"] = ""
        if not result.get("video_urls") and not result.get("m3u8_urls"):
            task["status"] = "error"
            task["error"] = "동영상 URL을 찾지 못했습니다."
            return
        # m3u8 URL을 video_urls에 합침
        if result.get("m3u8_urls"):
            result["video_urls"] = result["m3u8_urls"] + result.get("video_urls", [])
        if not result.get("headers"):
            result["headers"] = {"Referer": url}

    title = result.get("title", "video")
    task["title"] = title
    headers = result.get("headers", {})
    subtitle_url = result.get("subtitle_url", "")

    if not headers.get("Referer"):
        headers["Referer"] = url

    # 다운로드
    for video_url in result["video_urls"]:
        task["status"] = "downloading"
        task["message"] = "다운로드 중..."
        success = _download_file(task_id, video_url, title, headers)
        if success:
            # 자막 다운로드
            _download_subtitle_file(subtitle_url, title)
            task["status"] = "done"
            task["message"] = "다운로드 완료!"
            task["progress"] = 100
            return

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

    req_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
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


def _download_subtitle_file(subtitle_url: str, title: str):
    if not subtitle_url:
        return
    try:
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', title).strip('. ') or "video"
        ext = subtitle_url.rsplit(".", 1)[-1] if "." in subtitle_url.split("/")[-1] else "srt"
        output_path = os.path.join(DOWNLOAD_DIR, f"{safe_name}.{ext}")
        resp = http_requests.get(subtitle_url, verify=False, timeout=10)
        if resp.status_code == 200 and len(resp.content) > 10:
            with open(output_path, "wb") as f:
                f.write(resp.content)
    except Exception:
        pass


def _sse(data: dict) -> str:
    import json
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
