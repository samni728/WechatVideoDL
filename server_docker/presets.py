from __future__ import annotations

from typing import Any

PRESETS: list[dict[str, str]] = [
    {
        "id": "script_copy",
        "name": "脚本 / 字幕 / 文案提炼",
        "prompt": "提炼视频的完整口播脚本、字幕脉络、核心文案与结构；去除重复表达，保留重要信息和关键原句，并按主题分段整理。",
    },
    {
        "id": "engineering_framework",
        "name": "工程项目核心框架",
        "prompt": "针对工程、技术或项目型视频，提炼项目目标、背景、关键方案、实施步骤、关键技术、资源需求、风险、结果与可复用框架，形成结构化项目笔记。",
    },
    {
        "id": "knowledge_tags",
        "name": "知识库标签与元数据",
        "prompt": "为知识库导入提取：主题、3-10 个核心标签、人物/组织/产品/技术名词、关键结论、适用场景、检索关键词，并给出一段不超过 200 字的摘要。",
    },
    {
        "id": "marketing_breakdown",
        "name": "短视频卖点与传播结构",
        "prompt": "拆解视频的开场钩子、核心卖点、论证方式、情绪节奏、转折点、行动号召与可复用传播话术，指出最值得复用的表达结构。",
    },
    {
        "id": "knowledge_notes",
        "name": "结构化知识笔记",
        "prompt": "将视频整理成可长期保存的知识笔记：一句话结论、核心观点、关键事实/步骤、值得延伸研究的问题、可执行清单，并保留必要上下文。",
    },
]

_PRESET_MAP = {item["id"]: item for item in PRESETS}


def get_preset(preset_id: str | None) -> dict[str, str] | None:
    if not preset_id:
        return None
    item = _PRESET_MAP.get(str(preset_id))
    return dict(item) if item else None
