"""Serve the local Facebook scraper UI and loopback-only Playwright API."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
VENV_ROOT = HERE / ".venv"
VENV_PYTHON = VENV_ROOT / "Scripts" / "python.exe"
if VENV_PYTHON.is_file() and Path(sys.prefix).resolve() != VENV_ROOT.resolve():
    print(f"Starting Facebook Scraper with project environment: {VENV_PYTHON}", flush=True)
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

from dotenv import load_dotenv

from local_store import (
    init_store,
    known_group_aliases,
    known_post_keys,
    load_saved_data,
    load_saved_changes,
    normalize_post_url,
    register_source_aliases,
    save_post,
)
from local_throttle import cooldown_status,pause_message,setting


DIST = HERE / "dist"
HOST = "127.0.0.1"
PORT = int(os.environ.get("GROUPPULSE_PORT", "4173"))
MAX_REQUEST_BYTES = 16_384

load_dotenv(HERE / ".env")


@dataclass
class Job:
    job_id: str
    source_url: str = ""
    resume_history: bool = True
    status: str = "queued"
    progress: int = 0
    message: str = "Đang chuẩn bị…"
    posts: list[dict] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can let two servers bind the same address, serving
    # different versions unpredictably. Reserve this local API port instead.
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


def credentials_configured() -> bool:
    return bool(os.getenv("FACEBOOK_EMAIL", "").strip() and os.getenv("FACEBOOK_PASSWORD", ""))


def valid_facebook_source_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and (hostname == "facebook.com" or hostname.endswith(".facebook.com"))
            and bool(parsed.path and parsed.path != "/")
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False


def job_payload(job: Job, include_data: bool = True) -> dict:
    payload = {
        "id": job.job_id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "sourceUrl": job.source_url,
        "resumeHistory": job.resume_history,
        "createdAt": job.created_at,
        "error": job.error,
    }
    if include_data:
        payload.update(posts=job.posts,comments=job.comments)
    return payload


def run_scrape(job: Job, data: dict) -> None:
    source_url = data["sourceUrl"]
    try:
        stored_post_keys = known_post_keys(source_url)
        if not credentials_configured():
            raise RuntimeError("Thiếu FACEBOOK_EMAIL hoặc FACEBOOK_PASSWORD trong web/.env.")

        from local_scraper import scrape_facebook_posts

        def report(status: str, progress: int, message: str) -> None:
            with JOBS_LOCK:
                job.status = status
                job.progress = max(job.progress,progress)
                job.message = message

        def source_ready(canonical_url: str, aliases: list[str]) -> None:
            register_source_aliases(source_url, aliases, canonical_url)
            stored_post_keys.update(known_post_keys(source_url))

        def save_story(post: dict, comments: list[dict]) -> None:
            save_post(source_url, post, comments)

        scrape_facebook_posts(
            email=os.environ["FACEBOOK_EMAIL"],
            password=os.environ["FACEBOOK_PASSWORD"],
            source_url=source_url,
            include_replies=data["includeReplies"],
            progress=report,
            cancelled=job.cancel_event.is_set,
            known_post_urls=stored_post_keys,
            known_group_keys=known_group_aliases(source_url),
            on_source=source_ready,
            on_post=save_story,
            collect_results=False,
            resume_history=data.get('resumeHistory',True),
        )
        saved = load_saved_data(source_url)
        with JOBS_LOCK:
            job.posts = saved["posts"]
            job.comments = saved["comments"]
            job.progress = 100 if not job.cancel_event.is_set() else job.progress
            job.status = "cancelled" if job.cancel_event.is_set() else "completed"
            job.message = "Đã dừng; giữ lại dữ liệu đã thu thập." if job.cancel_event.is_set() else "Đã hoàn tất scrape."
    except Exception as error:
        try:
            saved = load_saved_data(source_url)
        except Exception:
            saved = {"posts": [], "comments": []}
        with JOBS_LOCK:
            job.posts = saved["posts"]
            job.comments = saved["comments"]
            job.status = "failed"
            job.error = str(error)[:500]
            job.message = "Scrape không hoàn tất."


class Handler(SimpleHTTPRequestHandler):
    server_version = "FacebookScraperLocal/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST), **kwargs)

    def end_headers(self) -> None:
        if not urlparse(self.path).path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format_string: str, *args) -> None:
        # Keep source URLs and browser/API details out of the console log.
        print(f"[{self.log_date_time_string()}] {self.command} {self.path.split('?')[0]}")

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def request_origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"} and parsed.port == PORT

    def read_json(self, max_bytes: int = MAX_REQUEST_BYTES) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Content-Length không hợp lệ.") from None
        if length <= 0 or length > max_bytes:
            raise ValueError("Kích thước yêu cầu không hợp lệ.")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Body phải là JSON hợp lệ.") from None
        if not isinstance(value, dict):
            raise ValueError("Body phải là một object JSON.")
        return value

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        attachment = re.fullmatch(r'/api/media/([a-f0-9]{64}\.(?:jpg|png|webp|gif|img))',path)
        if attachment:
            import local_store
            filename=local_store.DATA_DIR/'media'/attachment.group(1)
            if not filename.is_file():
                self.send_json(404,{"error":"Không tìm thấy ảnh đã lưu."})
                return
            self.send_response(200)
            self.send_header('Content-Type',mimetypes.guess_type(filename.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length',str(filename.stat().st_size))
            self.send_header('Cache-Control','private, max-age=86400, immutable')
            self.end_headers()
            with filename.open('rb') as data:
                import shutil
                shutil.copyfileobj(data,self.wfile)
            return
        if path == "/api/health":
            try:
                import onnxruntime as ort

                ocr_providers = ort.get_available_providers()
            except Exception:
                ocr_providers = []
            self.send_json(200, {
                "localBackend": True,
                "processId": os.getpid(),
                "scrapeEngine": "durable-pipeline-v1",
                "credentialsConfigured": credentials_configured(),
                "unlimitedScrape": True,
                "ocrGpuAvailable": False,
                "ocrDevice": "CPUExecutionProvider",
                "ocrProviders": ocr_providers,
                "requestIntervalSeconds":setting('FACEBOOK_REQUEST_INTERVAL_SECONDS',0,60),
                "facebookCooldown":cooldown_status(),
            })
            return
        if path in {"/api/data", "/api/data/changes"}:
            query = parse_qs(urlparse(self.path).query)
            source_url = query.get("sourceUrl", [""])[0].strip()
            if len(source_url) > 2_048 or not valid_facebook_source_url(source_url):
                self.send_json(400, {"error": "Invalid Facebook source URL."})
                return
            if path == '/api/data/changes':
                try:
                    revision = query.get('afterRevision', [''])[0]
                    if not re.fullmatch(r'[0-9]{1,19}', revision):
                        raise ValueError('Invalid saved-data revision')
                    data = load_saved_changes(source_url, int(revision))
                except ValueError:
                    self.send_json(400, {'error': 'Invalid saved-data revision.'})
                    return
            else:
                data = load_saved_data(source_url)
            self.send_json(200, data)
            return
        if path == '/api/jobs/active':
            source = parse_qs(urlparse(self.path).query).get('sourceUrl',[''])[0]
            with JOBS_LOCK:
                active = [job for job in JOBS.values() if job.status in {'queued','running'} and
                          (not source or normalize_post_url(job.source_url)==normalize_post_url(source))]
                job = max(active,key=lambda item:item.created_at) if active else None
                payload = job_payload(job,include_data=False) if job else None
            self.send_json(200,{'job':payload})
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9-]{36})", path)
        if match:
            with JOBS_LOCK:
                job = JOBS.get(match.group(1))
                summary = parse_qs(urlparse(self.path).query).get('summary') == ['1']
                payload = job_payload(job,include_data=not summary) if job else None
            if payload is None:
                self.send_json(404, {"error": "Không tìm thấy lần chạy này."})
            else:
                self.send_json(200, payload)
            return
        if path.startswith("/api/"):
            self.send_json(404, {"error": "API không tồn tại."})
            return
        super().do_GET()

    def do_POST(self) -> None:
        if not self.request_origin_allowed():
            self.send_json(403, {"error": "Chỉ chấp nhận yêu cầu từ trang local này."})
            return
        path = urlparse(self.path).path
        if path == "/api/jobs":
            self.start_job()
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9-]{36})/cancel", path)
        if match:
            with JOBS_LOCK:
                job = JOBS.get(match.group(1))
                if job and job.status in {"queued", "running"}:
                    job.cancel_event.set()
                    job.message = "Đang dừng sau bước hiện tại…"
            if not job:
                self.send_json(404, {"error": "Không tìm thấy lần chạy này."})
            else:
                self.send_json(200, job_payload(job))
            return
        self.send_json(404, {"error": "API không tồn tại."})

    def start_job(self) -> None:
        try:
            data = self.read_json()
            source_url = str(data.get("sourceUrl", "")).strip()
            include_replies = bool(data.get("includeReplies", True))
            resume_history = bool(data.get('resumeHistory',True))
        except (ValueError, TypeError) as error:
            self.send_json(400, {"error": str(error)})
            return
        if len(source_url) > 2_048 or not valid_facebook_source_url(source_url):
            self.send_json(400, {"error": "Nhập URL HTTPS hợp lệ của một group, page hoặc profile Facebook."})
            return
        if not credentials_configured():
            self.send_json(503, {"error": "Thiếu FACEBOOK_EMAIL hoặc FACEBOOK_PASSWORD trong web/.env."})
            return

        try:
            pause=cooldown_status()
        except Exception:
            self.send_json(503,{"error":"Không đọc được trạng thái tạm nghỉ; chưa gửi yêu cầu Facebook."})
            return
        if pause['remaining']:
            self.send_json(429,{'error':pause_message(pause['until']),'retryAfterSeconds':pause['remaining'],
                                'retryAt':pause['until']})
            return

        now = time.time()
        with JOBS_LOCK:
            for job_id in list(JOBS):
                if now - JOBS[job_id].created_at > 3_600 and JOBS[job_id].status not in {"queued", "running"}:
                    del JOBS[job_id]
            if any(job.status in {"queued", "running"} for job in JOBS.values()):
                self.send_json(409, {"error": "Đang có một lần scrape khác chạy. Hãy đợi hoặc dừng lần đó."})
                return
            job = Job(job_id=str(uuid.uuid4()),source_url=source_url,resume_history=resume_history)
            JOBS[job.job_id] = job
        threading.Thread(target=run_scrape, args=(job, {
            "sourceUrl": source_url,
            "includeReplies": include_replies,
            "resumeHistory": resume_history,
        }), daemon=True).start()
        self.send_json(202, {"id": job.job_id, "status": job.status})



def main() -> None:
    if not DIST.is_dir():
        raise SystemExit(f"Không tìm thấy thư mục giao diện: {DIST}")
    init_store()
    server = LocalHTTPServer((HOST, PORT), Handler)
    print(f"Facebook Scraper local server at http://{HOST}:{PORT}")
    print("Only this computer can access the Playwright API. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nFacebook Scraper local server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
