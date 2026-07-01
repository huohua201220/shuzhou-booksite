#!/usr/bin/env python3
import cgi
import html
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("DATA_DIR", str(ROOT))).resolve()
BOOK_DIR = DATA_ROOT / "books"
META_FILE = DATA_ROOT / "library.json"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "").strip()
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
CORS_ALLOW_ORIGINS = [
    item.strip()
    for item in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    if item.strip()
]
ALLOWED_EXTENSIONS = {".epub", ".pdf", ".txt", ".mobi", ".azw3", ".fb2", ".md", ".docx"}


class LibraryStore:
    def __init__(self, meta_file: Path):
        self.meta_file = meta_file
        self.lock = threading.Lock()
        self.books = self._load()

    def _load(self):
        if not self.meta_file.exists():
            return []
        try:
            return json.loads(self.meta_file.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self):
        self.meta_file.write_text(
            json.dumps(self.books, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(self):
        with self.lock:
            return sorted(self.books, key=lambda item: item["uploaded_at"], reverse=True)

    def get(self, book_id: str):
        with self.lock:
            for item in self.books:
                if item["id"] == book_id:
                    return dict(item)
        return None

    def add(self, book):
        with self.lock:
            self.books.append(book)
            self._save()

    def delete(self, book_id: str):
        with self.lock:
            before = len(self.books)
            self.books = [item for item in self.books if item["id"] != book_id]
            changed = len(self.books) != before
            if changed:
                self._save()
            return changed


store = LibraryStore(META_FILE)


def ensure_dirs():
    BOOK_DIR.mkdir(parents=True, exist_ok=True)


def origin_allowed(origin: str):
    if not origin:
        return False
    return "*" in CORS_ALLOW_ORIGINS or origin in CORS_ALLOW_ORIGINS


def request_password(handler):
    parsed = urllib.parse.urlparse(handler.path)
    query = urllib.parse.parse_qs(parsed.query)
    if "password" in query:
        return query.get("password", [""])[0]
    return handler.headers.get("X-Site-Password", "").strip()


def check_auth(handler):
    if not SITE_PASSWORD:
        return True
    return request_password(handler) == SITE_PASSWORD


def sanitize_name(filename: str):
    name = Path(filename or "book").name
    return name or "book"


def infer_title(filename: str):
    return Path(filename).stem or "未命名"


def file_path_for(book):
    return BOOK_DIR / book["stored_name"]


def load_preview(book, limit=6000):
    path = file_path_for(book)
    if not path.exists():
        return ""
    raw = path.read_bytes()[:limit]
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding).strip()
        except Exception:
            pass
    return ""


def save_upload(file_item):
    filename = sanitize_name(file_item.filename)
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError("unsupported file type")
    book_id = uuid.uuid4().hex[:12]
    stored_name = f"{book_id}{ext}"
    target = BOOK_DIR / stored_name
    size = 0
    with target.open("wb") as fh:
        while True:
            chunk = file_item.file.read(1024 * 256)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                target.unlink(missing_ok=True)
                raise ValueError("file too large")
            fh.write(chunk)
    return {
        "id": book_id,
        "title": infer_title(filename),
        "filename": filename,
        "stored_name": stored_name,
        "ext": ext,
        "size": size,
        "uploaded_at": int(time.time()),
        "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ShuzhouLite/1.0"

    def end_headers(self):
        origin = self.headers.get("Origin", "").strip()
        if origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Site-Password")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, text, status=HTTPStatus.OK):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, text, status=HTTPStatus.OK):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def unauthorized(self):
        self.send_json({"ok": False, "error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)

    def require_auth(self):
        if check_auth(self):
            return True
        self.unauthorized()
        return False

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self):
        ensure_dirs()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/healthz":
            return self.send_json({"ok": True})

        if path == "/":
            help_html = """
            <!doctype html><html lang="zh-CN"><meta charset="utf-8">
            <title>书舟后端</title>
            <body style="font-family:sans-serif;padding:24px;line-height:1.7">
            <h1>书舟后端已运行</h1>
            <p>把这个地址填到前台页面的“云书库地址”里即可。</p>
            <ul>
              <li><code>/healthz</code></li>
              <li><code>/api/books</code></li>
              <li><code>/upload</code></li>
            </ul>
            </body></html>
            """
            return self.send_html(help_html)

        if not self.require_auth():
            return

        if path == "/api/books":
            return self.send_json({"books": store.list()})

        if path.startswith("/api/read/"):
            book = store.get(path.rsplit("/", 1)[-1])
            if not book:
                return self.send_json({"ok": False, "error": "not found"}, status=HTTPStatus.NOT_FOUND)
            if book["ext"] not in {".txt", ".md"}:
                return self.send_json({"ok": False, "error": "preview unavailable"}, status=HTTPStatus.BAD_REQUEST)
            return self.send_json({"ok": True, "book": book, "preview": load_preview(book)})

        if path.startswith("/download/"):
            book = store.get(path.rsplit("/", 1)[-1])
            if not book:
                return self.send_text("not found", status=HTTPStatus.NOT_FOUND)
            file_path = file_path_for(book)
            if not file_path.exists():
                return self.send_text("not found", status=HTTPStatus.NOT_FOUND)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", book["mime_type"])
            safe_name = urllib.parse.quote(book["filename"])
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{safe_name}")
            self.send_header("Content-Length", str(file_path.stat().st_size))
            self.end_headers()
            with file_path.open("rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
            return

        self.send_text("not found", status=HTTPStatus.NOT_FOUND)

    def do_POST(self):
        ensure_dirs()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self.require_auth():
            return

        if path == "/upload":
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                },
            )
            file_item = form["book"] if "book" in form else None
            if not file_item or not getattr(file_item, "filename", ""):
                return self.send_json({"ok": False, "error": "missing file"}, status=HTTPStatus.BAD_REQUEST)
            try:
                book = save_upload(file_item)
            except ValueError as exc:
                return self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            store.add(book)
            return self.send_json({"ok": True, "book": book})

        if path.startswith("/api/delete/"):
            book_id = path.rsplit("/", 1)[-1]
            book = store.get(book_id)
            if not book:
                return self.send_json({"ok": False, "error": "not found"}, status=HTTPStatus.NOT_FOUND)
            file_path = file_path_for(book)
            if file_path.exists():
                file_path.unlink()
            store.delete(book_id)
            return self.send_json({"ok": True})

        self.send_json({"ok": False, "error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, fmt, *args):
        pass


def main():
    ensure_dirs()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Serving on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
