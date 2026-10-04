from __future__ import annotations

import re
from pathlib import Path
from typing import Any


OWNED_FILE_RE = re.compile(r"^wxv_\d{6}\.(?:txt|md|mp4)$")


def safe_owned_path(download_dir: Path, filename: str | None) -> Path | None:
    if not filename or not OWNED_FILE_RE.fullmatch(filename):
        return None
    root = Path(download_dir).resolve()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def write_analysis_file(download_dir: Path, job: dict[str, Any], output_format: str) -> Path:
    if output_format not in {"txt", "md"}:
        raise ValueError("output_format must be 'txt' or 'md'")
    job_id = str(job.get("id") or "")
    if not re.fullmatch(r"wxv_\d{6}", job_id):
        raise ValueError("invalid job id")

    root = Path(download_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{job_id}.{output_format}"
    input_url = str(job.get("input_url") or "")
    prompt = str(job.get("prompt") or "").strip()
    content = str(job.get("yuanbao_content") or "").strip()
    created_at = str(job.get("created_at") or "")
    preset_name = str(job.get("preset_name") or "").strip()

    if output_format == "txt":
        parts = [f"ID: {job_id}", f"INPUT_URL: {input_url}"]
        if created_at:
            parts.append(f"CREATED_AT: {created_at}")
        if preset_name:
            parts.append(f"PRESET: {preset_name}")
        if prompt:
            parts += ["", "PROMPT:", prompt]
        parts += ["", "YUANBAO_CONTENT:", content, ""]
        text = "\n".join(parts)
    else:
        lines = [
            "# WechatVideoDL Analysis",
            "",
            f"- ID: `{job_id}`",
            f"- Source: {input_url}",
        ]
        if created_at:
            lines.append(f"- Created: {created_at}")
        if preset_name:
            lines.append(f"- Preset: {preset_name}")
        if prompt:
            lines += ["", "## Prompt", "", prompt]
        lines += ["", "## Yuanbao Response", "", content, ""]
        text = "\n".join(lines)

    path.write_text(text, encoding="utf-8")
    return path
