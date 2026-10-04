from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, parse_qsl, urlencode, urlunparse

import requests
from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.exceptions import BadRequest
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from .history_store import HistoryStore
    from .job_files import safe_owned_path
    from .job_runner import run_parse_job
    from .presets import PRESETS, get_preset
except ImportError:  # Docker runs modules from /app.
    from history_store import HistoryStore
    from job_files import safe_owned_path
    from job_runner import run_parse_job
    from presets import PRESETS, get_preset

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", DATA_DIR / "downloads")).resolve()
COOKIE_FILE = Path(os.environ.get("YUANBAO_COOKIE_FILE", DATA_DIR / "yuanbao.tencent.com_cookies.txt")).resolve()
SESSION_FILE = Path(os.environ.get("YUANBAO_SESSION_FILE", DATA_DIR / "yuanbao_session.json")).resolve()
FUNCTION_FILE = Path(os.environ.get("BROWSERLESS_FUNCTION_FILE", APP_DIR / "browserless_function.js")).resolve()
SEQUENCE_FILE = DATA_DIR / "sequence.txt"

BROWSERLESS_URL = os.environ.get("BROWSERLESS_URL", "http://browserless:3000").rstrip("/")
BROWSERLESS_TOKEN = os.environ.get("BROWSERLESS_TOKEN", "")
WEBUI_USERNAME = os.environ.get("WEBUI_USERNAME", "admin")
WEBUI_PASSWORD = os.environ.get("WEBUI_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
BROWSER_TIMEOUT = int(os.environ.get("BROWSER_TIMEOUT", "180"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "300"))
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "false").strip().lower() in {"1", "true", "yes", "on"}
PROXY_HOPS = max(1, int(os.environ.get("PROXY_HOPS", "1")))
MAX_PARSE_ATTEMPTS = min(3, max(1, int(os.environ.get("MAX_PARSE_ATTEMPTS", "3"))))
RETRY_SLEEPER = time.sleep
HISTORY_STORE: HistoryStore | None = None

USER_AGENT = os.environ.get(
    "BROWSER_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)

PROCESS_LOCK = threading.Lock()

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
if TRUST_PROXY_HEADERS:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=PROXY_HOPS,
        x_proto=PROXY_HOPS,
        x_host=PROXY_HOPS,
        x_port=PROXY_HOPS,
        x_prefix=PROXY_HOPS,
    )


class AppError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 500, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


def valid_input_url(value: str) -> str:
    value = (value or "").strip()
    try:
        p = urlparse(value)
    except Exception as exc:
        raise AppError("INVALID_URL", "URL 格式不正确", 400) from exc
    allowed = {"weixin.qq.com", "www.weixin.qq.com", "channels.weixin.qq.com"}
    if p.scheme not in {"http", "https"} or p.hostname not in allowed:
        raise AppError("INVALID_URL", "仅支持微信视频号 weixin.qq.com / channels.weixin.qq.com 链接", 400)
    return value


def load_cookies() -> list[dict[str, Any]]:
    if not COOKIE_FILE.exists():
        raise AppError("COOKIE_FILE_MISSING", f"Cookie 文件不存在：{COOKIE_FILE}", 503)

    out: list[dict[str, Any]] = []
    now = int(time.time())
    for raw in COOKIE_FILE.read_text("utf-8", errors="replace").splitlines():
        line = raw.strip()
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            continue

        domain, _, path, secure, expires, name = parts[:6]
        value = "\t".join(parts[6:])
        domain_check = domain.lstrip(".").lower()
        if not ("yuanbao.tencent.com" == domain_check or "yuanbao.tencent.com".endswith("." + domain_check)):
            continue

        try:
            exp = int(expires or "0")
        except ValueError:
            exp = 0
        if exp and exp < now:
            continue

        item: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
            "httpOnly": http_only,
        }
        if exp:
            item["expires"] = exp
        out.append(item)

    if not out:
        raise AppError("COOKIE_EMPTY", "Cookie 文件没有可用于元宝的有效 Cookie", 503)
    return out


def load_session_state() -> dict[str, Any]:
    if not SESSION_FILE.exists():
        return {}
    try:
        value = json.loads(SESSION_FILE.read_text("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        raise AppError("SESSION_STATE_INVALID", f"元宝 session 状态文件无法读取：{exc}", 503) from exc


def browserless_function(source: str, context: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
    url = f"{BROWSERLESS_URL}/function"
    params: dict[str, Any] = {"timeout": int((timeout or BROWSER_TIMEOUT) * 1000)}
    if BROWSERLESS_TOKEN:
        params["token"] = BROWSERLESS_TOKEN
    try:
        r = requests.post(
            url,
            params=params,
            json={"code": source, "context": context},
            timeout=timeout or BROWSER_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AppError("BROWSERLESS_UNREACHABLE", f"Browserless 请求失败：{exc}", 503) from exc

    if r.status_code != 200:
        raise AppError(
            "BROWSERLESS_ERROR",
            f"Browserless 返回 HTTP {r.status_code}",
            502,
            r.text[-3000:],
        )
    try:
        data = r.json()
    except ValueError as exc:
        raise AppError("BROWSERLESS_BAD_JSON", "Browserless 返回内容不是 JSON", 502, r.text[-3000:]) from exc
    if isinstance(data, dict) and data.get("error"):
        raise AppError("BROWSERLESS_SCRIPT_ERROR", str(data.get("error")), 502, data)
    if isinstance(data, dict) and isinstance(data.get("data"), dict) and not data.get("yuanbaoContent"):
        data = data["data"]
    if not isinstance(data, dict):
        raise AppError("BROWSERLESS_BAD_RESULT", "Browserless 返回格式异常", 502, data)
    return data


def analyze_with_yuanbao(input_url: str, prompt: str | None) -> dict[str, Any]:
    if not FUNCTION_FILE.exists():
        raise AppError("FUNCTION_FILE_MISSING", f"Browserless 函数文件不存在：{FUNCTION_FILE}", 503)
    request_text = input_url if not prompt else f"{input_url}\n\n{prompt.strip()}"
    context = {
        "requestText": request_text,
        "cookies": load_cookies(),
        "session": load_session_state(),
    }
    result = browserless_function(FUNCTION_FILE.read_text("utf-8"), context, timeout=BROWSER_TIMEOUT)
    content = str(result.get("yuanbaoContent") or "").strip()
    preview_url = str(result.get("previewUrl") or "").strip()
    direct_url = str(result.get("directUrl") or "").strip()
    if not content:
        raise AppError("YUANBAO_EMPTY_RESPONSE", "元宝回复为空", 502, result)
    if not preview_url:
        raise AppError("YUANBAO_PREVIEW_URL_MISSING", "没有解析到视频号完整链接", 502, result)
    if not direct_url:
        raise AppError("VIDEO_URL_MISSING", "没有解析到真实视频 URL", 502, result)
    return {
        "content": content,
        "rawText": str(result.get("rawContent") or ""),
        "previewUrl": preview_url,
        "directUrl": direct_url,
        "card": {
            "title": str(result.get("title") or "").strip(),
            "source": str(result.get("source") or "").strip(),
            "coverUrl": str(result.get("coverUrl") or "").strip(),
            "url": input_url,
        },
    }


def next_id() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    current = 0
    if SEQUENCE_FILE.exists():
        try:
            current = int(SEQUENCE_FILE.read_text("utf-8").strip() or "0")
        except Exception:
            current = 0
    if current <= 0:
        for path in DOWNLOAD_DIR.glob("wxv_*.*"):
            m = re.fullmatch(r"wxv_(\d+)\.(?:txt|md|mp4)", path.name)
            if m:
                current = max(current, int(m.group(1)))
    current += 1
    tmp = SEQUENCE_FILE.with_suffix(".tmp")
    tmp.write_text(str(current), "utf-8")
    os.replace(tmp, SEQUENCE_FILE)
    return f"wxv_{current:06d}"


def save_text(job_id: str, input_url: str, prompt: str | None, content: str) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = DOWNLOAD_DIR / f"{job_id}.txt"
    parts = [f"ID: {job_id}", f"INPUT_URL: {input_url}"]
    if prompt:
        parts += ["", "PROMPT:", prompt]
    parts += ["", "YUANBAO_CONTENT:", content.strip(), ""]
    path.write_text("\n".join(parts), "utf-8")
    return path


def download_video(job_id: str, direct_url: str, referer: str) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    template = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--user-agent", USER_AGENT,
        "--referer", referer,
        "--print", "after_move:filepath",
        "-o", template,
        direct_url,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise AppError("DOWNLOAD_TIMEOUT", "视频下载超时", 504) from exc

    if p.returncode != 0:
        raise AppError("DOWNLOAD_FAILED", "yt-dlp 下载失败", 502, p.stderr[-3000:])

    lines = [x.strip() for x in p.stdout.splitlines() if x.strip()]
    path = Path(lines[-1]) if lines else None
    if not path or not path.exists() or path.stat().st_size <= 0:
        candidates = sorted(DOWNLOAD_DIR.glob(f"{job_id}.*"), key=lambda x: x.stat().st_mtime, reverse=True)
        path = candidates[0] if candidates else None
    if not path or not path.exists() or path.stat().st_size <= 0:
        raise AppError("DOWNLOAD_VERIFY_FAILED", "下载命令结束，但未找到有效视频文件", 502)
    return path


def media_info(path: Path) -> dict[str, Any] | None:
    try:
        p = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration,format_name",
                "-show_entries", "stream=codec_type,codec_name,width,height",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if p.returncode == 0:
            return json.loads(p.stdout)
    except Exception:
        pass
    return None


def _first_forwarded(name: str, fallback: str = "") -> str:
    raw = request.headers.get(name, "")
    if not raw:
        return fallback
    values = [part.strip() for part in raw.split(",") if part.strip()]
    hops = max(1, int(PROXY_HOPS))
    if len(values) < hops:
        return fallback
    return values[-hops]


def _trusted_prefix() -> str:
    if not TRUST_PROXY_HEADERS:
        return ""
    prefix = _first_forwarded("X-Forwarded-Prefix", request.script_root or "")
    if not prefix or prefix == "/":
        return ""
    return "/" + prefix.strip("/")


def external_base() -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip("/")
    if TRUST_PROXY_HEADERS:
        scheme = _first_forwarded("X-Forwarded-Proto", request.scheme)
        host = _first_forwarded("X-Forwarded-Host", request.host)
        return f"{scheme}://{host}{_trusted_prefix()}"
    return f"{request.scheme}://{request.host}"


def build_file_links(name: str) -> dict[str, str]:
    encoded = quote(Path(name).name)
    prefix = _trusted_prefix() if not PUBLIC_BASE_URL else ""
    download_path = f"{prefix}/files/{encoded}" if prefix else f"/files/{encoded}"
    return {
        "download_path": download_path,
        "download_url": f"{external_base()}/files/{encoded}",
    }


def make_file_url(name: str) -> str:
    return build_file_links(name)["download_url"]


def get_history_store() -> HistoryStore:
    global HISTORY_STORE
    if HISTORY_STORE is None:
        HISTORY_STORE = HistoryStore(DATA_DIR / "history.db")
    return HISTORY_STORE


def serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": job["id"],
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "input_url": job.get("input_url"),
        "prompt": job.get("prompt"),
        "preset_id": job.get("preset_id"),
        "preset_name": job.get("preset_name"),
        "output_format": job.get("output_format"),
        "download_requested": job.get("download_requested"),
        "status": job.get("status"),
        "attempts_used": job.get("attempts_used", 0),
        "max_attempts": job.get("max_attempts", MAX_PARSE_ATTEMPTS),
        "retry_errors": job.get("retry_errors", []),
        "error_code": job.get("error_code"),
        "error_message": job.get("error_message"),
        "content": job.get("yuanbao_content") or "",
        "elapsed_ms": job.get("elapsed_ms"),
        "yuanbao": {
            "preview_url": job.get("preview_url"),
        },
    }
    text_filename = job.get("text_filename")
    if text_filename:
        text_links = build_file_links(text_filename)
        result["text"] = {
            "filename": text_filename,
            "bytes": job.get("text_bytes"),
            **text_links,
        }
    else:
        result["text"] = {"filename": None, "bytes": None, "download_path": None, "download_url": None}
    video_filename = job.get("video_filename")
    if video_filename:
        video_links = build_file_links(video_filename)
        video_path = safe_owned_path(DOWNLOAD_DIR, video_filename)
        result["video"] = {
            "source_direct_url": job.get("source_direct_url"),
            "downloaded": True,
            "filename": video_filename,
            "bytes": job.get("video_bytes"),
            **video_links,
            "media": media_info(video_path) if video_path and video_path.exists() else None,
        }
    else:
        result["video"] = {
            "source_direct_url": job.get("source_direct_url"),
            "downloaded": False,
            "filename": None,
            "bytes": None,
            "download_path": None,
            "download_url": None,
            "media": None,
        }
    return result


def failed_job_status(job: dict[str, Any]) -> int:
    code = str(job.get("error_code") or "")
    if code in {"YUANBAO_LOGIN_REQUIRED", "UNAUTHORIZED"}:
        return 401
    if code in {"BROWSERLESS_UNREACHABLE", "COOKIE_FILE_MISSING", "COOKIE_EMPTY"}:
        return 503
    return 502


def basic_credentials() -> tuple[str, str] | None:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(auth.split(None, 1)[1]).decode("utf-8")
        return tuple(raw.split(":", 1))  # type: ignore[return-value]
    except Exception:
        return None


def is_authorized() -> bool:
    if session.get("logged_in") is True:
        return True
    creds = basic_credentials()
    return bool(creds and secrets.compare_digest(creds[0], WEBUI_USERNAME) and secrets.compare_digest(creds[1], WEBUI_PASSWORD))


def api_or_session_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if is_authorized():
            return fn(*args, **kwargs)
        if request.path.startswith("/api/") or request.path.startswith("/files/"):
            return jsonify({"ok": False, "error": {"code": "UNAUTHORIZED", "message": "需要登录"}}), 401
        return redirect(url_for("login"))
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if secrets.compare_digest(u, WEBUI_USERNAME) and secrets.compare_digest(p, WEBUI_PASSWORD):
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "账号或密码错误"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@api_or_session_required
def index():
    return render_template("index.html")


@app.route("/history")
@api_or_session_required
def history_page():
    return render_template("history.html")


@app.route("/health")
def health():
    browserless = False
    detail = ""
    try:
        params = {"token": BROWSERLESS_TOKEN} if BROWSERLESS_TOKEN else {}
        r = requests.get(f"{BROWSERLESS_URL}/pressure", params=params, timeout=5)
        browserless = r.status_code == 200
        detail = r.text[:300]
    except Exception as exc:
        detail = str(exc)
    return jsonify({
        "ok": browserless and COOKIE_FILE.exists(),
        "browserless": browserless,
        "cookie_file": COOKIE_FILE.exists(),
        "busy": PROCESS_LOCK.locked(),
        "detail": detail,
    }), (200 if browserless and COOKIE_FILE.exists() else 503)


@app.route("/files/<path:name>")
@api_or_session_required
def files(name: str):
    clean = Path(name).name
    return send_from_directory(DOWNLOAD_DIR, clean, as_attachment=True)


@app.route("/api/presets", methods=["GET"])
@api_or_session_required
def api_presets():
    return jsonify({"ok": True, "presets": PRESETS})


def _valid_job_id(job_id: str) -> bool:
    return bool(re.fullmatch(r"wxv_\d{6}", job_id or ""))


@app.route("/api/history", methods=["GET"])
@api_or_session_required
def api_history():
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    jobs = [serialize_job(job) for job in get_history_store().list_jobs(limit=limit)]
    return jsonify({"ok": True, "jobs": jobs})


@app.route("/api/history/<job_id>", methods=["GET"])
@api_or_session_required
def api_history_detail(job_id: str):
    if not _valid_job_id(job_id):
        return jsonify({"ok": False, "error": {"code": "INVALID_JOB_ID", "message": "任务 ID 格式不正确"}}), 400
    job = get_history_store().get_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": {"code": "NOT_FOUND", "message": "历史记录不存在"}}), 404
    return jsonify({"ok": True, "job": serialize_job(job)})


@app.route("/api/history/<job_id>", methods=["DELETE"])
@api_or_session_required
def api_history_delete(job_id: str):
    if not _valid_job_id(job_id):
        return jsonify({"ok": False, "error": {"code": "INVALID_JOB_ID", "message": "任务 ID 格式不正确"}}), 400
    store = get_history_store()
    job = store.get_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": {"code": "NOT_FOUND", "message": "历史记录不存在"}}), 404
    deleted_files: list[str] = []
    for key in ("text_filename", "video_filename"):
        path = safe_owned_path(DOWNLOAD_DIR, job.get(key))
        if path and path.exists():
            try:
                path.unlink()
                deleted_files.append(path.name)
            except FileNotFoundError:
                pass
    store.delete_job(job_id)
    return jsonify({"ok": True, "id": job_id, "deleted_files": deleted_files})


@app.route("/api/parse", methods=["POST"])
@api_or_session_required
def api_parse():
    try:
        body = request.get_json(force=True, silent=False) or {}
        input_url = valid_input_url(str(body.get("url") or ""))
        prompt_raw = body.get("prompt")
        prompt = str(prompt_raw).strip() if prompt_raw is not None else None
        if not prompt:
            prompt = None
        output_format = str(body.get("output_format") or "txt").strip().lower()
        if output_format not in {"txt", "md"}:
            raise AppError("INVALID_OUTPUT_FORMAT", "output_format 仅支持 txt 或 md", 400)
        do_download = bool(body.get("download", True))
        preset = get_preset(str(body.get("preset_id") or "") or None)
        job_id = next_id()
        store = get_history_store()
        store.create_job(
            id=job_id,
            input_url=input_url,
            prompt=prompt,
            preset_id=preset["id"] if preset else None,
            preset_name=preset["name"] if preset else None,
            output_format=output_format,
            download_requested=do_download,
            max_attempts=MAX_PARSE_ATTEMPTS,
        )
        with PROCESS_LOCK:
            job = run_parse_job(
                job_id,
                input_url,
                prompt,
                preset,
                output_format,
                do_download,
                store=store,
                download_dir=DOWNLOAD_DIR,
                analyzer=analyze_with_yuanbao,
                downloader=download_video,
                max_attempts=MAX_PARSE_ATTEMPTS,
                sleeper=RETRY_SLEEPER,
            )
        serialized = serialize_job(job)
        if job.get("status") == "completed":
            return jsonify({"ok": True, **serialized})
        return jsonify({
            "ok": False,
            "error": {
                "code": job.get("error_code") or "PARSE_FAILED",
                "message": job.get("error_message") or "解析失败",
            },
            "job": serialized,
        }), failed_job_status(job)
    except BadRequest as exc:
        return jsonify({"ok": False, "error": {"code": "BAD_REQUEST", "message": "请求 JSON 格式不正确", "detail": str(exc)}}), 400
    except AppError as exc:
        return jsonify({"ok": False, "error": {"code": exc.code, "message": exc.message, "detail": exc.detail}}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(exc)}}), 500


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
