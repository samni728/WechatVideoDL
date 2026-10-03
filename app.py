#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.environ.get("WX_VIDEO_DOWNLOAD_DIR", PROJECT_DIR / "downloads")).resolve()
STATE_DIR = PROJECT_DIR / ".runtime"
SEQUENCE_FILE = STATE_DIR / "sequence.txt"
DEFAULT_HOST = os.environ.get("WX_VIDEO_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("WX_VIDEO_PORT", "18769"))
YUANBAO_HOME = "https://yuanbao.tencent.com/"
OPENCLI_TIMEOUT = int(os.environ.get("WX_VIDEO_OPENCLI_TIMEOUT", "35"))
YUANBAO_TIMEOUT = int(os.environ.get("WX_VIDEO_YUANBAO_TIMEOUT", "120"))
DOWNLOAD_TIMEOUT = int(os.environ.get("WX_VIDEO_DOWNLOAD_TIMEOUT", "300"))
API_KEY = os.environ.get("WX_VIDEO_API_KEY", "").strip()
DEFAULT_COOKIE_FILE = PROJECT_DIR / "yuanbao.tencent.com_cookies.txt"
COOKIE_FILE = Path(
    os.environ.get("YUANBAO_COOKIE_FILE", str(DEFAULT_COOKIE_FILE))
).expanduser().resolve()
PROCESS_LOCK = threading.Lock()


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 500, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


def _which_opencli() -> str | None:
    explicit = os.environ.get("OPENCLI_BIN")
    if explicit:
        return explicit
    return shutil.which("opencli")


def _yt_dlp_command() -> list[str] | None:
    explicit = os.environ.get("YTDLP_BIN")
    if explicit:
        return [explicit]

    system = shutil.which("yt-dlp")
    if system:
        return [system]

    local_python = PROJECT_DIR / ".venv" / "bin" / "python"
    if local_python.exists():
        probe = subprocess.run(
            [str(local_python), "-c", "import yt_dlp"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if probe.returncode == 0:
            return [str(local_python), "-m", "yt_dlp"]
    return None


def dependency_status() -> dict[str, Any]:
    opencli = _which_opencli()
    ytdlp = _yt_dlp_command()
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    return {
        "opencli": {"ok": bool(opencli), "path": opencli},
        "yt_dlp": {"ok": bool(ytdlp), "command": ytdlp},
        "ffmpeg": {"ok": bool(ffmpeg), "path": ffmpeg},
        "ffprobe": {"ok": bool(ffprobe), "path": ffprobe},
    }


def require_dependencies(*, need_download: bool) -> None:
    deps = dependency_status()
    if not deps["opencli"]["ok"]:
        raise ServiceError(
            "OPENCLI_NOT_FOUND",
            "没有检测到 opencli。请先安装并确保 opencli 在 PATH 中，然后重新请求。",
            status=503,
            detail={"hint": "运行 opencli doctor 确认 Browser Bridge 正常。"},
        )
    if need_download and not deps["yt_dlp"]["ok"]:
        raise ServiceError(
            "YTDLP_NOT_FOUND",
            "没有检测到 yt-dlp。请先运行项目里的 ./scripts/bootstrap.sh 安装项目本地 yt-dlp。",
            status=503,
        )


def run_command(args: list[str], *, timeout: int, check: bool = True) -> CommandResult:
    try:
        p = subprocess.run(
            args,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceError(
            "COMMAND_TIMEOUT",
            f"命令执行超时：{args[0]}",
            status=504,
            detail={"timeout_seconds": timeout, "stdout": exc.stdout, "stderr": exc.stderr},
        ) from exc
    except FileNotFoundError as exc:
        raise ServiceError("COMMAND_NOT_FOUND", f"命令不存在：{args[0]}", status=503) from exc

    result = CommandResult(args=args, returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)
    if check and p.returncode != 0:
        raise ServiceError(
            "COMMAND_FAILED",
            f"命令执行失败：{args[0]}",
            status=502,
            detail={
                "returncode": p.returncode,
                "stdout": p.stdout[-5000:],
                "stderr": p.stderr[-5000:],
                "args": args[:5],
            },
        )
    return result


def opencli_browser(session: str, *parts: str, timeout: int | None = None, check: bool = True) -> CommandResult:
    opencli = _which_opencli()
    if not opencli:
        raise ServiceError("OPENCLI_NOT_FOUND", "opencli 不在 PATH 中。", status=503)
    args = [opencli, "browser", session, *parts]
    return run_command(args, timeout=timeout or OPENCLI_TIMEOUT, check=check)


def opencli_yuanbao(*parts: str, timeout: int | None = None, check: bool = True) -> CommandResult:
    opencli = _which_opencli()
    if not opencli:
        raise ServiceError("OPENCLI_NOT_FOUND", "opencli 不在 PATH 中。", status=503)
    args = [opencli, "yuanbao", *parts]
    return run_command(args, timeout=timeout or YUANBAO_TIMEOUT, check=check)


def safe_close_browser(session: str) -> None:
    try:
        opencli_browser(session, "close", timeout=12, check=False)
    except Exception:
        # Cleanup must never turn an otherwise successful API request into 5xx.
        pass


def parse_json_loose(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        if not starts:
            raise
        start = min(starts)
        for end in range(len(text), start, -1):
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
        raise


def unique_session(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def clean_eval_output(text: str) -> str:
    value = text.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, str):
                return decoded
        except json.JSONDecodeError:
            pass
    return value


def _load_netscape_cookies(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    cookies: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            continue

        domain, include_subdomains, cookie_path, secure, expires, name = parts[:6]
        value = "\t".join(parts[6:])
        cookies.append(
            {
                "domain": domain,
                "include_subdomains": include_subdomains.upper() == "TRUE",
                "path": cookie_path or "/",
                "secure": secure.upper() == "TRUE",
                "expires": int(expires or "0") if (expires or "0").isdigit() else 0,
                "name": name,
                "value": value,
                "http_only": http_only,
            }
        )
    return cookies


def import_yuanbao_cookies(session: str) -> dict[str, Any]:
    cookies = _load_netscape_cookies(COOKIE_FILE)
    if not cookies:
        return {"used": False, "path": str(COOKIE_FILE), "imported": 0, "skipped_http_only": 0}

    assignments: list[str] = []
    skipped_http_only = 0
    for cookie in cookies:
        domain = str(cookie["domain"]).lstrip(".").lower()
        if not ("yuanbao.tencent.com" == domain or "yuanbao.tencent.com".endswith("." + domain)):
            continue
        if cookie["http_only"]:
            skipped_http_only += 1
            continue

        parts = [f'{cookie["name"]}={cookie["value"]}', f'Path={cookie["path"]}']
        cookie_domain = str(cookie["domain"])
        if cookie_domain:
            parts.append(f"Domain={cookie_domain}")
        if cookie["secure"]:
            parts.append("Secure")
        expires = int(cookie["expires"] or 0)
        if expires > 0:
            stamp = dt.datetime.fromtimestamp(expires, tz=dt.timezone.utc)
            parts.append("Expires=" + stamp.strftime("%a, %d %b %Y %H:%M:%S GMT"))
        assignments.append("; ".join(parts))

    if not assignments:
        return {
            "used": False,
            "path": str(COOKIE_FILE),
            "imported": 0,
            "skipped_http_only": skipped_http_only,
        }

    # Values are kept local: the API never logs or returns cookie contents.
    script = (
        "(()=>{"
        + f"const xs={json.dumps(assignments, ensure_ascii=False)};"
        + "for(const x of xs){document.cookie=x;}"
        + "return {count:xs.length,names:xs.map(x=>x.split('=',1)[0])};"
        + "})()"
    )
    result = opencli_browser(session, "eval", script, timeout=OPENCLI_TIMEOUT, check=False)
    if result.returncode != 0:
        raise ServiceError(
            "YUANBAO_COOKIE_IMPORT_FAILED",
            "读取到了元宝 Cookie 文件，但导入浏览器失败。",
            status=503,
            detail={"path": str(COOKIE_FILE), "stderr": result.stderr[-1200:]},
        )
    return {
        "used": True,
        "path": str(COOKIE_FILE),
        "imported": len(assignments),
        "skipped_http_only": skipped_http_only,
    }


def _extract_assistant_content(raw: str, request_text: str) -> str:
    if not raw:
        return ""
    # Yuanbao's link card is followed by the app label "微信视频号".
    # Everything after that label is the assistant's textual analysis.
    if "微信视频号" in raw:
        after = raw.split("微信视频号", 1)[1].strip()
        if after:
            return after

    cleaned = raw.replace("点击全选以下消息", "", 1).strip()
    if request_text and request_text in cleaned:
        cleaned = cleaned.replace(request_text, "", 1).strip()
    return cleaned


def _card_metadata(session: str) -> dict[str, Any]:
    script = (
        '(()=>{const e=document.querySelector(".hyc-link-card");'
        'if(!e)return null;'
        'const k=Object.keys(e).find(x=>x.startsWith("__reactFiber"));'
        'const p=e[k]?.return?.memoizedProps||{};'
        'return {url:p.url||"",content:p.content||"",source:p.source||"",coverUrl:p.coverUrl||""}})()'
    )
    out = opencli_browser(session, "eval", script, timeout=OPENCLI_TIMEOUT).stdout
    parsed = parse_json_loose(out)
    return parsed if isinstance(parsed, dict) else {}


def _resolved_card_url(session: str) -> str:
    script = (
        '(()=>{const e=document.querySelector(".hyc-link-card");'
        'if(!e)return "";'
        'const k=Object.keys(e).find(x=>x.startsWith("__reactFiber"));'
        'const p=e[k]?.return?.memoizedProps||{};'
        'return p.resolveUrl ? p.resolveUrl(p.url) : (p.url||"")})()'
    )
    out = opencli_browser(session, "eval", script, timeout=OPENCLI_TIMEOUT).stdout
    return clean_eval_output(out)


def yuanbao_analyze(url: str, prompt: str | None) -> dict[str, Any]:
    auth_session = unique_session("wxydl_auth")
    card_session = unique_session("wxydl_card")
    request_text = url if not prompt else f"{url}\n\n{prompt.strip()}"
    started = time.monotonic()
    cookie_status: dict[str, Any] = {
        "used": False,
        "path": str(COOKIE_FILE),
        "imported": 0,
        "skipped_http_only": 0,
    }
    try:
        # 先把 Netscape Cookie 导入 OpenCLI 所连接的 Chrome Profile。
        # 导入后直接使用 OpenCLI 自带 yuanbao adapter，让它负责等待完整回答；
        # 这比自行轮询 DOM 稳定，尤其能避免把“源”或“请求中”误判成最终回复。
        opencli_browser(auth_session, "open", YUANBAO_HOME, "--window", "background", timeout=OPENCLI_TIMEOUT)
        opencli_browser(auth_session, "wait", "time", "1", timeout=OPENCLI_TIMEOUT)
        cookie_status = import_yuanbao_cookies(auth_session)
        if cookie_status["used"]:
            opencli_browser(auth_session, "open", YUANBAO_HOME, timeout=OPENCLI_TIMEOUT)
            opencli_browser(auth_session, "wait", "time", "1", timeout=OPENCLI_TIMEOUT)

        whoami = opencli_yuanbao(
            "whoami",
            "--window",
            "background",
            "--site-session",
            "persistent",
            "-f",
            "json",
            timeout=OPENCLI_TIMEOUT,
            check=False,
        )
        if whoami.returncode != 0:
            raise ServiceError(
                "YUANBAO_LOGIN_REQUIRED",
                "元宝 Cookie 没有形成有效登录态。请更新 yuanbao.tencent.com_cookies.txt 或重新扫码登录。",
                status=503,
                detail={"cookie_file": str(COOKIE_FILE)},
            )

        opencli_yuanbao(
            "new",
            "--window",
            "background",
            "--site-session",
            "persistent",
            "-f",
            "json",
            timeout=OPENCLI_TIMEOUT,
        )

        ask = opencli_yuanbao(
            "ask",
            request_text,
            "--timeout",
            str(YUANBAO_TIMEOUT),
            "--search",
            "false",
            "--window",
            "background",
            "--site-session",
            "persistent",
            "--keep-tab",
            "true",
            "-f",
            "json",
            timeout=YUANBAO_TIMEOUT + 30,
        )
        ask_payload = parse_json_loose(ask.stdout)
        if not isinstance(ask_payload, list):
            raise ServiceError(
                "YUANBAO_BAD_RESPONSE",
                "OpenCLI yuanbao adapter 返回了无法识别的响应格式。",
                status=502,
            )

        assistant_content = ""
        for item in reversed(ask_payload):
            if isinstance(item, dict) and str(item.get("Role", "")).lower() == "assistant":
                assistant_content = str(item.get("Text") or "").strip()
                break
        if not assistant_content:
            raise ServiceError(
                "YUANBAO_EMPTY_RESPONSE",
                "元宝没有返回可用的文本内容。",
                status=502,
            )

        status_result = opencli_yuanbao(
            "status",
            "--window",
            "background",
            "--site-session",
            "persistent",
            "-f",
            "json",
            timeout=OPENCLI_TIMEOUT,
        )
        status_payload = parse_json_loose(status_result.stdout)
        conversation_url = ""
        if isinstance(status_payload, list) and status_payload and isinstance(status_payload[0], dict):
            conversation_url = str(status_payload[0].get("Url") or "")
        if not conversation_url:
            raise ServiceError(
                "YUANBAO_CONVERSATION_URL_MISSING",
                "元宝已经返回文本，但没有拿到当前会话 URL。",
                status=502,
            )

        # 重新打开刚才的元宝会话，读取富卡片里的 resolveUrl()。
        opencli_browser(card_session, "open", conversation_url, "--window", "background", timeout=OPENCLI_TIMEOUT)
        # 历史会话默认停在回答底部，元宝会虚拟化顶部的用户消息/视频卡片；
        # 先把会话往上滚，让 .hyc-link-card 真正挂载到 DOM。
        for _ in range(6):
            opencli_browser(card_session, "scroll", "up", timeout=OPENCLI_TIMEOUT, check=False)
            time.sleep(0.2)
        deadline = time.monotonic() + 20
        card_seen = False
        while time.monotonic() < deadline:
            time.sleep(1.5)
            probe = opencli_browser(
                card_session,
                "eval",
                'document.querySelector(".hyc-link-card") ? "1" : "0"',
                timeout=OPENCLI_TIMEOUT,
                check=False,
            )
            if probe.returncode == 0 and clean_eval_output(probe.stdout) == "1":
                card_seen = True
                break
        if not card_seen:
            raise ServiceError(
                "YUANBAO_LINK_CARD_MISSING",
                "元宝已经返回文本，但历史会话里的微信视频号卡片没有加载出来。",
                status=502,
                detail={"conversation_url": conversation_url},
            )

        card = _card_metadata(card_session)
        resolved_url = _resolved_card_url(card_session)
        if not resolved_url:
            raise ServiceError(
                "YUANBAO_LINK_NOT_FOUND",
                "元宝已经返回内容，但没有解析出视频号可打开链接。",
                status=502,
                detail={"card": card, "conversation_url": conversation_url},
            )

        return {
            "preview_url": resolved_url,
            "content": assistant_content,
            "raw_content": ask_payload,
            "card": card,
            "conversation_url": conversation_url,
            "cookie_auth": {
                "used": cookie_status["used"],
                "path": cookie_status["path"],
                "imported": cookie_status["imported"],
                "skipped_http_only": cookie_status["skipped_http_only"],
            },
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        safe_close_browser(auth_session)
        safe_close_browser(card_session)


def resolve_video_source(preview_url: str) -> dict[str, Any]:
    session = unique_session("wxydl_video")
    started = time.monotonic()
    try:
        opencli_browser(session, "open", preview_url, "--window", "background", timeout=OPENCLI_TIMEOUT)
        deadline = time.monotonic() + 30
        direct = ""
        while time.monotonic() < deadline:
            time.sleep(1.5)
            out = opencli_browser(
                session,
                "eval",
                'document.querySelector("video")?.currentSrc || document.querySelector("video")?.src || ""',
                timeout=OPENCLI_TIMEOUT,
                check=False,
            )
            if out.returncode == 0:
                direct = clean_eval_output(out.stdout)
            if direct.startswith("http"):
                break

        if not direct:
            raise ServiceError(
                "VIDEO_SOURCE_NOT_FOUND",
                "已经打开元宝解析出的微信视频号页面，但没有找到 video.currentSrc。",
                status=502,
                detail={"preview_url": preview_url},
            )

        return {
            "direct_url": direct,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        safe_close_browser(session)


def safe_filename(text: str) -> str:
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._ ")
    return (text[:80] or "wechat_video")


def next_job_id() -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    current = 0
    if SEQUENCE_FILE.exists():
        try:
            current = int(SEQUENCE_FILE.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            current = 0
    if current <= 0:
        for path in DOWNLOAD_DIR.glob("wxv_*.mp4"):
            match = re.fullmatch(r"wxv_(\d+)\.mp4", path.name)
            if match:
                current = max(current, int(match.group(1)))
    current += 1
    tmp = SEQUENCE_FILE.with_suffix(".tmp")
    tmp.write_text(str(current), encoding="utf-8")
    os.replace(tmp, SEQUENCE_FILE)
    return f"wxv_{current:06d}"


def save_text_result(job_id: str, *, input_url: str, prompt: str | None, content: str) -> dict[str, Any]:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = DOWNLOAD_DIR / f"{job_id}.txt"
    parts = [
        f"ID: {job_id}",
        f"INPUT_URL: {input_url}",
    ]
    if prompt:
        parts.extend(["", "PROMPT:", prompt])
    parts.extend(["", "YUANBAO_CONTENT:", content.strip(), ""])
    path.write_text("\n".join(parts), encoding="utf-8")
    return {
        "path": str(path.resolve()),
        "filename": path.name,
        "bytes": path.stat().st_size,
    }


def download_video(direct_url: str, job_id: str) -> dict[str, Any]:
    cmd = _yt_dlp_command()
    if not cmd:
        raise ServiceError(
            "YTDLP_NOT_FOUND",
            "yt-dlp 不可用。运行 ./scripts/bootstrap.sh 后重试。",
            status=503,
        )

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    template = DOWNLOAD_DIR / f"{job_id}.%(ext)s"

    args = [
        *cmd,
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--print",
        "after_move:filepath",
        "-o",
        str(template),
        direct_url,
    ]
    result = run_command(args, timeout=DOWNLOAD_TIMEOUT, check=True)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    final_path = Path(lines[-1]) if lines else None

    if not final_path or not final_path.exists():
        candidates = sorted(
            DOWNLOAD_DIR.glob(f"{job_id}.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        final_path = candidates[0] if candidates else None

    if not final_path or not final_path.exists() or final_path.stat().st_size <= 0:
        raise ServiceError(
            "DOWNLOAD_VERIFY_FAILED",
            "yt-dlp 命令结束，但没有找到有效下载文件。",
            status=502,
            detail={"stdout": result.stdout[-3000:], "stderr": result.stderr[-3000:]},
        )

    info: dict[str, Any] = {
        "path": str(final_path.resolve()),
        "filename": final_path.name,
        "bytes": final_path.stat().st_size,
    }

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        probe = run_command(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name",
                "-show_entries",
                "stream=codec_type,codec_name,width,height",
                "-of",
                "json",
                str(final_path),
            ],
            timeout=30,
            check=False,
        )
        if probe.returncode == 0:
            try:
                info["media"] = json.loads(probe.stdout)
            except json.JSONDecodeError:
                pass
    return info


def validate_input_url(url: str) -> str:
    url = (url or "").strip()
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise ServiceError("INVALID_URL", "URL 格式不正确。", status=400) from exc

    allowed = {
        "weixin.qq.com",
        "www.weixin.qq.com",
        "channels.weixin.qq.com",
    }
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in allowed:
        raise ServiceError(
            "INVALID_URL",
            "目前只接受 weixin.qq.com 或 channels.weixin.qq.com 的微信视频号链接。",
            status=400,
        )
    return url


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "WXVideoDownload/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not API_KEY:
            return True
        supplied = self.headers.get("X-API-Key", "").strip()
        auth = self.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        return supplied == API_KEY

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(401, {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "API key 不正确。"}})
        return False

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        if not self._require_auth():
            return

        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._json(
                200,
                {
                    "ok": True,
                    "service": "wx_video_download",
                    "endpoint": "POST /api/parse",
                    "health": "GET /health",
                    "downloads": "GET /files/<filename>",
                },
            )
            return

        if path == "/health":
            self._json(
                200,
                {
                    "ok": True,
                    "dependencies": dependency_status(),
                    "download_dir": str(DOWNLOAD_DIR),
                    "api_key_required": bool(API_KEY),
                    "busy": PROCESS_LOCK.locked(),
                },
            )
            return

        if path.startswith("/files/"):
            self._serve_file(path[len("/files/"):])
            return

        self._json(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "接口不存在。"}})

    def _serve_file(self, encoded_name: str) -> None:
        name = Path(urllib.parse.unquote(encoded_name)).name
        file_path = (DOWNLOAD_DIR / name).resolve()
        if file_path.parent != DOWNLOAD_DIR or not file_path.exists() or not file_path.is_file():
            self._json(404, {"ok": False, "error": {"code": "FILE_NOT_FOUND", "message": "文件不存在。"}})
            return

        ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        size = file_path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(file_path.name)}")
        self._cors_headers()
        self.end_headers()

        with file_path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile, length=1024 * 1024)

    def do_POST(self) -> None:
        if not self._require_auth():
            return

        path = urllib.parse.urlparse(self.path).path
        if path != "/api/parse":
            self._json(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "接口不存在。"}})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 262144:
                raise ServiceError("INVALID_BODY", "请求体为空或过大。", status=400)

            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception as exc:
                raise ServiceError("INVALID_JSON", "请求体必须是 JSON。", status=400) from exc

            url = validate_input_url(str(payload.get("url") or ""))
            prompt_raw = payload.get("prompt")
            prompt = str(prompt_raw).strip() if prompt_raw is not None else None
            if prompt == "":
                prompt = None
            do_download = bool(payload.get("download", True))

            require_dependencies(need_download=do_download)
            started = time.monotonic()

            with PROCESS_LOCK:
                yuanbao = yuanbao_analyze(url, prompt)
                video_source = resolve_video_source(yuanbao["preview_url"])
                job_id = next_job_id()
                text_info = save_text_result(
                    job_id,
                    input_url=url,
                    prompt=prompt,
                    content=str(yuanbao.get("content") or ""),
                )

                download_info = None
                if do_download:
                    download_info = download_video(video_source["direct_url"], job_id)

            host = self.headers.get("Host") or f"127.0.0.1:{DEFAULT_PORT}"
            scheme = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip() or "http"

            response: dict[str, Any] = {
                "ok": True,
                "id": job_id,
                "input_url": url,
                "prompt": prompt,
                "yuanbao": yuanbao,
                "video": {
                    "source_direct_url": video_source["direct_url"],
                    "source_resolve_ms": video_source["elapsed_ms"],
                    "downloaded": bool(download_info),
                },
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }

            text_url = f"{scheme}://{host}/files/{urllib.parse.quote(text_info['filename'])}"
            response["text"] = {
                "content": str(yuanbao.get("content") or ""),
                "download_url": text_url,
                "filename": text_info["filename"],
                "bytes": text_info["bytes"],
                "local_path": text_info["path"],
            }

            if download_info:
                file_url = f"{scheme}://{host}/files/{urllib.parse.quote(download_info['filename'])}"
                response["video"].update(
                    {
                        "download_url": file_url,
                        "filename": download_info["filename"],
                        "bytes": download_info["bytes"],
                        "local_path": download_info["path"],
                        "media": download_info.get("media"),
                    }
                )

            self._json(200, response)
        except ServiceError as exc:
            self._json(
                exc.status,
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "detail": exc.detail,
                    },
                },
            )
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._json(
                500,
                {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": str(exc),
                    },
                },
            )


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="微信视频号 -> 元宝解析 -> OpenCLI -> yt-dlp API")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--check", action="store_true", help="只检查依赖后退出")
    args = parser.parse_args()

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    if args.check:
        print(json.dumps(dependency_status(), ensure_ascii=False, indent=2))
        return

    deps = dependency_status()
    print(json.dumps({"dependencies": deps}, ensure_ascii=False, indent=2))
    if not deps["opencli"]["ok"]:
        print("ERROR: opencli 不存在，无法启动解析服务。", file=sys.stderr)
        sys.exit(2)

    server = ApiServer((args.host, args.port), ApiHandler)
    print(f"wx_video_download API listening on http://{args.host}:{args.port}")
    print(f"downloads: {DOWNLOAD_DIR}")
    if API_KEY:
        print("API key protection: enabled")
    else:
        print("API key protection: disabled (set WX_VIDEO_API_KEY to enable)")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
