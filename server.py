#!/usr/bin/env python3
"""웹 동영상 다운로더 - 웹 서버"""

import json
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
        # 2단계: Playwright
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
    items = []
    for video_url in result["video_urls"]:
        size = _get_file_size(video_url, headers)
        items.append({
            "title": title,
            "video_url": video_url,
            "size": size,
            "subtitle_url": result.get("subtitle_url", ""),
            "headers": headers,
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

    success = _download_file(task_id, video_url, title, headers)
    if success:
        downloaded_files.add(task["filename"])
        sub_filename = _download_subtitle_file(subtitle_url, title)
        if sub_filename:
            downloaded_files.add(sub_filename)
        task["status"] = "done"
        task["message"] = "다운로드 완료!"
        task["progress"] = 100
    else:
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


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
