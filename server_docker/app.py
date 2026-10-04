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
from flask import Flask, Response, jsonify, redirect, render_template_string, request, send_from_directory, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

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
        for path in DOWNLOAD_DIR.glob("wxv_*.mp4"):
            m = re.fullmatch(r"wxv_(\d+)\.mp4", path.name)
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
    return values[-1] if values else fallback


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


LOGIN_HTML = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WX Video Login</title><style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0e1117;color:#e9eef6;margin:0;display:grid;place-items:center;min-height:100vh}
.card{width:min(420px,90vw);background:#171c25;border:1px solid #283140;border-radius:18px;padding:28px;box-shadow:0 20px 80px #0008}
h1{margin:0 0 20px;font-size:24px}input{box-sizing:border-box;width:100%;padding:12px 14px;margin:8px 0;border-radius:10px;border:1px solid #364052;background:#0f141c;color:#fff}
button{width:100%;padding:12px;margin-top:12px;border:0;border-radius:10px;background:#3b82f6;color:#fff;font-weight:700;cursor:pointer}
.err{color:#ff8b8b;margin-top:12px}</style></head><body><form class="card" method="post"><h1>WX Video Download</h1>
<input name="username" placeholder="Username" autocomplete="username" required>
<input name="password" type="password" placeholder="Password" autocomplete="current-password" required>
<button type="submit">登录</button>{% if error %}<div class="err">{{ error }}</div>{% endif %}</form></body></html>
"""

INDEX_HTML = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WX Video Download</title><style>
:root{color-scheme:dark}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0b0f15;color:#e8edf5;margin:0}
.wrap{max-width:980px;margin:38px auto;padding:0 18px}.top{display:flex;justify-content:space-between;align-items:center}.muted{color:#8996a8}
.card{background:#151b24;border:1px solid #273140;border-radius:16px;padding:22px;margin-top:18px}
label{display:block;font-weight:700;margin:14px 0 7px}input,textarea{box-sizing:border-box;width:100%;border:1px solid #344154;border-radius:10px;background:#0d121a;color:#fff;padding:12px}
textarea{min-height:120px;resize:vertical}.row{display:flex;gap:12px;align-items:center}.row input[type=checkbox]{width:auto}
button{padding:11px 18px;border:0;border-radius:10px;background:#3b82f6;color:white;font-weight:700;cursor:pointer}button:disabled{opacity:.5}
pre{white-space:pre-wrap;word-break:break-word;background:#0a0e14;border-radius:10px;padding:14px;max-height:480px;overflow:auto}
a{color:#72a7ff}.links a{display:inline-block;margin:6px 14px 6px 0}.spinner{display:none;color:#9fb4cf;margin-left:12px}
</style></head><body><div class="wrap">
<div class="top"><div><h1>微信视频号解析 / 下载</h1><div class="muted">Browserless Headless + Yuanbao + yt-dlp</div></div><a href="/logout">退出</a></div>
<div class="card">
<label>微信视频号 URL</label><input id="url" placeholder="https://weixin.qq.com/sph/..." autofocus>
<label>Prompt（可选）</label><textarea id="prompt" placeholder="例如：总结一下这个视频的核心内容"></textarea>
<div class="row"><input id="download" type="checkbox" checked><label for="download" style="margin:0;font-weight:500">下载 MP4</label></div>
<div style="margin-top:18px"><button id="go">开始解析</button><span id="spin" class="spinner">处理中，可能需要 1–3 分钟…</span></div>
</div>
<div class="card" id="result" style="display:none"><div id="summary"></div><div class="links" id="links"></div><pre id="content"></pre><details><summary>完整 JSON</summary><pre id="json"></pre></details></div>
</div><script>
const go=document.getElementById("go"), spin=document.getElementById("spin"), result=document.getElementById("result");
go.onclick=async()=>{go.disabled=true;spin.style.display="inline";result.style.display="none";
 try{const r=await fetch("/api/parse",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
 url:document.getElementById("url").value,prompt:document.getElementById("prompt").value||null,download:document.getElementById("download").checked})});
 const j=await r.json(); result.style.display="block"; document.getElementById("json").textContent=JSON.stringify(j,null,2);
 if(!j.ok){document.getElementById("summary").textContent=(j.error&&j.error.message)||"请求失败";document.getElementById("links").innerHTML="";document.getElementById("content").textContent="";return}
 document.getElementById("summary").textContent="任务 "+j.id+" 已完成";
 let links='<a href="'+j.text.download_url+'">下载 TXT</a>';
 if(j.video.download_url) links+='<a href="'+j.video.download_url+'">下载 MP4</a>';
 document.getElementById("links").innerHTML=links;
 document.getElementById("content").textContent=j.content||"";
 }catch(e){result.style.display="block";document.getElementById("summary").textContent="请求失败："+e}
 finally{go.disabled=false;spin.style.display="none"}};
</script></body></html>
"""


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
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@api_or_session_required
def index():
    return render_template_string(INDEX_HTML)


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


@app.route("/api/parse", methods=["POST"])
@api_or_session_required
def api_parse():
    started = time.monotonic()
    try:
        body = request.get_json(force=True, silent=False) or {}
        input_url = valid_input_url(str(body.get("url") or ""))
        prompt_raw = body.get("prompt")
        prompt = str(prompt_raw).strip() if prompt_raw is not None else None
        if not prompt:
            prompt = None
        do_download = bool(body.get("download", True))

        with PROCESS_LOCK:
            yuanbao = analyze_with_yuanbao(input_url, prompt)
            job_id = next_id()
            text_path = save_text(job_id, input_url, prompt, yuanbao["content"])
            video_path = download_video(job_id, yuanbao["directUrl"], yuanbao["previewUrl"]) if do_download else None

        result: dict[str, Any] = {
            "ok": True,
            "id": job_id,
            "input_url": input_url,
            "prompt": prompt,
            "content": yuanbao["content"],
            "yuanbao": {
                "preview_url": yuanbao["previewUrl"],
                "card": yuanbao.get("card"),
            },
            "text": {
                "filename": text_path.name,
                "bytes": text_path.stat().st_size,
                "download_url": make_file_url(text_path.name),
            },
            "video": {
                "source_direct_url": yuanbao["directUrl"],
                "downloaded": bool(video_path),
                "download_url": make_file_url(video_path.name) if video_path else None,
                "filename": video_path.name if video_path else None,
                "bytes": video_path.stat().st_size if video_path else None,
                "media": media_info(video_path) if video_path else None,
            },
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        return jsonify(result)
    except AppError as exc:
        return jsonify({"ok": False, "error": {"code": exc.code, "message": exc.message, "detail": exc.detail}}), exc.status
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(exc)}}), 500


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
