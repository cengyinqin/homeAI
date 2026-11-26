#!/usr/bin/env python3
"""Local gallery and prompt workbench for the homai image library.

Python standard library only. Pillow is optional and is used solely for
on-demand 480 px JPEG thumbnails.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import math
import mimetypes
import os
import queue
import re
import shutil
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen

try:
    from PIL import Image, ImageOps

    PIL_AVAILABLE = True
except Exception:  # Pillow is optional; the API still works without it.
    Image = None
    ImageOps = None
    PIL_AVAILABLE = False

import credentials as model_credentials


ROOT = Path(__file__).resolve().parent
GUI_DIR = ROOT / "gui"
DEMO_DIR = ROOT / "demo"
IMAGES_DIR = ROOT / "images"
SAMPLES_DIR = ROOT / "samples"
STYLE_DIR = ROOT / "style"
CACHE_DIR = ROOT / "gui_cache"
THUMB_DIR = CACHE_DIR / "thumbs"
OVERRIDES_PATH = STYLE_DIR / "prompt_overrides.json"
GENERATED_SAMPLES_PATH = CACHE_DIR / "generated_samples.json"
CONFIG_PATH = GUI_DIR / "config.json"

PROMPT_FIELDS = ("prompt_en", "prompt_zh", "prompt_short_en", "negative_en")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_BODY_BYTES = 1024 * 1024
MAX_PROMPT_CHARS = 20000
SAMPLE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# Code assets must be revalidated on every navigation so a server restart is
# picked up by a plain reload. Immutable media uses version-unique filenames
# and can stay in the browser cache for a year.
STATIC_CACHE = "no-cache, must-revalidate"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"

# ---------- old-house remodel ----------

REMODEL_DIR = Path(os.environ.get("HOMAI_REMODEL_DIR") or (ROOT / "remodel")).resolve()
REMODEL_UPLOAD_DIR = REMODEL_DIR / "uploads"
REMODEL_JOB_DIR = REMODEL_DIR / "jobs"
REMODEL_ITEM_DIR = REMODEL_DIR / "items"
REMODEL_TRASH_DIR = REMODEL_DIR / "trash"
STYLE_PACKS_PATH = STYLE_DIR / "style_packs.json"
SMOKE_MODE = os.environ.get("HOMAI_SMOKE") == "1"

MAX_REMODEL_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_REMODEL_UPLOAD_BYTES + 1024 * 1024
REMODEL_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ITEM_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
GEN_ID_RE = ITEM_ID_RE
CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")

REMODEL_VISION_MODEL = "qwen4-max"
REMODEL_TEXT_MODEL = "qwen4-max"
REMODEL_MODES = {"img2img", "text"}
REMODEL_DEFAULT_MODE = "img2img"
REMODEL_ROOM_FUNCTIONS = (
    "living",
    "dining",
    "bedroom",
    "kitchen",
    "study",
    "bathroom",
    "entry",
    "balcony",
    "other",
    "unknown",
)
REMODEL_ROOM_FUNCTION_LABELS = {
    "living": "living room",
    "dining": "dining room",
    "bedroom": "bedroom",
    "kitchen": "kitchen",
    "study": "study",
    "bathroom": "bathroom",
    "entry": "entryway",
    "balcony": "balcony",
    "other": "room",
    "unknown": "room",
}
# Room-function aliases are matched case-insensitively as substrings so both
# "dining room" and "主餐厅" resolve. The order matters only for ambiguous
# names; explicit structure analysis always wins over the project name.
_ROOM_FUNCTION_ALIASES = (
    ("living", ("living room", "sitting room", "lounge", "客厅", "起居室", "起居")),
    ("dining", ("dining room", "dining", "餐厅", "饭厅", "餐区", "用餐区")),
    (
        "bedroom",
        (
            "bedroom",
            "master bedroom",
            "kids room",
            "kids' room",
            "卧室",
            "主卧",
            "次卧",
            "儿童房",
            "老人房",
        ),
    ),
    ("kitchen", ("kitchen", "厨房")),
    ("study", ("home office", "study", "书房", "工作间")),
    ("bathroom", ("bathroom", "shower room", "卫生间", "浴室", "洗手间")),
    ("entry", ("entryway", "entry", "foyer", "hallway", "玄关", "门厅", "过道")),
    ("balcony", ("balcony", "阳台")),
    ("other", ("other", "其它", "其他")),
    ("unknown", ("unknown", "未知", "不确定")),
)
# A supplementary requirement that is nothing but a room name is a room-type
# instruction, not a furnishing request. It is matched exactly after stripping
# whitespace/punctuation and a small set of trailing qualifiers, so "餐厅" and
# "餐厅改造" both resolve while "餐厅放一个餐边柜" does not.
_EXTRA_ROOM_ALIASES = (
    ("dining", ("餐厅", "饭厅", "餐区", "用餐区", "dining", "diningroom")),
    (
        "living",
        ("客厅", "起居室", "起居", "living", "livingroom", "sittingroom", "lounge"),
    ),
    (
        "bedroom",
        (
            "卧室",
            "主卧",
            "次卧",
            "儿童房",
            "老人房",
            "bedroom",
            "masterbedroom",
            "kidsroom",
            "kids",
        ),
    ),
    ("kitchen", ("厨房", "kitchen")),
    ("study", ("书房", "工作间", "study", "homeoffice")),
    ("bathroom", ("卫生间", "浴室", "洗手间", "bath", "bathroom", "showerroom")),
    ("entry", ("玄关", "门厅", "过道", "entry", "entryway", "foyer", "hallway")),
    ("balcony", ("阳台", "balcony")),
)
_EXTRA_ROOM_SUFFIXES = ("改造", "空间", "装修", "设计", "房间")
_EXTRA_ROOM_STRIP_RE = re.compile(
    r"[\s。，、！？,.!?\"'“”‘’（）()【】\[\]：:；;·—\-_/\\]+"
)
EXTRA_VERBATIM_PREFIX = "Additional requirement (verbatim Chinese, apply literally): "
_REMODEL_ROOM_FUNCTION_FURNITURE = {
    "dining": (
        "a dining table with four to six chairs at the existing table position, "
        "a sideboard or console along the same wall, a pendant or chandelier above "
        "the table, a rug under the table"
    ),
    "bedroom": (
        "a bed with its headboard on the same wall, two nightstands, a wardrobe or "
        "dresser, bedside lamps, a rug"
    ),
    "kitchen": (
        "base and wall cabinets along the existing runs, the same island or "
        "peninsula if present, counter stools"
    ),
    "study": (
        "a desk facing the same direction, a task chair, a bookshelf, a small "
        "console for storage"
    ),
    "bathroom": (
        "a vanity with mirror on the same wall, the same shower or tub, a towel "
        "rail, a bath mat"
    ),
    "entry": (
        "a console or shoe cabinet along the same wall, a bench, coat hooks, a mirror"
    ),
    "balcony": "a two-seat outdoor set, a side table, planters",
}
REMODEL_ROOM_FUNCTION_VISION_PROMPT = """Classify the function of the room in this photograph.

Return STRICT JSON only, no markdown fences and no prose:
{"room_function": "living", "confidence": 0.0}

room_function must be exactly one of: living, dining, bedroom, kitchen, study,
bathroom, entry, balcony, other, unknown.

Judge the room from its fixed architecture and its principal furniture (a
dining table and chairs mean dining; a bed means bedroom; a sofa and coffee
table mean living). Do not let decor or colour scheme change the classification.
confidence is 0.0-1.0.
"""
REMODEL_INPUT_LONG_EDGE = 1024
REMODEL_IMG2IMG_TIMEOUT = 900
REMODEL_TEXT_TIMEOUT = 600
REMODEL_ANALYSIS_TIMEOUT = 180
REMODEL_SELF_CHECK_TIMEOUT = 240
REMODEL_DOWNLOAD_TIMEOUT = 180
REMODEL_QUEUE_TIMEOUT = 900
REMODEL_SAVE_TIMEOUT = 120
REMODEL_REAPER_INTERVAL = 30.0
REMODEL_REAPER_GRACE = 60.0
REMODEL_THUMB_LONG_EDGE = 560
REMODEL_THUMB_QUALITY = 82
REMODEL_THUMB_MAX_BYTES = 400 * 1024
REMODEL_STAGES = (
    "queued",
    "encoding",
    "analyzing",
    "requesting",
    "downloading",
    "saving",
    "checking",
    "repairing",
    "done",
    "error",
)
GUI_SERVICE_LOG = Path(
    os.environ.get("HOMAI_SERVICE_LOG") or (ROOT / "logs" / "gui-service.log")
)
_STAGE_LOG_LOCK = threading.Lock()


def remodel_image_request_options(mode: str) -> tuple[float, bool]:
    """Return (timeout seconds, retry_network) for one remodel generation call.

    img2img renders take about five minutes on this gateway and can queue for
    much longer, so it gets the long timeout but a single attempt: a timeout
    must fail the attempt instead of silently doubling the wait. Text mode
    keeps its 600 s budget.
    """
    if mode == "img2img":
        return float(REMODEL_IMG2IMG_TIMEOUT), False
    return float(REMODEL_TEXT_TIMEOUT), False


def remodel_timeout_message(timeout: float, mode: str = "img2img") -> str:
    message = (
        f"生成接口排队超时（已等待 {int(round(timeout))} 秒），稍后重试"
    )
    if mode != "text":
        message += "或改用纯文本模式"
    return message
REMODEL_IMG2IMG_INSTRUCTION = (
    "Renovate this exact room photograph. Keep the architecture exactly as in the "
    "input image: same wall planes, same opening positions and sizes, same ceiling "
    "height, same floor level, same camera position, same perspective, same framing "
    "and aspect ratio. Do not add, remove, resize or move any opening."
)
try:
    REMODEL_MAX_ATTEMPTS = max(1, int(os.environ.get("HOMAI_REMODEL_MAX_ATTEMPTS") or 2))
except (TypeError, ValueError):
    REMODEL_MAX_ATTEMPTS = 2
REMODEL_SCHEMA_VERSION = 4
REMODEL_ARCHITECTURE_KEYS = (
    "openings",
    "count",
    "framing",
    "perspective",
    "ceiling",
    "floor",
    "fixed_features",
    "storage",
)
REMODEL_CHECK_KEYS = REMODEL_ARCHITECTURE_KEYS + (
    "lighting",
    "richness",
    "realism",
)
REMODEL_SCORE_WEIGHTS = {
    "architecture": 0.55,
    "lighting": 0.15,
    "richness": 0.15,
    "realism": 0.15,
}
REMODEL_DENSITY_TARGET = (
    "Density target: at least 12 distinct furnishing and decor items, "
    "at least 3 styled surfaces, 2 layered textiles per seating area."
)
REMODEL_LIGHTING_FALLBACK = (
    "Late afternoon sun with soft overcast weather enters from the main window "
    "wall as the only key light, with daylight at 5200 K. Two practical fixtures "
    "at 2700 K, a paper lantern pendant and a brass wall sconce, are decorative "
    "only and low output; they cast no shadows and add no fill light. Every shadow "
    "in the room runs away from the window wall. No bloom, no halo, no light spill "
    "on the ceiling, no lens flare, no double shadows; the window stays readable. "
    "Interior exposure is balanced for the room, not for the view outside. The key "
    "light reveals boucle nap, walnut grain and linen weave."
)
REMODEL_CHECK_PLAIN_EN = {
    "openings": "Restore every analysed opening on its original wall.",
    "count": "Restore the exact number of openings shown in the source photo.",
    "framing": "Restore the original wall framing and opening width.",
    "perspective": "Restore the original camera perspective.",
    "ceiling": "Restore the original ceiling construction and height.",
    "floor": (
        "Restore the original floor material and laying pattern, including plank "
        "direction, tile grid, herringbone or terrazzo."
    ),
    "fixed_features": "Restore every fixed architectural feature from the source photo.",
    "storage": (
        "Show one bookshelf, shelf unit or sideboard with books, plus one desk, "
        "console or writing surface."
    ),
    "lighting": (
        "Keep one shadow direction from the window wall, no visible halo or bloom "
        "around fixtures, no extra practical shadows and a readable window."
    ),
    "richness": (
        "Add at least ten distinct furnishing and decor items and at least three "
        "styled surfaces."
    ),
    "realism": (
        "Restore material micro-texture, grain and imperfections without plastic "
        "surfaces, HDR halo or CGI sheen."
    ),
}
FRAMING_SPAN_KEYS = (
    "front_wall_and_opening_span",
    "right_wall_span",
    "left_wall_span",
)
FRAMING_SCALAR_KEYS = (
    "ceiling_bottom_y",
    "floor_top_y",
    "opening_width_over_wall_width",
)
SELF_CHECK_MAX_TOKENS = 2500
SELF_CHECK_INSTRUCTION = (
    "Reply with the JSON object only, no prose, no markdown fences"
)
SELF_CHECK_SHORT_NOTE_WORDS = 12
REMODEL_NEGATIVE = (
    "people, text, watermark, logo, cartoon, illustration, CGI, 3d render, octane "
    "render, unreal engine, architectural visualization, archviz, showroom, plastic "
    "sheen, HDR glow, hdr, oversaturated colours, wide-angle distortion, fisheye, "
    "warped walls, extra doors, extra windows, extra wall panels, missing openings, "
    "changed room proportions, room widened, raised ceiling, duplicated furniture, "
    "floating furniture, blurry detail, flat lighting, duplicate light sources, "
    "mismatched shadow directions, multiple shadow directions, double shadows, bloom, "
    "glow halo around lamps, light spill on ceiling, blown out window, lens flare, "
    "mixed white balance, empty surfaces, showroom emptiness, cgi render, uniform "
    "flat materials, perfect symmetry, retouched glossy look, plastic reflections, "
    "plastic materials"
)
REMODEL_REALISM = (
    "REALISM\n"
    "Shot as an editorial interior photograph for a design magazine: 35 mm full-frame "
    "body, 24 mm tilt-shift kept level, natural imperfect surfaces - fabric wrinkles, "
    "curtain folds that are not perfectly symmetrical, faint scuffs on the skirting, "
    "real dust-free but lived-in tabletops, subtle sensor grain, gentle highlight "
    "rolloff. No retouching sheen, no HDR, no CGI, no archviz, no perfectly uniform "
    "material, no flawless symmetry, no plastic reflections."
)
FORBIDDEN_IMAGE_KEYS = {
    "image",
    "file",
    "ref_image",
    "img",
    "image_url",
    "image_base64",
    "photo",
    "reference_image",
}

# Explicit ranges from the product spec. Outside these ranges we choose the nearest.
ASPECT_RANGES = (
    (0.74, 0.76, 0.75, "1080*1440", "3:4 竖构图"),
    (1.32, 1.35, 4.0 / 3.0, "1440*1080", "4:3 横构图"),
    (0.99, 1.01, 1.0, "1152*1152", "1:1 方构图"),
    (1.76, 1.79, 16.0 / 9.0, "1440*810", "16:9 宽幅"),
)
ASPECT_HINTS = {
    "1080*1440": "Vertical 3:4 composition.",
    "1440*1080": "Horizontal 4:3 composition.",
    "1152*1152": "Square 1:1 composition.",
    "1440*810": "Widescreen 16:9 composition.",
}

REMODEL_VISION_PROMPT = """You are an architectural analyst preparing a text-only brief for a photorealistic interior remodel.

Analyze the attached photograph. Describe ONLY the immovable architecture, wall openings and camera. Ignore all furniture, soft furnishings, lighting fixtures, decor and colour schemes of movable objects: they will be replaced.

Estimate real-world dimensions from standard references: a door is about 2.0-2.1 m tall, a ceiling is 2.6-2.9 m high, a kitchen counter is 0.9 m high. Numbers may be approximate but must be internally consistent: width_m and depth_m must match the shape you describe, and center_offset_fraction is measured from 0.0 at the left edge of that wall to 1.0 at its right edge as seen by the camera.

Return STRICT JSON only, with no markdown fences and no prose outside the JSON object:
{
  "schema_version": 4,
  "room_type_en": "living room",
  "room_function": "living",
  "shape_en": "rectangular, wider than deep",
  "ceiling_en": "flat ceiling at standard 2.8 m height, plain white, no beams",
  "walls_en": "flat painted walls, one continuous plane each side, skirting at floor",
  "floor_en": "light oak planks laid lengthwise, continuous floor plane, no steps or level changes",
  "geometry": {
    "room_type_en": "living room",
    "shape_en": "rectangular, wider than deep",
    "width_m": 4.2,
    "depth_m": 3.1,
    "ceiling_m": 2.8,
    "floor_level_changes": false
  },
  "openings": [
    {"side": "left", "type": "open doorway", "width_m": 0.95, "height_m": 2.05, "sill_m": 0.0, "center_offset_fraction": 0.42, "detail": "no door leaf, dark threshold, leads to corridor"},
    {"side": "front-right", "type": "floor-to-ceiling sliding glass door", "width_m": 2.4, "height_m": 2.4, "sill_m": 0.0, "center_offset_fraction": 0.62, "detail": "black slim frame, four panes, transom window above"}
  ],
  "camera": {
    "height_m": 1.5,
    "distance_to_far_wall_m": 3.6,
    "lens_mm": 24,
    "tilt_deg": -3,
    "horizon_fraction": 0.55,
    "perspective": "one-point, straight at the far wall"
  },
  "framing": {
    "front_wall_and_opening_span": {"from_x": 0.10, "to_x": 0.90},
    "right_wall_span": {"from_x": 0.78, "to_x": 1.00},
    "left_wall_span": {"from_x": 0.00, "to_x": 0.12},
    "ceiling_bottom_y": 0.22,
    "floor_top_y": 0.68,
    "opening_width_over_wall_width": 0.85
  },
  "openings_total": 2,
  "fixed_features": [
    {"key": "ceiling cornice", "detail": "plain 80 mm plaster cornice along all walls"},
    {"key": "skirting board", "detail": "painted timber skirting, 90 mm high"}
  ],
  "camera_en": "camera at 1.5 m height, one-point perspective straight at the far wall, slight downward tilt, 24 mm equivalent wide lens",
  "light_en": "main daylight from the front-right glass door, soft fill from the ceiling fixture",
  "structure_en": "one dense paragraph of 80-130 English words describing ONLY immovable architecture, openings, camera and fixed features. The server rebuilds the final paragraph deterministically from the numeric fields, so the numbers in this draft are only a reference. No furniture, no decor, no colours of furnishings.",
  "immovable_zh": ["左墙标准门洞，通向走廊", "右前方落地推拉门带亮窗", "平顶 2.8 米，无吊顶"],
  "movable_zh": ["布艺沙发", "地毯", "电视柜"]
}

Rules:
- Every *_en value must be English only.
- room_function must be exactly one of living, dining, bedroom, kitchen, study, bathroom, entry, balcony, other, unknown. Judge it from the room's fixed architecture and principal furniture even though the movable pieces will be replaced.
- geometry.width_m and geometry.depth_m are the room's plan dimensions in metres; ceiling_m is the floor-to-ceiling height; floor_level_changes is true only when a step, split level or ramp is visible.
- floor_en must name the visible floor material and laying pattern (for example oak planks running lengthwise, square tile grid, herringbone, terrazzo) as well as any level change. This floor is fixed and must be preserved.
- openings must list every visible window, transom, door, doorway, arch or sliding/balcony door as ONE opening each. A transom above a sliding door is one opening whose detail mentions the transom; a window with a transom is one opening. side is one of left, right, front, front-left, front-right, back-left, back-right, back, ceiling, floor. Give each opening numeric width_m, height_m, sill_m (0.0 for doors) and center_offset_fraction.
- openings_total MUST equal the number of objects in openings.
- camera describes the photograph: height_m above the floor, distance_to_far_wall_m, lens_mm full-frame equivalent, tilt_deg (negative looks down), horizon_fraction 0.0-1.0 measured from the top of the frame, and perspective.
- perspective MUST describe the real camera in the photo. Start with one-point, two-point or three-point and add the angle, for example "two-point, angled toward the front-left corner" or "one-point, straight at the far wall". Do not copy the example if the photo differs.
- framing MUST be measured by looking at the uploaded photo, not guessed from the room dimensions. Report wall spans and horizontal lines, not shares of the frame:
  - from_x/to_x are fractions of the image width where that plane touches the frame edge; 0.00 is the left edge, 1.00 the right edge.
  - ceiling_bottom_y is the fraction of the image height where the ceiling plane meets the walls; floor_top_y where the far floor meets the far wall skirting.
  - opening_width_over_wall_width is how much of the wall carrying the main opening is glass/door: 1.0 = the opening spans the whole wall, 0.5 = half.
  - Estimate from visible edges only; if a plane is not visible, omit it.
- fixed_features lists at most 6 installed parts of the shell that must not disappear: ceiling cornice, skirting, trims, built-in wall panels, electrical boxes, air-conditioner units, window mullion/sash layout, installed ceiling fittings. Give a short key and a specific detail. Do NOT list furniture-style light fittings such as pendants or floor lamps, and do not list movable decor.
- structure_en is kept only as structure_en_model for debugging; the final structure paragraph is rebuilt in code from the numeric fields. It must be 80-130 English words if provided, and must not contain furniture, decor or furnishing colours.
- immovable_zh: at most 5 short Simplified-Chinese chips for the UI.
- movable_zh: at most 6 short Simplified-Chinese chips for the UI.
- Do not invent structure that is not visible. If a dimension is uncertain, use a neutral standard.
"""

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8765,
    "models": ["qwen-image-3.0-pro"],
    "sizes": ["1080*1440"],
}


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            config.update(data)
    config["models"] = [str(x) for x in config.get("models", []) if str(x).strip()]
    config["sizes"] = [str(x) for x in config.get("sizes", []) if str(x).strip()]
    if not config["models"]:
        config["models"] = list(DEFAULT_CONFIG["models"])
    if not config["sizes"]:
        config["sizes"] = list(DEFAULT_CONFIG["sizes"])
    return config


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _read_json_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    if not PIL_AVAILABLE:
        return None, None
    try:
        with Image.open(path) as opened:
            width, height = ImageOps.exif_transpose(opened).size
    except Exception:
        return None, None
    if width <= 0 or height <= 0:
        return None, None
    return width, height


ROOM_TYPE_ZH = {
    "living room": "客厅",
    "bedroom": "卧室",
    "dining room": "餐厅",
    "kitchen": "厨房",
    "bathroom": "卫生间",
    "study": "书房",
    "balcony": "阳台",
    "hallway": "玄关",
    "kids room": "儿童房",
    "home office": "书房",
}
DEFAULT_ITEM_NAME_RE = re.compile(r"^改造项目 [0-9a-f]{4,8}$")


def _normalize_room_function(value) -> str:
    """Map a room-function label or free-form name to the canonical enum.

    Returns an empty string when the value is missing or unrecognised so the
    caller can continue down its priority list.
    """
    key = " ".join(str(value or "").split()).strip().casefold()
    if not key:
        return ""
    for function, aliases in _ROOM_FUNCTION_ALIASES:
        for alias in aliases:
            if alias in key:
                return function
    return ""


def _room_function_from_name(name) -> str:
    """Project names are a strong user signal; only accept a known keyword."""
    return _normalize_room_function(name)


def _extra_room_function_override(value) -> str:
    """Return the room function when ``value`` is only a room word.

    The comparison is exact after removing whitespace/punctuation and one
    optional trailing qualifier (``改造``/``空间``/...). A sentence that merely
    contains a room name is therefore not treated as a room-type instruction.
    """
    text = _EXTRA_ROOM_STRIP_RE.sub("", str(value or "").casefold())
    if not text:
        return ""
    for function, aliases in _EXTRA_ROOM_ALIASES:
        for alias in aliases:
            if text == alias:
                return function
            for suffix in _EXTRA_ROOM_SUFFIXES:
                if text == alias + suffix:
                    return function
    return ""


def _additional_requirement_sentence(extra) -> str:
    """Render one ``Additional requirement`` sentence without double-prefixing."""
    text = " ".join(str(extra or "").split())
    if not text:
        return ""
    if text.casefold().startswith("additional requirement"):
        return text
    return "Additional requirement: " + text


def _prompt_has_disallowed_cjk(text: str, allow_verbatim_extra: bool = False) -> bool:
    """CJK guard that permits the explicit verbatim-extra marker.

    The verbatim fallback deliberately keeps the user's Chinese words so the
    intent is not replaced by an invented English sentence. Everything else in
    the prompt must still be English.
    """
    value = str(text or "")
    if not CJK_RE.search(value):
        return False
    if allow_verbatim_extra:
        value = re.sub(re.escape(EXTRA_VERBATIM_PREFIX) + r"[^\n]*", "", value)
    return bool(CJK_RE.search(value))


def room_type_zh(analysis: dict | None) -> str:
    """Map the analysis ``room_type_en`` to the short Chinese label."""
    if not isinstance(analysis, dict):
        return ""
    candidates = [analysis.get("room_type_en")]
    geometry = analysis.get("geometry")
    if isinstance(geometry, dict):
        candidates.append(geometry.get("room_type_en"))
    for raw in candidates:
        key = " ".join(str(raw or "").split()).strip().lower()
        if key in ROOM_TYPE_ZH:
            return ROOM_TYPE_ZH[key]
    return ""


def default_item_name(item_id: str, room_zh: str = "", style_zh: str = "") -> str:
    room = str(room_zh or "").strip()
    style = str(style_zh or "").strip()
    if room and style:
        return f"{room} · {style}"
    if room:
        return f"{room} · 未指定风格"
    return f"改造项目 {str(item_id or '')[:8]}"


def is_default_item_name(
    name: str, room_zh: str = "", styles: object = None
) -> bool:
    value = str(name or "").strip()
    if DEFAULT_ITEM_NAME_RE.fullmatch(value):
        return True
    room = str(room_zh or "").strip()
    if not room:
        return False
    if value == f"{room} · 未指定风格":
        return True
    known = {str(style) for style in (styles or []) if str(style).strip()}
    return value in {f"{room} · {style}" for style in known}


def latest_remodel_gen_style(
    item: dict, item_dir: Path | None = None, fallback: bool = True
) -> str:
    """The newest generation's style, optionally falling back to the item default."""
    directory = Path(item_dir) if item_dir is not None else None
    for gid in item.get("gens") or []:
        if directory is None:
            break
        meta = _read_json_file(directory / "gen" / f"{gid}.json") or {}
        style = str(meta.get("style") or "").strip()
        if style:
            return style
    return str(item.get("default_style") or "").strip() if fallback else ""


def remodel_thumb_dimensions(
    width: int | None,
    height: int | None,
    long_edge: int = REMODEL_THUMB_LONG_EDGE,
) -> tuple[int, int]:
    if not width or not height or width <= 0 or height <= 0:
        return long_edge, long_edge
    scale = float(long_edge) / float(max(width, height))
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def write_remodel_thumbnail(source_path: Path, target_path: Path) -> tuple[int, int]:
    """Write a metadata-free JPEG thumbnail with an exact ``long_edge``.

    Returns the thumbnail's pixel size.
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("缺少 Pillow，无法生成缩略图")
    with Image.open(source_path) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode != "RGB":
            image = image.convert("RGB")
        width, height = image.size
        target_width, target_height = remodel_thumb_dimensions(width, height)
        if (width, height) != (target_width, target_height):
            image = image.resize(
                (target_width, target_height), Image.Resampling.LANCZOS
            )
        image.info = {}
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
        quality = REMODEL_THUMB_QUALITY
        try:
            while True:
                image.save(
                    tmp,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )
                if (
                    tmp.stat().st_size <= REMODEL_THUMB_MAX_BYTES
                    or quality <= 60
                ):
                    break
                quality -= 5
            os.replace(tmp, target_path)
        finally:
            tmp.unlink(missing_ok=True)
    return target_width, target_height


def _migration_source_name(uploads_dir: Path, upload_id: str) -> str:
    sidecar = _read_json_file(uploads_dir / f"{upload_id}.json") or {}
    for key in ("original_name", "original_filename", "filename", "source_name"):
        raw = str(sidecar.get(key) or "").strip()
        if not raw:
            continue
        stem = Path(raw).name.rsplit(".", 1)[0].strip()
        if stem:
            return stem[:120]
    return f"改造项目 {upload_id[:8]}"


def migrate_remodel_projects(base: Path | None = None) -> dict:
    """Copy legacy uploads/jobs into the item tree. Never deletes or moves legacy files.

    Idempotent: a project is skipped when an item already carries the same
    ``legacy_upload_id``.  Copied files use deterministic names so a partial
    migration can be re-run without creating duplicates.
    """
    root = Path(base or REMODEL_DIR).resolve()
    uploads_dir = root / "uploads"
    jobs_dir = root / "jobs"
    items_dir = root / "items"
    stats = {
        "projects": 0,
        "gens": 0,
        "skipped": 0,
        "missing_source": 0,
        "errors": 0,
    }
    # Normalize items migrated by an earlier build: add the user-rename flag
    # and replace information-free default names with room/style defaults.
    if items_dir.is_dir():
        for item_path in sorted(items_dir.glob("*/item.json")):
            data = _read_json_file(item_path)
            if not data:
                continue
            item_id = str(data.get("item_id") or item_path.parent.name)
            if not ITEM_ID_RE.fullmatch(item_id):
                continue
            changed = False
            if "renamed_by_user" not in data:
                data["renamed_by_user"] = False
                changed = True
            name = str(data.get("name") or "")
            if not data.get("renamed_by_user") and DEFAULT_ITEM_NAME_RE.fullmatch(name):
                room_zh = room_type_zh(data.get("structure"))
                style_zh = latest_remodel_gen_style(
                    data, item_path.parent, fallback=False
                )
                new_name = default_item_name(item_id, room_zh, style_zh)
                if new_name != name:
                    data["name"] = new_name
                    changed = True
            if changed:
                atomic_write_json(item_path, data)

    if not jobs_dir.is_dir():
        return stats

    known_legacy: set[str] = set()
    if items_dir.is_dir():
        for item_path in items_dir.glob("*/item.json"):
            data = _read_json_file(item_path)
            if data:
                legacy = str(data.get("legacy_upload_id") or "")
                if legacy:
                    known_legacy.add(legacy)

    groups: dict[str, list[tuple[Path, dict]]] = defaultdict(list)
    for job_path in sorted(jobs_dir.glob("*.json")):
        job = _read_json_file(job_path)
        if not job:
            continue
        upload_id = str(job.get("upload_id") or "").strip()
        if not UPLOAD_ID_RE.fullmatch(upload_id):
            continue
        groups[upload_id].append((job_path, job))

    for upload_id, entries in groups.items():
        if upload_id in known_legacy:
            stats["skipped"] += 1
            continue
        item_id = upload_id
        item_dir = items_dir / item_id
        if (item_dir / "item.json").is_file():
            known_legacy.add(upload_id)
            stats["skipped"] += 1
            continue
        source_file = None
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = uploads_dir / f"{upload_id}{ext}"
            if candidate.is_file():
                source_file = candidate
                break
        if source_file is None:
            stats["missing_source"] += 1
            continue
        try:
            source_dir = item_dir / "source"
            gen_dir = item_dir / "gen"
            source_dir.mkdir(parents=True, exist_ok=True)
            gen_dir.mkdir(parents=True, exist_ok=True)
            sid = upload_id
            source_target = source_dir / f"{sid}{source_file.suffix.lower()}"
            if not source_target.exists():
                shutil.copy2(source_file, source_target)
            try:
                write_remodel_thumbnail(
                    source_target, item_dir / "thumb" / f"src-{sid}.jpg"
                )
            except Exception:
                pass
            analysis = _read_json_file(uploads_dir / f"{upload_id}.json")
            structure = analysis if analysis and str(analysis.get("structure_en") or "").strip() else None

            collected: list[tuple[float, str, str, dict]] = []
            created_values: list[tuple[float, str]] = []
            for job_path, job in entries:
                job_id = str(job.get("job_id") or job_path.stem)
                if not ITEM_ID_RE.fullmatch(job_id):
                    continue
                try:
                    created_ts = float(job.get("created_ts") or 0)
                except (TypeError, ValueError):
                    created_ts = 0.0
                created_at = str(job.get("created_at") or "").strip()
                if not created_ts and created_at:
                    try:
                        created_ts = datetime.fromisoformat(
                            created_at.replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        created_ts = 0.0
                if not created_at:
                    created_at = iso_from_timestamp(job_path.stat().st_mtime)
                if not created_ts:
                    created_ts = job_path.stat().st_mtime
                created_values.append((created_ts, created_at))

                pattern = re.compile(
                    r"^" + re.escape(job_id) + r"(?:-r\d+)?(?:-\d+)?\.png$"
                )
                for png_path in sorted(jobs_dir.glob(f"{job_id}*.png")):
                    if not pattern.fullmatch(png_path.name):
                        continue
                    gid = hashlib.sha256(
                        f"{upload_id}:{png_path.name}".encode("utf-8")
                    ).hexdigest()[:32]
                    png_target = gen_dir / f"{gid}.png"
                    if not png_target.exists():
                        shutil.copy2(png_path, png_target)
                    try:
                        write_remodel_thumbnail(
                            png_target, item_dir / "thumb" / f"{gid}.jpg"
                        )
                    except Exception:
                        pass
                    width, height = _image_dimensions(png_path)
                    width = width or job.get("width_final") or job.get("width")
                    height = height or job.get("height_final") or job.get("height")
                    mode = str(job.get("mode") or "text")
                    if mode not in REMODEL_MODES:
                        mode = "text"
                    size_final = str(job.get("size_final") or "").strip()
                    if not size_final and width and height:
                        size_final = f"{width}*{height}"
                    meta = {
                        "gid": gid,
                        "item_id": item_id,
                        "created_at": created_at,
                        "mode": mode,
                        "style": str(job.get("style") or ""),
                        "model": str(job.get("model") or ""),
                        "prompt": str(job.get("prompt") or ""),
                        "negative": str(job.get("negative") or ""),
                        "score": job.get("final_score"),
                        "attempts": job.get("attempts") or 0,
                        "size_requested": str(
                            job.get("size_requested") or job.get("size") or ""
                        ),
                        "size_final": size_final,
                        "width": width,
                        "height": height,
                        "model_seconds": round(
                            float(job.get("model_seconds") or 0), 1
                        ),
                        "total_seconds": round(float(job.get("seconds") or 0), 1),
                        "checks": job.get("checks")
                        if isinstance(job.get("checks"), list)
                        else [],
                        "source_sid": sid,
                        "job_id": job_id,
                        "note": "",
                    }
                    json_path = gen_dir / f"{gid}.json"
                    if not json_path.exists():
                        atomic_write_json(json_path, meta)
                    collected.append(
                        (created_ts, png_path.name, gid, meta)
                    )

            collected.sort(key=lambda row: (row[0], row[1]), reverse=True)
            gens = [row[2] for row in collected]
            newest = collected[0][3] if collected else {}
            if created_values:
                created_ts, created_at = min(created_values, key=lambda row: row[0])
            else:
                created_ts = item_dir.stat().st_mtime
                created_at = iso_from_timestamp(created_ts)
            updated_at = (
                str(newest.get("created_at") or "").strip()
                or iso_from_timestamp(time.time())
            )
            default_style = str(newest.get("style") or "").strip()
            default_mode = str(newest.get("mode") or "").strip()
            if default_mode not in REMODEL_MODES:
                default_mode = REMODEL_DEFAULT_MODE
            legacy_name = _migration_source_name(uploads_dir, upload_id)
            if DEFAULT_ITEM_NAME_RE.fullmatch(legacy_name):
                legacy_name = default_item_name(
                    item_id, room_type_zh(structure), default_style
                )
            item = {
                "item_id": item_id,
                "name": legacy_name,
                "renamed_by_user": False,
                "created_at": created_at,
                "updated_at": updated_at,
                "notes": "",
                "default_style": default_style,
                "default_mode": default_mode,
                "source_current": sid,
                "source_ids": [sid],
                "cover_gen": None,
                "structure": structure,
                "analysis_error": None,
                "gens": gens,
                "legacy_upload_id": upload_id,
            }
            atomic_write_json(item_dir / "item.json", item)
            known_legacy.add(upload_id)
            stats["projects"] += 1
            stats["gens"] += len(gens)
        except Exception as exc:
            stats["errors"] += 1
            print(
                f"[gui] remodel migration failed for {upload_id}: {clean_error(exc)}",
                file=sys.stderr,
            )
    if stats["projects"] or stats["gens"]:
        print(
            f"[gui] remodel migration: {stats['projects']} projects, "
            f"{stats['gens']} generations",
            file=sys.stderr,
        )
    return stats


def clean_error(exc: BaseException) -> str:
    message = str(exc).strip()
    message = re.sub(r"Bearer\s+\S+", "Bearer ***", message, flags=re.IGNORECASE)
    return (message or exc.__class__.__name__)[:400]


class RemodelCheckResponseError(RuntimeError):
    """The self-check answer was truncated or was not valid JSON."""


class RemodelCheckError(RuntimeError):
    """A failed self-check together with the diagnostic check entries to keep."""

    def __init__(self, message: str, checks: list[dict] | None = None):
        super().__init__(message)
        self.checks = list(checks or [])


class RemodelNetworkError(RuntimeError):
    """A transport-level generation failure; safe to retry once."""


class RemodelTimeoutError(RemodelNetworkError):
    """The generation gateway did not answer before the configured timeout."""


def iso_now_ms() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def call_with_deadline(func, deadline: float, timeout_message: str):
    """Run ``func`` in a daemon thread and give up at the wall-clock deadline.

    ``urlopen(timeout=...)`` only bounds individual socket operations: a
    gateway that trickles bytes can keep a request alive forever.  The daemon
    thread is abandoned on overrun (it dies with the process) so a stuck call
    can never block the job worker.
    """
    outcome: dict[str, object] = {}

    def runner() -> None:
        try:
            outcome["value"] = func()
        except BaseException as exc:  # Re-raised in the caller's thread.
            outcome["error"] = exc

    thread = threading.Thread(
        target=runner, name="remodel-deadline", daemon=True
    )
    thread.start()
    thread.join(max(0.0, float(deadline)))
    if thread.is_alive():
        raise RemodelTimeoutError(timeout_message)
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome.get("value")


def remodel_stage_budget(job: dict) -> float:
    """Wall-clock budget for the stage the job is currently in."""
    stage = str(job.get("stage") or "queued")
    mode = str(job.get("mode") or "text")
    if stage in {"requesting", "repairing"}:
        return float(remodel_image_request_options(mode)[0])
    if stage == "checking":
        return float(REMODEL_SELF_CHECK_TIMEOUT)
    if stage == "downloading":
        return float(REMODEL_DOWNLOAD_TIMEOUT)
    if stage in {"encoding", "analyzing"}:
        return float(REMODEL_ANALYSIS_TIMEOUT)
    if stage == "saving":
        return float(REMODEL_SAVE_TIMEOUT)
    if stage == "queued":
        return float(REMODEL_QUEUE_TIMEOUT)
    return float(REMODEL_QUEUE_TIMEOUT)


def reap_stalled_remodel_jobs(
    jobs: dict[str, dict], now_mono: float | None = None
) -> list[str]:
    """Fail running jobs whose current stage outlived its budget by 60 s.

    Returns the ids that were failed.  Pure over the supplied dict so the
    reaper is directly testable; ``GalleryStore`` handles logging/persistence.
    """
    now = time.monotonic() if now_mono is None else float(now_mono)
    failed: list[str] = []
    for job_id, job in list(jobs.items()):
        if not isinstance(job, dict) or job.get("kind") != "remodel":
            continue
        if job.get("status") not in {"queued", "running"}:
            continue
        started = job.get("stage_started_mono")
        if started is None:
            continue
        budget = remodel_stage_budget(job)
        if now - float(started) <= budget + REMODEL_REAPER_GRACE:
            continue
        started_at = iso_now_ms()
        job["status"] = "error"
        if str(job.get("stage") or "") == "analyzing":
            job["error"] = (
                f"分析原图超时（已等待 {int(round(budget))} 秒），"
                "请稍后重试或改用图生图"
            )
        else:
            job["error"] = remodel_timeout_message(
                budget, str(job.get("mode") or "text")
            )
        job["finished_mono"] = now
        created_mono = job.get("created_mono")
        if created_mono is not None:
            job["seconds"] = max(0.0, now - float(created_mono))
        job["stage"] = "error"
        job["stage_started_mono"] = now
        job["stage_started_at"] = started_at
        job["stage_seconds"] = 0.0
        job["heartbeat_at"] = started_at
        stage_log = job.setdefault("stage_log", [])
        stage_log.append(
            {
                "stage": "error",
                "at": started_at,
                "elapsed": round(
                    max(0.0, now - float(created_mono)), 1
                )
                if created_mono is not None
                else 0.0,
            }
        )
        failed.append(job_id)
    return failed


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def map_aspect(aspect: float) -> dict:
    """Map the upload's real aspect ratio to a supported output size."""
    for low, high, _target, size, label in ASPECT_RANGES:
        if low <= aspect <= high:
            return {"size": size, "label": label, "exact": True, "nearest": False, "note": ""}
    _low, _high, target, size, label = min(
        ASPECT_RANGES, key=lambda row: abs(aspect - row[2])
    )
    note = (
        f"原图比例 {aspect:.3f} 不在标准比例范围内，"
        f"使用最接近的 {label}（{size.replace('*', '×')}）"
    )
    return {"size": size, "label": label, "exact": False, "nearest": True, "note": note}


def describe_aspect(aspect: float) -> tuple[str, dict]:
    mapping = map_aspect(aspect)
    width, height = mapping["size"].split("*")
    prefix = "近似 " if mapping["nearest"] else ""
    return f"{prefix}{mapping['label']} · 输出 {width}×{height}", mapping


def crop_image_to_aspect(image, target_ratio: float, tolerance: float = 0.004):
    """Centre-crop an image to the target aspect ratio.

    Cropping only: never pads and never resizes up. Returns (image, changed).
    A ratio already within ``tolerance`` is left untouched.
    """
    if image is None:
        return image, False
    try:
        target = float(target_ratio)
    except (TypeError, ValueError):
        return image, False
    if not math.isfinite(target) or target <= 0:
        return image, False
    width, height = image.size
    if width <= 0 or height <= 0:
        return image, False
    current = width / height
    if abs(current - target) <= tolerance:
        return image, False
    if current > target:
        new_width = max(1, min(width, int(round(height * target))))
        left = (width - new_width) // 2
        box = (left, 0, left + new_width, height)
    else:
        new_height = max(1, min(height, int(round(width / target))))
        top = (height - new_height) // 2
        box = (0, top, width, top + new_height)
    return image.crop(box), True


def coerce_float(value, default=None, minimum=None, maximum=None):
    """Read a finite float from model output, accepting values such as "2.8 m"."""
    if isinstance(value, bool) or value is None:
        number = None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value))
        number = float(match.group(0)) if match else None
    if number is None or not math.isfinite(number):
        number = default
    if number is None:
        return None
    if minimum is not None:
        number = max(float(minimum), number)
    if maximum is not None:
        number = min(float(maximum), number)
    return number


def coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "y", "1"}:
        return True
    if text in {"false", "no", "n", "0"}:
        return False
    return default


def fmt_number(value, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(number):
        return "0"
    text = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def parse_multipart(body: bytes, content_type: str) -> dict[str, list[dict]]:
    """Minimal multipart/form-data parser; user-supplied filenames are recorded but never used."""
    match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type, re.IGNORECASE)
    boundary = ((match.group(1) or match.group(2)) if match else "").strip()
    if not boundary:
        raise ValueError("multipart 缺少 boundary")
    delimiter = b"--" + boundary.encode("latin-1", "ignore")
    result: dict[str, list[dict]] = {}
    for part in body.split(delimiter):
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if not part or part.startswith(b"--"):
            continue
        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        header_blob = part[:header_end].decode("latin-1", "replace")
        content = part[header_end + 4 :]
        headers: dict[str, str] = {}
        for line in header_blob.split("\r\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]*)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        result.setdefault(name, []).append(
            {
                "filename": filename_match.group(1) if filename_match else "",
                "content_type": headers.get("content-type", ""),
                "data": content,
            }
        )
    return result


def completion_content(response: dict) -> tuple[str, str]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("模型响应没有 choices")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = str(choice.get("finish_reason") or "")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        content = "".join(parts)
    return (content if isinstance(content, str) else ""), finish_reason


def parse_model_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("响应中没有 JSON 对象")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("JSON 顶层不是对象")
    return parsed


class GalleryStore:
    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.RLock()
        self.thumb_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.jobs: dict[str, dict] = {}
        self.job_queue: queue.Queue[str] = queue.Queue()
        self._job_prompts: dict[str, str] = {}

        self.metadata = self._load_metadata()
        self.meta_by_id = {item["id"]: item for item in self.metadata}
        self.csv_columns = self._load_csv_columns()
        self.prompt_base = self._load_prompt_base()
        self.style_by_id, self.style_errors = self._load_style_items()
        self.styles, self.categories = self._build_facets()
        self.search_text = self._build_search_index()
        self.overrides = self._load_overrides()
        self.generated_records = self._load_generated_records()
        self.samples_by_id, self.generated_ids = self._build_samples()
        self.style_packs = self._load_style_packs()
        self.style_counts = {
            str(entry.get("name_zh") or ""): int(entry.get("count") or 0)
            for entry in self.styles
            if entry.get("name_zh")
        }
        self.remodel_jobs: dict[str, dict] = {}
        self.item_lock = threading.RLock()
        self.remodel_thumb_lock = threading.RLock()

        try:
            self.migration_stats = migrate_remodel_projects(REMODEL_DIR)
        except Exception as exc:
            self.migration_stats = {}
            print(
                f"[gui] remodel migration skipped: {clean_error(exc)}",
                file=sys.stderr,
            )

        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        for index in range(2):
            worker = threading.Thread(
                target=self._generation_worker,
                name=f"image-worker-{index + 1}",
                daemon=True,
            )
            worker.start()

        reaper = threading.Thread(
            target=self._remodel_reaper_loop,
            name="remodel-reaper",
            daemon=True,
        )
        reaper.start()

    # ---------- loading ----------

    def _load_metadata(self) -> list[dict]:
        with (ROOT / "metadata.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise RuntimeError("metadata.json 不是数组")
        return [item for item in data if isinstance(item, dict) and item.get("id")]

    def _load_csv_columns(self) -> list[str]:
        path = ROOT / "metadata.csv"
        if not path.exists():
            columns: list[str] = []
            seen = set()
            for item in self.metadata:
                for key in item:
                    if key not in seen:
                        seen.add(key)
                        columns.append(key)
            return columns
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            return list(reader.fieldnames or [])

    def _load_prompt_base(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        path = STYLE_DIR / "prompts.csv"
        if path.exists():
            with path.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    sid = (row.get("id") or "").strip()
                    if sid:
                        out[sid] = {field: row.get(field, "") or "" for field in PROMPT_FIELDS}
        for item in self.metadata:
            sid = item["id"]
            base = out.setdefault(sid, {})
            for field in PROMPT_FIELDS:
                if not base.get(field):
                    base[field] = str(item.get(field) or "")
        return out

    def _load_style_items(self) -> tuple[dict[str, dict], list[str]]:
        out: dict[str, dict] = {}
        errors: list[str] = []
        directory = STYLE_DIR / "items"
        if not directory.exists():
            return out, errors
        for path in sorted(directory.glob("*.json")):
            try:
                with path.open(encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    out[path.stem] = data
            except Exception:
                errors.append(path.name)
        return out, errors

    def _load_style_packs(self) -> dict[str, dict]:
        if not STYLE_PACKS_PATH.exists():
            return {}
        try:
            with STYLE_PACKS_PATH.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            print(f"[gui] 无法读取风格包：{clean_error(exc)}", file=sys.stderr)
            return {}
        styles = data.get("styles") if isinstance(data, dict) else None
        if not isinstance(styles, dict):
            return {}
        out: dict[str, dict] = {}
        for style_zh, pack in styles.items():
            if not isinstance(pack, dict):
                continue
            if not all(
                str(pack.get(field) or "").strip()
                for field in ("style_en", "style_dna_en", "furniture_plan_en", "signature_zh")
            ):
                continue
            if not isinstance(pack.get("palette"), list):
                continue
            out[str(style_zh)] = pack
        return out

    def _build_facets(self) -> tuple[list[dict], list[dict]]:
        style_counts = Counter(
            (item.get("style_primary_zh") or "").strip()
            for item in self.metadata
            if (item.get("style_primary_zh") or "").strip()
        )
        category_counts = Counter(
            (item.get("category_zh") or item.get("category") or "").strip()
            for item in self.metadata
            if (item.get("category_zh") or item.get("category") or "").strip()
        )
        styles = [
            {"name_zh": name, "count": count}
            for name, count in sorted(style_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        categories = [
            {"name_zh": name, "count": count}
            for name, count in sorted(category_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return styles, categories

    def _build_search_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for item in self.metadata:
            sid = item["id"]
            style = self.style_by_id.get(sid, {})
            parts = [
                sid,
                item.get("name", ""),
                item.get("title_zh", ""),
                item.get("title_en", ""),
                item.get("category_zh", ""),
                item.get("category_en", ""),
                item.get("style_primary_zh", ""),
                item.get("style_primary_en", ""),
                item.get("style_secondary_zh", ""),
                item.get("style_tags_zh", ""),
            ]
            for color in style.get("palette", []) or []:
                parts.append(str(color.get("name_zh", "")))
                parts.append(str(color.get("name_en", "")))
                parts.append(str(color.get("hex", "")))
            for furniture in style.get("furniture", []) or []:
                if isinstance(furniture, dict):
                    parts.extend(
                        str(furniture.get(key, ""))
                        for key in ("item_zh", "item_en", "material_zh", "material_en", "color_zh", "color_en", "note_zh")
                    )
            for decor in style.get("decor", []) or []:
                if isinstance(decor, dict):
                    parts.extend(str(decor.get(key, "")) for key in ("item_zh", "item_en"))
            index[sid] = " ".join(parts).casefold()
        return index

    def _load_overrides(self) -> dict[str, dict]:
        if not OVERRIDES_PATH.exists():
            return {}
        try:
            with OVERRIDES_PATH.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {}
            clean: dict[str, dict] = {}
            for sid, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                kept = {field: str(entry[field]) for field in PROMPT_FIELDS if field in entry}
                if kept:
                    if entry.get("updated_at"):
                        kept["updated_at"] = str(entry["updated_at"])
                    clean[str(sid)] = kept
            return clean
        except Exception as exc:
            print(f"[gui] 无法读取提示词覆盖文件：{exc}", file=sys.stderr)
            return {}

    def _load_generated_records(self) -> list[dict]:
        if not GENERATED_SAMPLES_PATH.exists():
            return []
        try:
            with GENERATED_SAMPLES_PATH.open(encoding="utf-8") as fh:
                data = json.load(fh)
            return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []
        except Exception as exc:
            print(f"[gui] 无法读取生图记录：{exc}", file=sys.stderr)
            return []

    # ---------- samples ----------

    def _build_samples(self) -> tuple[dict[str, list[dict]], set[str]]:
        records_by_file: dict[str, dict] = {}

        def add_record(entry: dict) -> None:
            file_value = str(entry.get("file") or "").strip()
            if not file_value:
                return
            candidate = (ROOT / file_value).resolve()
            try:
                candidate.relative_to(SAMPLES_DIR.resolve())
            except ValueError:
                return
            if not candidate.is_file() or candidate.suffix.lower() not in SAMPLE_EXTENSIONS:
                return
            try:
                rel_root = candidate.relative_to(ROOT).as_posix()
                rel_samples = candidate.relative_to(SAMPLES_DIR.resolve()).as_posix()
            except ValueError:
                return
            stat = candidate.stat()
            created = str(entry.get("created_at") or "") or iso_from_timestamp(stat.st_mtime)
            records_by_file[rel_root] = {
                "file": rel_root,
                "url": "/samples/" + quote(rel_samples, safe="/"),
                "model": str(entry.get("model") or ""),
                "size": str(entry.get("size") or ""),
                "seconds": entry.get("seconds", 0) if isinstance(entry.get("seconds", 0), (int, float)) else 0,
                "created_at": created,
                "prompt_en": str(entry.get("prompt_en") or ""),
                "_mtime": stat.st_mtime,
            }

        index_path = SAMPLES_DIR / "index.json"
        if index_path.exists():
            try:
                with index_path.open(encoding="utf-8") as fh:
                    index_data = json.load(fh)
                if isinstance(index_data, list):
                    for entry in index_data:
                        if isinstance(entry, dict):
                            add_record(entry)
            except Exception as exc:
                print(f"[gui] 无法读取 samples/index.json：{exc}", file=sys.stderr)

        for entry in self.generated_records:
            add_record(entry)

        # Pick up samples created outside the GUI when their filename is
        # <id>.png or <id>-<unix timestamp>.png.
        known_ids = sorted(self.meta_by_id, key=len, reverse=True)
        if SAMPLES_DIR.exists():
            for path in SAMPLES_DIR.iterdir():
                if not path.is_file() or path.suffix.lower() not in SAMPLE_EXTENSIONS:
                    continue
                rel_root = path.relative_to(ROOT).as_posix()
                if rel_root in records_by_file:
                    continue
                stem = path.stem
                sample_id = None
                for sid in known_ids:
                    if stem == sid:
                        sample_id = sid
                        break
                    if stem.startswith(sid + "-") and stem[len(sid) + 1 :].isdigit():
                        sample_id = sid
                        break
                if sample_id:
                    add_record({"file": rel_root, "id": sample_id})

        by_id: dict[str, list[dict]] = defaultdict(list)
        for rel_root, record in records_by_file.items():
            stem = Path(rel_root).stem
            sample_id = None
            for sid in sorted(self.meta_by_id, key=len, reverse=True):
                if stem == sid or (stem.startswith(sid + "-") and stem[len(sid) + 1 :].isdigit()):
                    sample_id = sid
                    break
            if sample_id:
                by_id[sample_id].append(record)

        for records in by_id.values():
            records.sort(key=lambda row: (row["_mtime"], row["file"]))
            for row in records:
                row.pop("_mtime", None)
        return dict(by_id), set(by_id)

    def _sample_record(self, sid: str, path: Path, model: str, size: str, seconds: float, prompt_en: str) -> dict:
        rel_root = path.relative_to(ROOT).as_posix()
        rel_samples = path.relative_to(SAMPLES_DIR.resolve()).as_posix()
        return {
            "id": sid,
            "file": rel_root,
            "url": "/samples/" + quote(rel_samples, safe="/"),
            "model": model,
            "size": size,
            "seconds": round(seconds, 1),
            "created_at": iso_now(),
            "prompt_en": prompt_en,
        }

    def _append_generated_sample(self, record: dict) -> None:
        with self.lock:
            persisted = {k: v for k, v in record.items() if k != "url"}
            self.generated_records.append(persisted)
            atomic_write_json(GENERATED_SAMPLES_PATH, self.generated_records)
            self.samples_by_id, self.generated_ids = self._build_samples()

    def samples_for(self, sid: str) -> list[dict]:
        with self.lock:
            return [dict(entry) for entry in self.samples_by_id.get(sid, [])]

    # ---------- records ----------

    def prompt_value(self, sid: str, field: str) -> str:
        with self.lock:
            override = self.overrides.get(sid, {})
            if field in override:
                return str(override[field])
        return str(self.prompt_base.get(sid, {}).get(field, self.meta_by_id.get(sid, {}).get(field, "")) or "")

    def merged_record(self, sid: str) -> dict | None:
        meta = self.meta_by_id.get(sid)
        if not meta:
            return None
        style = self.style_by_id.get(sid, {})
        record = dict(meta)
        for key, value in style.items():
            if key not in PROMPT_FIELDS:
                record[key] = value
        record["style_detail"] = style
        for field in PROMPT_FIELDS:
            record[field] = self.prompt_value(sid, field)
        with self.lock:
            record["overridden"] = sid in self.overrides
            record["samples"] = [dict(entry) for entry in self.samples_by_id.get(sid, [])]
        record["thumb_url"] = self.thumb_url_for(sid)
        return record

    def thumb_url_for(self, sid: str) -> str:
        """Thumbnail URL carrying a content version so the immutable cache
        busts whenever the main image changes (regen, PNG → WebP swap...)."""
        meta = self.meta_by_id.get(sid) or {}
        version = str(meta.get("sha256") or "")[:8] or str(int(meta.get("bytes") or 0))
        base = f"/thumbs/{quote(sid, safe='')}.jpg"
        return f"{base}?v={version}" if version else base

    def summary_record(self, sid: str) -> dict:
        meta = self.meta_by_id[sid]
        style = self.style_by_id.get(sid, {})
        palette = []
        for color in style.get("palette", []) or []:
            if not isinstance(color, dict):
                continue
            palette.append(
                {
                    "hex": str(color.get("hex", "")),
                    "name_zh": str(color.get("name_zh", "")),
                    "share": color.get("share", 0),
                }
            )
        with self.lock:
            generated = 1 if self.samples_by_id.get(sid) else 0
            edited = sid in self.overrides
        return {
            "id": sid,
            "name": meta.get("name", ""),
            "category_zh": meta.get("category_zh") or meta.get("category", ""),
            "style_primary_zh": meta.get("style_primary_zh", ""),
            "style_confidence": meta.get("style_confidence", 0),
            "palette": palette,
            "thumb_url": self.thumb_url_for(sid),
            "generated": generated,
            "edited": edited,
        }

    def bootstrap(self) -> dict:
        with self.lock:
            generated_count = len(self.generated_ids)
            edited_count = len(self.overrides)
        return {
            "total": len(self.metadata),
            "styles": self.styles,
            "categories": self.categories,
            "models": list(self.config.get("models", [])),
            "sizes": list(self.config.get("sizes", [])),
            "generated_count": generated_count,
            "edited_count": edited_count,
        }

    @staticmethod
    def _split_values(values: list[str]) -> set[str]:
        out: set[str] = set()
        for value in values:
            for part in str(value).split(","):
                part = part.strip()
                if part:
                    out.add(part)
        return out

    def query_items(self, query: dict[str, list[str]]) -> dict:
        styles = self._split_values(query.get("style", []))
        categories = self._split_values(query.get("category", []))
        q = (query.get("q", [""])[0] or "").strip().casefold()
        generated_only = (query.get("generated", [""])[0] or "").lower() in {"1", "true", "yes"}
        sort_key = (query.get("sort", [""])[0] or "style").lower()

        with self.lock:
            generated_ids = set(self.generated_ids)
            overrides = set(self.overrides)
        selected: list[dict] = []
        for item in self.metadata:
            sid = item["id"]
            if styles and (item.get("style_primary_zh") or "") not in styles:
                continue
            if categories:
                category_key = item.get("category_zh") or item.get("category") or ""
                category_raw = item.get("category") or ""
                if category_key not in categories and category_raw not in categories:
                    continue
            if generated_only and sid not in generated_ids:
                continue
            if q and q not in self.search_text.get(sid, ""):
                continue
            selected.append(item)

        if sort_key == "name":
            selected.sort(key=lambda item: ((item.get("name") or ""), item["id"]))
        else:
            selected.sort(
                key=lambda item: (
                    (item.get("style_primary_zh") or ""),
                    (item.get("category") or ""),
                    int(item.get("sort_order") or 0),
                    (item.get("name") or ""),
                    item["id"],
                )
            )

        items = []
        for item in selected:
            sid = item["id"]
            summary = self.summary_record(sid)
            items.append(summary)
        return {"total": len(items), "items": items}

    # ---------- overlays ----------

    def save_prompt(self, sid: str, fields: dict) -> dict | None:
        if sid not in self.meta_by_id:
            return None
        entry = {str(k): str(v) for k, v in fields.items()}
        entry["updated_at"] = iso_now()
        with self.lock:
            next_overrides = dict(self.overrides)
            existing = dict(next_overrides.get(sid, {}))
            existing.update(entry)
            next_overrides[sid] = existing
            atomic_write_json(OVERRIDES_PATH, next_overrides)
            self.overrides = next_overrides
        return self.merged_record(sid)

    def restore_prompt(self, sid: str) -> dict | None:
        if sid not in self.meta_by_id:
            return None
        with self.lock:
            if sid in self.overrides:
                next_overrides = dict(self.overrides)
                next_overrides.pop(sid, None)
                atomic_write_json(OVERRIDES_PATH, next_overrides)
                self.overrides = next_overrides
        return self.merged_record(sid)

    # ---------- CSV ----------

    def export_csv(self) -> bytes:
        columns = list(self.csv_columns)
        for field in PROMPT_FIELDS:
            if field not in columns:
                columns.append(field)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for item in self.metadata:
            record = self.merged_record(item["id"]) or {}
            row = {}
            for column in columns:
                value = record.get(column, "")
                if value is None:
                    row[column] = ""
                elif value is True:
                    row[column] = "true"
                elif value is False:
                    row[column] = "false"
                else:
                    row[column] = str(value)
            writer.writerow(row)
        return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")

    # ---------- generation ----------

    def submit_generation(self, sid: str, body: dict) -> str:
        if sid not in self.meta_by_id:
            raise ValueError("条目不存在")
        prompt = body.get("prompt", "")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("提示词不能为空")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"提示词不能超过 {MAX_PROMPT_CHARS} 个字符")
        model = body.get("model") or (self.config["models"][0] if self.config["models"] else "")
        size = body.get("size") or (self.config["sizes"][0] if self.config["sizes"] else "")
        if model not in self.config.get("models", []):
            raise ValueError("不支持的模型")
        if size not in self.config.get("sizes", []):
            raise ValueError("不支持的尺寸")
        count = body.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 4:
            raise ValueError("count 必须是 1 到 4 的整数")

        job_id = uuid.uuid4().hex
        with self.lock:
            self._job_prompts[job_id] = prompt
            self.jobs[job_id] = {
                "id": job_id,
                "item_id": sid,
                "status": "queued",
                "created_wall": time.time(),
                "started_mono": None,
                "finished_mono": None,
                "seconds": 0.0,
                "image_url": None,
                "file": None,
                "error": None,
                "model": model,
                "size": size,
                "count": count,
            }
        self.job_queue.put(job_id)
        return job_id

    def job_snapshot(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            seconds = job["seconds"]
            if job["status"] == "running" and job["started_mono"] is not None:
                seconds = time.monotonic() - job["started_mono"]
            return {
                "status": job["status"],
                "seconds": round(float(seconds), 1),
                "image_url": job["image_url"],
                "file": job["file"],
                "error": job["error"],
            }

    def _generation_worker(self) -> None:
        while True:
            job_id = self.job_queue.get()
            job = None
            is_remodel = False
            try:
                with self.lock:
                    job = self.jobs.get(job_id) or self.remodel_jobs.get(job_id)
                    if not job:
                        continue
                    is_remodel = job.get("kind") == "remodel"
                    job["status"] = "running"
                    job["started_mono"] = time.monotonic()
                if is_remodel:
                    self._set_remodel_stage(job_id, "encoding")
                    self._run_remodel_job(job_id)
                else:
                    self._run_generation_job(job_id)
            except Exception as exc:
                with self.lock:
                    job = self.jobs.get(job_id) or self.remodel_jobs.get(job_id)
                    if job:
                        already_failed = job.get("status") == "error"
                        if not already_failed:
                            job["status"] = "error"
                            job["error"] = clean_error(exc)
                            job["finished_mono"] = time.monotonic()
                            if job.get("started_mono") is not None:
                                job["seconds"] = (
                                    job["finished_mono"] - job["started_mono"]
                                )
                if is_remodel:
                    self._set_remodel_stage(job_id, "error")
                    try:
                        self._write_remodel_job_json(job_id)
                    except Exception:
                        pass
            finally:
                self.job_queue.task_done()

    def _run_generation_job(self, job_id: str) -> None:
        with self.lock:
            job = dict(self.jobs[job_id])
            prompt = self._job_prompts.pop(job_id, "")
        sid = job["item_id"]
        if not prompt:
            raise RuntimeError("生成提示词丢失")
        key, base_url = self._read_credentials()
        images: list[dict] = []
        started = time.monotonic()
        for index in range(int(job["count"])):
            image_url = self._request_image(base_url, key, prompt, str(job["model"]), str(job["size"]))
            target = self._download_image(sid, image_url)
            elapsed = time.monotonic() - started
            record = self._sample_record(
                sid,
                target,
                str(job["model"]),
                str(job["size"]),
                elapsed,
                prompt,
            )
            self._append_generated_sample(record)
            images.append({"image_url": record["url"], "file": record["file"]})
        with self.lock:
            live_job = self.jobs[job_id]
            live_job["status"] = "done"
            live_job["finished_mono"] = time.monotonic()
            live_job["seconds"] = live_job["finished_mono"] - (live_job["started_mono"] or live_job["finished_mono"])
            if images:
                live_job["image_url"] = images[-1]["image_url"]
                live_job["file"] = images[-1]["file"]
            live_job["images"] = images

    def _read_credentials(self) -> tuple[str, str]:
        """每次调用实时解析，保存后无需重启即可生效。"""
        key, base_url, _source = model_credentials.resolve_model_credentials()
        return key, base_url

    def credentials_status(self) -> dict:
        return model_credentials.credentials_snapshot()

    def save_credentials(self, api_key, base_url=None) -> dict:
        if not isinstance(api_key, str) or len(api_key.strip()) <= 20:
            raise ValueError("API Key 长度必须大于 20 个字符")
        if base_url is not None and not isinstance(base_url, str):
            raise ValueError("base_url 必须是字符串")
        cleaned_base = str(base_url or "").strip()
        if not cleaned_base:
            try:
                _key, cleaned_base, _source = (
                    model_credentials.resolve_model_credentials()
                )
            except RuntimeError:
                cleaned_base = model_credentials.DEFAULT_BASE_URL
        return model_credentials.save_local_credentials(
            api_key,
            cleaned_base,
            credentials_path=model_credentials.CREDENTIALS_PATH,
        )

    def clear_credentials(self) -> dict:
        return model_credentials.delete_local_credentials(
            credentials_path=model_credentials.CREDENTIALS_PATH,
        )

    @staticmethod
    def _build_image_payload(
        prompt: str,
        model: str,
        size: str,
        image_data_url: str | None = None,
    ) -> dict:
        """Build the gateway body and enforce the image-part contract.

        ``img2img`` sends exactly one image part (the user's own photo) and one
        text part. ``text`` sends zero image parts. A style reference image is
        never passed in by any caller.
        """
        content: list[dict] = []
        if image_data_url is not None:
            value = str(image_data_url)
            if not value.startswith("data:image/jpeg;base64,"):
                raise ValueError("img2img 图片必须是 JPEG data URL")
            content.append({"image": value})
        content.append({"text": prompt})
        image_parts = sum(1 for part in content if "image" in part)
        text_parts = sum(1 for part in content if "text" in part)
        expected_images = 1 if image_data_url is not None else 0
        if image_parts != expected_images or text_parts != 1:
            raise RuntimeError(
                "图片请求图片数量不合法："
                f"期望 {expected_images} 张图 / 1 段文字，实际 {image_parts} / {text_parts}"
            )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "n": 1,
            "watermark": False,
        }
        if image_data_url is None:
            # The probed img2img request omits size: the model follows the input
            # image's aspect ratio. Text-only keeps the advisory size hint.
            payload["size"] = size
        return payload

    def _request_image(
        self,
        base_url: str,
        key: str,
        prompt: str,
        model: str,
        size: str,
        image_data_url: str | None = None,
        timeout: float = 600,
        retry_network: bool = False,
        timeout_mode: str = "img2img",
    ) -> str:
        payload = self._build_image_payload(prompt, model, size, image_data_url)
        request = Request(
            base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        network_attempts = 2 if retry_network else 1
        result = None
        for network_attempt in range(1, network_attempts + 1):

            def fetch() -> dict:
                with urlopen(request, timeout=timeout) as response:
                    return json.load(response)

            try:
                result = call_with_deadline(
                    fetch,
                    timeout,
                    remodel_timeout_message(timeout, timeout_mode),
                )
                break
            except RemodelTimeoutError:
                raise
            except HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "ignore")[:240]
                except Exception:
                    detail = ""
                raise RuntimeError(
                    f"生成接口返回 HTTP {exc.code}" + (f"：{detail}" if detail else "")
                ) from None
            except (URLError, TimeoutError, ConnectionError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
                    # A timeout means the gateway is still rendering/queueing.
                    # Retrying would only double an already long wait.
                    raise RemodelTimeoutError(
                        remodel_timeout_message(timeout, timeout_mode)
                    ) from None
                if network_attempt < network_attempts:
                    continue
                raise RemodelNetworkError(
                    f"无法连接生成接口：{reason}（已重试 {network_attempts} 次）"
                ) from None
            except Exception as exc:
                raise RuntimeError(f"生成请求失败：{clean_error(exc)}") from None

        try:
            image_url = result["output"]["choices"][0]["message"]["content"][0]["image"]
        except (KeyError, IndexError, TypeError):
            request_id = result.get("request_id") if isinstance(result, dict) else ""
            suffix = f"（request_id: {request_id}）" if request_id else ""
            raise RuntimeError("生成接口未返回图片" + suffix) from None
        if not isinstance(image_url, str) or not image_url:
            raise RuntimeError("生成接口返回了空图片地址")
        return image_url

    def _download_image(self, sid: str, image_url: str) -> Path:
        def fetch() -> bytes:
            with urlopen(
                Request(image_url, headers={"User-Agent": "homai-gallery-gui/1.0"}),
                timeout=REMODEL_DOWNLOAD_TIMEOUT,
            ) as response:
                return response.read()

        try:
            data = call_with_deadline(
                fetch,
                REMODEL_DOWNLOAD_TIMEOUT,
                f"生成图下载超时（已等待 {int(REMODEL_DOWNLOAD_TIMEOUT)} 秒），稍后重试",
            )
        except RemodelTimeoutError:
            raise
        except HTTPError as exc:
            raise RuntimeError(f"生成图下载失败（HTTP {exc.code}）") from None
        except Exception:
            raise RuntimeError("生成图下载失败") from None
        if not data:
            raise RuntimeError("生成图内容为空")
        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())
        while True:
            target = SAMPLES_DIR / f"{sid}-{timestamp}.png"
            if not target.exists():
                break
            timestamp += 1
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return target

    # ---------- old-house remodel ----------

    @staticmethod
    def _append_remodel_stage_log(job_id: str, stage: str, elapsed: float) -> None:
        line = f"remodel {job_id[:8]} {stage} +{elapsed:.1f}s\n"
        try:
            GUI_SERVICE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _STAGE_LOG_LOCK:
                with GUI_SERVICE_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError:
            pass

    def _set_remodel_stage(
        self, job_id: str, stage: str, persist: bool = True
    ) -> None:
        """Advance a job's stage, reset its stage clock, and heartbeat."""
        if stage not in REMODEL_STAGES:
            return
        now_mono = time.monotonic()
        with self.lock:
            job = self.remodel_jobs.get(job_id)
            if job is None or job.get("stage") == stage:
                return
            if job.get("status") == "error" and stage != "error":
                # A reaped job stays failed even if its abandoned worker wakes
                # up and tries to advance.
                return
            created_mono = job.get("created_mono")
            elapsed = (
                max(0.0, now_mono - float(created_mono))
                if created_mono is not None
                else 0.0
            )
            stamp = iso_now_ms()
            job["stage"] = stage
            job["stage_started_mono"] = now_mono
            job["stage_started_at"] = stamp
            job["stage_seconds"] = 0.0
            job["heartbeat_at"] = stamp
            job.setdefault("stage_log", []).append(
                {"stage": stage, "at": stamp, "elapsed": round(elapsed, 1)}
            )
        self._append_remodel_stage_log(job_id, stage, elapsed)
        if persist:
            try:
                self._write_remodel_job_json(job_id)
            except Exception:
                pass

    def _touch_remodel_stage(self, job_id: str, stage: str | None = None) -> None:
        """Reset the stage clock without logging a transition.

        Multi-call stages (self-check full + short retry) refresh this between
        calls so the reaper sees progress, not one long silent stall.
        """
        if not job_id:
            return
        with self.lock:
            job = self.remodel_jobs.get(job_id)
            if job is None or job.get("status") != "running":
                return
            if stage is not None and job.get("stage") != stage:
                return
            now = time.monotonic()
            stamp = iso_now_ms()
            job["stage_started_mono"] = now
            job["stage_started_at"] = stamp
            job["stage_seconds"] = 0.0
            job["heartbeat_at"] = stamp

    def _reap_stalled_remodel_jobs(self) -> list[str]:
        with self.lock:
            failed = reap_stalled_remodel_jobs(self.remodel_jobs)
            details: list[tuple[str, float]] = []
            for job_id in failed:
                job = self.remodel_jobs.get(job_id) or {}
                created_mono = job.get("created_mono")
                elapsed = (
                    max(0.0, time.monotonic() - float(created_mono))
                    if created_mono is not None
                    else 0.0
                )
                details.append((job_id, elapsed))
        for job_id, elapsed in details:
            self._append_remodel_stage_log(job_id, "error", elapsed)
            try:
                self._write_remodel_job_json(job_id)
            except Exception:
                pass
        return failed

    def _remodel_reaper_loop(self) -> None:
        while True:
            time.sleep(REMODEL_REAPER_INTERVAL)
            try:
                self._reap_stalled_remodel_jobs()
            except Exception:
                pass

    def remodel_packs(self) -> list[dict]:
        out = []
        for style_zh, pack in self.style_packs.items():
            count = int(pack.get("sample_count") or self.style_counts.get(style_zh, 0))
            if count < 3:
                continue
            palette = []
            for entry in pack.get("palette") or []:
                if not isinstance(entry, dict):
                    continue
                hex_value = str(entry.get("hex") or "").strip()
                if not hex_value:
                    continue
                palette.append(
                    {"hex": hex_value, "name_en": str(entry.get("name_en") or "")}
                )
            out.append(
                {
                    "style_zh": style_zh,
                    "style_en": str(pack.get("style_en") or ""),
                    "signature_zh": str(pack.get("signature_zh") or ""),
                    "palette": palette,
                    "sample_count": count,
                }
            )
        out.sort(key=lambda entry: (-int(entry["sample_count"]), entry["style_zh"]))
        return out

    def _find_style_pack(self, style: str) -> tuple[str, dict | None]:
        wanted = str(style or "").strip()
        if not wanted:
            return "", None
        if wanted in self.style_packs:
            return wanted, self.style_packs[wanted]
        folded = wanted.casefold()
        for style_zh, pack in self.style_packs.items():
            if str(pack.get("style_en") or "").casefold() == folded:
                return style_zh, pack
        return "", None

    # ---------- remodel items ----------

    @staticmethod
    def _item_id_ok(item_id: str) -> bool:
        return bool(ITEM_ID_RE.fullmatch(str(item_id or "")))

    def _item_dir(self, item_id: str, create: bool = False) -> Path | None:
        if not self._item_id_ok(item_id):
            return None
        base = REMODEL_ITEM_DIR.resolve()
        candidate = (base / item_id).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            return None
        if create:
            candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def _read_item(self, item_id: str) -> dict | None:
        item_dir = self._item_dir(item_id)
        if item_dir is None:
            return None
        data = _read_json_file(item_dir / "item.json")
        if not data or str(data.get("item_id") or "") != item_id:
            return None
        data.setdefault("gens", [])
        data.setdefault("source_ids", [])
        return data

    def _write_item(self, item: dict) -> None:
        item_id = str(item.get("item_id") or "")
        item_dir = self._item_dir(item_id, create=True)
        if item_dir is None:
            raise ValueError("无效的项目 ID")
        atomic_write_json(item_dir / "item.json", item)

    def _item_source_path(self, item: dict, sid: str | None = None) -> Path | None:
        item_id = str(item.get("item_id") or "")
        item_dir = self._item_dir(item_id)
        if item_dir is None:
            return None
        wanted = str(sid or item.get("source_current") or "")
        if not ITEM_ID_RE.fullmatch(wanted):
            return None
        for path in sorted((item_dir / "source").glob(f"{wanted}.*")):
            if path.is_file() and path.suffix.lower() in REMODEL_IMAGE_EXTENSIONS:
                return path
        return None

    @staticmethod
    def _item_source_url(item_id: str, filename: str) -> str:
        return (
            "/remodel/items/"
            + quote(item_id, safe="")
            + "/source/"
            + quote(filename, safe="")
        )

    @staticmethod
    def _item_gen_url(item_id: str, gid: str) -> str:
        return (
            "/remodel/items/"
            + quote(item_id, safe="")
            + "/gen/"
            + quote(gid, safe="")
            + ".png"
        )

    @staticmethod
    def _item_gen_thumb_url(item_id: str, gid: str) -> str:
        return (
            "/remodel/items/"
            + quote(item_id, safe="")
            + "/thumb/"
            + quote(gid, safe="")
            + ".jpg"
        )

    @staticmethod
    def _item_source_thumb_url(item_id: str, sid: str) -> str:
        return (
            "/remodel/items/"
            + quote(item_id, safe="")
            + "/thumb/src-"
            + quote(sid, safe="")
            + ".jpg"
        )

    def _remodel_thumb_path(self, item_id: str, name: str) -> Path | None:
        item_dir = self._item_dir(item_id)
        if item_dir is None:
            return None
        return item_dir / "thumb" / name

    def _ensure_remodel_thumb(
        self, item_id: str, name: str, source_path: Path | None
    ) -> Path | None:
        """Create one item thumbnail if missing. Never overwrites good thumbs."""
        if source_path is None or not source_path.is_file():
            return None
        target = self._remodel_thumb_path(item_id, name)
        if target is None:
            return None
        if target.is_file() and target.stat().st_size > 0:
            return target
        if not PIL_AVAILABLE:
            return None
        with self.remodel_thumb_lock:
            if target.is_file() and target.stat().st_size > 0:
                return target
            try:
                write_remodel_thumbnail(source_path, target)
                return target
            except Exception as exc:
                print(
                    f"[gui] 改造缩略图生成失败 {item_id[:8]}/{name}: "
                    f"{clean_error(exc)}",
                    file=sys.stderr,
                )
                return None

    def _ensure_source_thumb(
        self, item: dict, source_path: Path | None = None, sid: str | None = None
    ) -> Path | None:
        item_id = str(item.get("item_id") or "")
        source_sid = str(sid or item.get("source_current") or "")
        if not ITEM_ID_RE.fullmatch(source_sid):
            return None
        path = source_path or self._item_source_path(item, source_sid)
        return self._ensure_remodel_thumb(
            item_id, f"src-{source_sid}.jpg", path
        )

    def _ensure_gen_thumb(
        self, item_id: str, gid: str, png_path: Path | None = None
    ) -> Path | None:
        if not GEN_ID_RE.fullmatch(str(gid or "")):
            return None
        if png_path is None:
            item_dir = self._item_dir(item_id)
            if item_dir is None:
                return None
            png_path = item_dir / "gen" / f"{gid}.png"
        return self._ensure_remodel_thumb(item_id, f"{gid}.jpg", png_path)

    def _ensure_item_thumbnails(
        self, item: dict, gens: list[dict] | None = None, all_gens: bool = False
    ) -> None:
        """Lazily backfill the thumbs a list/detail response is about to serve."""
        item_id = str(item.get("item_id") or "")
        if not item_id:
            return
        source_path = self._item_source_path(item)
        if source_path is not None:
            self._ensure_source_thumb(item, source_path=source_path)
        if gens is None:
            gens = self._item_gens(item)
        if all_gens:
            for sid in item.get("source_ids") or []:
                sid_text = str(sid)
                if not ITEM_ID_RE.fullmatch(sid_text):
                    continue
                path = self._item_source_path(item, sid_text)
                if path is not None:
                    self._ensure_source_thumb(
                        item, source_path=path, sid=sid_text
                    )
            for gen in gens:
                self._ensure_gen_thumb(item_id, str(gen.get("gid") or ""))
            return
        cover_gid = self._item_cover_gid(item, gens) or (
            str(gens[0].get("gid") or "") if gens else None
        )
        if cover_gid:
            self._ensure_gen_thumb(item_id, cover_gid)

    def backfill_remodel_thumbnails(self) -> dict:
        """Create every missing item thumbnail. Idempotent; prints counts."""
        stats = {
            "items": 0,
            "sources": 0,
            "generations": 0,
            "created": 0,
            "skipped": 0,
            "missing": 0,
            "errors": 0,
        }
        if not REMODEL_ITEM_DIR.is_dir():
            print(
                "[gui] remodel thumbnails backfill: no items directory",
                file=sys.stderr,
            )
            return stats
        for item_path in sorted(REMODEL_ITEM_DIR.glob("*/item.json")):
            data = _read_json_file(item_path)
            if not data:
                continue
            item_id = str(data.get("item_id") or item_path.parent.name)
            if not ITEM_ID_RE.fullmatch(item_id):
                continue
            item_dir = item_path.parent
            stats["items"] += 1
            for source_path in sorted((item_dir / "source").glob("*")):
                if (
                    not source_path.is_file()
                    or source_path.suffix.lower() not in REMODEL_IMAGE_EXTENSIONS
                ):
                    continue
                sid = source_path.stem
                if not ITEM_ID_RE.fullmatch(sid):
                    continue
                stats["sources"] += 1
                target = item_dir / "thumb" / f"src-{sid}.jpg"
                before = target.is_file() and target.stat().st_size > 0
                self._ensure_remodel_thumb(
                    item_id, f"src-{sid}.jpg", source_path
                )
                after = target.is_file() and target.stat().st_size > 0
                if before:
                    stats["skipped"] += 1
                elif after:
                    stats["created"] += 1
                else:
                    stats["errors"] += 1
            for png_path in sorted((item_dir / "gen").glob("*.png")):
                gid = png_path.stem
                if not GEN_ID_RE.fullmatch(gid):
                    continue
                stats["generations"] += 1
                target = item_dir / "thumb" / f"{gid}.jpg"
                before = target.is_file() and target.stat().st_size > 0
                self._ensure_remodel_thumb(item_id, f"{gid}.jpg", png_path)
                after = target.is_file() and target.stat().st_size > 0
                if before:
                    stats["skipped"] += 1
                elif after:
                    stats["created"] += 1
                else:
                    stats["errors"] += 1
        print(
            "[gui] remodel thumbnails backfill: "
            f"items={stats['items']} sources={stats['sources']} "
            f"generations={stats['generations']} created={stats['created']} "
            f"skipped={stats['skipped']} errors={stats['errors']}",
            file=sys.stderr,
        )
        return stats

    def _failed_jobs_for_item(
        self, item_id: str, source_thumb_url: str | None = None
    ) -> list[dict]:
        """Failed item jobs, newest first, so the UI can show what happened."""
        out: list[dict] = []
        for job in self._persisted_item_jobs():
            if str(job.get("item_id") or "") != item_id:
                continue
            if job.get("status") != "error":
                continue
            try:
                created_ts = float(job.get("created_ts") or 0)
            except (TypeError, ValueError):
                created_ts = 0.0
            out.append(
                {
                    "job_id": str(job.get("job_id") or ""),
                    "item_id": item_id,
                    "gen_id": job.get("gen_id"),
                    "status": "error",
                    "stage": str(job.get("stage") or "error"),
                    "error": str(job.get("error") or ""),
                    "style": str(job.get("style") or ""),
                    "style_en": str(job.get("style_en") or ""),
                    "mode": str(job.get("mode") or "text"),
                    "created_at": str(job.get("created_at") or ""),
                    "seconds": round(float(job.get("seconds") or 0), 1),
                    "note": str(job.get("note") or ""),
                    "url": None,
                    "thumb_url": source_thumb_url,
                    "preview_url": str(job.get("preview_url") or ""),
                    "_sort": created_ts,
                }
            )
        out.sort(key=lambda entry: entry.get("_sort") or 0.0, reverse=True)
        for entry in out:
            entry.pop("_sort", None)
        return out

    def _default_style_value(self) -> str:
        packs = self.remodel_packs()
        return str(packs[0].get("style_zh") or "") if packs else ""

    @staticmethod
    def _inspect_image_bytes(data: bytes) -> dict:
        if not PIL_AVAILABLE:
            raise ValueError("缺少 Pillow，无法处理上传图片")
        if not data:
            raise ValueError("图片内容为空")
        if len(data) > MAX_REMODEL_UPLOAD_BYTES:
            raise ValueError("图片不能超过 12 MB")
        fmt = ""
        try:
            with Image.open(io.BytesIO(data)) as opened:
                fmt = str(opened.format or "").upper()
                opened.verify()
        except Exception:
            raise ValueError("无法识别的图片文件（仅支持 JPEG/PNG/WebP）") from None
        if fmt not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("只支持 JPEG、PNG、WebP 图片")
        try:
            with Image.open(io.BytesIO(data)) as opened:
                width, height = ImageOps.exif_transpose(opened).size
        except Exception:
            raise ValueError("无法读取图片尺寸") from None
        if width <= 0 or height <= 0:
            raise ValueError("图片尺寸无效")
        ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[fmt]
        aspect = width / height
        label, mapping = describe_aspect(aspect)
        return {
            "ext": ext,
            "width": width,
            "height": height,
            "aspect": round(aspect, 4),
            "aspect_label": label,
            "mapping": mapping,
        }

    def _new_gen_id(self, item_id: str) -> str:
        item_dir = self._item_dir(item_id)
        if item_dir is None:
            raise ValueError("无效的项目 ID")
        gen_dir = item_dir / "gen"
        for _ in range(64):
            gid = uuid.uuid4().hex
            if not (gen_dir / f"{gid}.png").exists() and not (
                gen_dir / f"{gid}.json"
            ).exists():
                return gid
        raise RuntimeError("无法分配新的版本 ID")

    def _expanded_gen(self, item_id: str, gid: str) -> dict | None:
        item_dir = self._item_dir(item_id)
        if item_dir is None or not GEN_ID_RE.fullmatch(str(gid or "")):
            return None
        meta = _read_json_file(item_dir / "gen" / f"{gid}.json")
        if not meta:
            return None
        meta = dict(meta)
        meta["url"] = self._item_gen_url(item_id, gid)
        meta["thumb_url"] = self._item_gen_thumb_url(item_id, gid)
        thumb_width, thumb_height = meta.get("width"), meta.get("height")
        if not thumb_width or not thumb_height:
            thumb_path = item_dir / "thumb" / f"{gid}.jpg"
            if thumb_path.is_file():
                thumb_width, thumb_height = _image_dimensions(thumb_path)
        meta["thumb_width"], meta["thumb_height"] = remodel_thumb_dimensions(
            thumb_width, thumb_height
        )
        return meta

    def _item_gens(self, item: dict) -> list[dict]:
        item_id = str(item.get("item_id") or "")
        out: list[dict] = []
        for gid in item.get("gens") or []:
            expanded = self._expanded_gen(item_id, str(gid))
            if expanded:
                out.append(expanded)
        return out

    def _latest_item_gen(self, item: dict) -> dict | None:
        """Newest existing gen PNG, ordered by the gen json ``created_at``."""
        item_id = str(item.get("item_id") or "")
        item_dir = self._item_dir(item_id)
        if item_dir is None:
            return None
        best: dict | None = None
        best_at = ""
        for raw_gid in item.get("gens") or []:
            gid = str(raw_gid or "")
            if not GEN_ID_RE.fullmatch(gid):
                continue
            png_path = item_dir / "gen" / f"{gid}.png"
            if not png_path.is_file():
                continue
            meta = _read_json_file(item_dir / "gen" / f"{gid}.json") or {}
            created_at = str(meta.get("created_at") or "")
            # item["gens"] is newest-first, so an equal/empty timestamp keeps
            # the earlier entry instead of reshuffling the order.
            if best is None or created_at > best_at:
                best = {
                    "gid": gid,
                    "path": png_path,
                    "meta": meta,
                    "created_at": created_at,
                }
                best_at = created_at
        return best

    def _persisted_item_jobs(self) -> list[dict]:
        out: list[dict] = []
        if not REMODEL_JOB_DIR.is_dir():
            return out
        for path in REMODEL_JOB_DIR.glob("*.json"):
            data = _read_json_file(path)
            if data and str(data.get("item_id") or ""):
                out.append(data)
        return out

    def _item_cover_gid(self, item: dict, gens: list[dict]) -> str | None:
        """The explicitly chosen cover only; None means "use newest"."""
        cover = str(item.get("cover_gen") or "").strip()
        available = {str(gen.get("gid") or "") for gen in gens}
        return cover if cover and cover in available else None

    @staticmethod
    def _analysis_state(item: dict) -> str:
        """Public analysis state: ready | error | none.

        Img2img never needs this; text mode analyses on demand inside the job.
        """
        structure = item.get("structure")
        if isinstance(structure, dict) and str(
            structure.get("structure_en") or ""
        ).strip():
            return "ready"
        if str(item.get("analysis_error") or "").strip():
            return "error"
        return "none"

    def _item_public(
        self,
        item: dict,
        gens: list[dict] | None = None,
        running_job: str | None = None,
        last_error: str = "",
    ) -> dict:
        item_id = str(item.get("item_id") or "")
        gens = self._item_gens(item) if gens is None else gens
        source_path = self._item_source_path(item)
        source_url = (
            self._item_source_url(item_id, source_path.name)
            if source_path is not None
            else None
        )
        source_sid = str(item.get("source_current") or "")
        source_thumb_url = (
            self._item_source_thumb_url(item_id, source_sid)
            if source_path is not None and ITEM_ID_RE.fullmatch(source_sid)
            else None
        )
        source_width, source_height = (
            _image_dimensions(source_path) if source_path is not None else (None, None)
        )
        source_thumb_width, source_thumb_height = remodel_thumb_dimensions(
            source_width, source_height
        )
        source_ids = [str(value) for value in (item.get("source_ids") or [])]
        if source_sid and source_sid not in source_ids:
            source_ids.insert(0, source_sid)
        sources: list[dict] = []
        for sid in source_ids:
            if not ITEM_ID_RE.fullmatch(sid):
                continue
            path = source_path if sid == source_sid else self._item_source_path(item, sid)
            if path is None or not path.is_file():
                continue
            width, height = (
                (source_width, source_height)
                if sid == source_sid
                else _image_dimensions(path)
            )
            thumb_width, thumb_height = remodel_thumb_dimensions(width, height)
            sources.append(
                {
                    "sid": sid,
                    "current": sid == source_sid,
                    "url": self._item_source_url(item_id, path.name),
                    "thumb_url": self._item_source_thumb_url(item_id, sid),
                    "width": width,
                    "height": height,
                    "thumb_width": thumb_width,
                    "thumb_height": thumb_height,
                }
            )
        source_obj = next(
            (entry for entry in sources if entry.get("current")),
            sources[0] if sources else None,
        )
        cover_gid = self._item_cover_gid(item, gens)
        effective_cover = cover_gid or (
            str(gens[0].get("gid") or "") if gens else None
        )
        cover_url = (
            self._item_gen_url(item_id, effective_cover)
            if effective_cover
            else source_url
        )
        cover_gen_entry = next(
            (gen for gen in gens if str(gen.get("gid") or "") == effective_cover),
            None,
        )
        if effective_cover:
            cover_thumb_url = self._item_gen_thumb_url(item_id, effective_cover)
            cover_thumb_width = (
                cover_gen_entry.get("thumb_width") if cover_gen_entry else None
            )
            cover_thumb_height = (
                cover_gen_entry.get("thumb_height") if cover_gen_entry else None
            )
        else:
            cover_thumb_url = source_thumb_url
            cover_thumb_width = source_thumb_width
            cover_thumb_height = source_thumb_height
        if not cover_thumb_width or not cover_thumb_height:
            cover_thumb_width, cover_thumb_height = remodel_thumb_dimensions(
                None, None
            )
        styles: list[str] = []
        for gen in gens:
            style = str(gen.get("style") or "").strip()
            if style and style not in styles:
                styles.append(style)
        default_style = str(item.get("default_style") or "").strip()
        if not styles and default_style:
            styles = [default_style]
        public = {
            "item_id": item_id,
            "name": str(item.get("name") or ""),
            "renamed_by_user": bool(item.get("renamed_by_user")),
            "created_at": str(item.get("created_at") or ""),
            "updated_at": str(item.get("updated_at") or ""),
            "notes": str(item.get("notes") or ""),
            "default_style": default_style,
            "default_mode": str(item.get("default_mode") or REMODEL_DEFAULT_MODE),
            "source_current": source_sid,
            "source_ids": list(item.get("source_ids") or []),
            "source_url": source_url,
            "source_thumb_url": source_thumb_url,
            "source": source_obj,
            "sources": sources,
            "cover_gen": cover_gid,
            "cover_url": cover_url,
            "url": cover_url,
            "thumb_url": cover_thumb_url,
            "thumb_width": cover_thumb_width,
            "thumb_height": cover_thumb_height,
            "gen_count": len(gens),
            "styles": styles,
            "structure": item.get("structure") if isinstance(item.get("structure"), dict) else None,
            "analysis": item.get("structure") if isinstance(item.get("structure"), dict) else None,
            "analysis_state": self._analysis_state(item),
            "analysis_error": str(item.get("analysis_error") or "") or None,
            "gens": gens,
            "legacy_upload_id": item.get("legacy_upload_id"),
            "running": bool(running_job),
            "running_job": running_job,
            "last_error": last_error,
        }
        return public

    def _running_job_for_item(self, item_id: str) -> str | None:
        with self.lock:
            best = None
            best_ts = -1.0
            for job_id, job in self.remodel_jobs.items():
                if str(job.get("item_id") or "") != item_id:
                    continue
                if job.get("status") not in {"queued", "running"}:
                    continue
                created = float(job.get("created_ts") or 0)
                if created >= best_ts:
                    best_ts = created
                    best = job_id
            return best

    def list_remodel_items(self, limit: int = 50, offset: int = 0) -> dict:
        limit = max(1, min(100, int(limit)))
        offset = max(0, int(offset))
        items: list[dict] = []
        if REMODEL_ITEM_DIR.is_dir():
            for item_path in REMODEL_ITEM_DIR.glob("*/item.json"):
                data = _read_json_file(item_path)
                if data and self._item_id_ok(str(data.get("item_id") or "")):
                    data.setdefault("gens", [])
                    data.setdefault("source_ids", [])
                    items.append(data)
        items.sort(key=lambda entry: str(entry.get("updated_at") or ""), reverse=True)
        total = len(items)
        page = items[offset : offset + limit]
        persisted = self._persisted_item_jobs()
        errors: dict[str, tuple[float, str]] = {}
        for job in persisted:
            item_id = str(job.get("item_id") or "")
            if job.get("status") != "error":
                continue
            message = str(job.get("error") or "").strip()
            if not message:
                continue
            try:
                ts = float(job.get("created_ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            previous = errors.get(item_id)
            if previous is None or ts >= previous[0]:
                errors[item_id] = (ts, message)
        out = []
        for item in page:
            item_id = str(item.get("item_id") or "")
            gens = self._item_gens(item)
            self._ensure_item_thumbnails(item, gens=gens, all_gens=False)
            running = self._running_job_for_item(item_id)
            last_error = str(item.get("analysis_error") or "").strip()
            if not last_error and item_id in errors:
                last_error = errors[item_id][1]
            public = self._item_public(
                item, gens=gens, running_job=running, last_error=last_error
            )
            out.append(
                {
                    "item_id": public["item_id"],
                    "name": public["name"],
                    "renamed_by_user": public["renamed_by_user"],
                    "created_at": public["created_at"],
                    "updated_at": public["updated_at"],
                    "cover_url": public["cover_url"],
                    "url": public["url"],
                    "thumb_url": public["thumb_url"],
                    "thumb_width": public["thumb_width"],
                    "thumb_height": public["thumb_height"],
                    "source_thumb_url": public["source_thumb_url"],
                    "cover_gen": public["cover_gen"],
                    "gen_count": public["gen_count"],
                    "styles": public["styles"],
                    "default_mode": public["default_mode"],
                    "default_style": public["default_style"],
                    "source_url": public["source_url"],
                    "running": public["running"],
                    "running_job": public["running_job"],
                    "last_error": public["last_error"],
                }
            )
        return {"items": out, "total": total}

    def get_remodel_item(self, item_id: str) -> dict | None:
        item = self._read_item(item_id)
        if item is None:
            return None
        item_id = str(item.get("item_id") or "")
        gens = self._item_gens(item)
        self._ensure_item_thumbnails(item, gens=gens, all_gens=True)
        running = self._running_job_for_item(item_id)
        last_error = str(item.get("analysis_error") or "").strip()
        if not last_error:
            best_ts = -1.0
            for job in self._persisted_item_jobs():
                if str(job.get("item_id") or "") != item_id:
                    continue
                if job.get("status") != "error":
                    continue
                try:
                    ts = float(job.get("created_ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts >= best_ts and str(job.get("error") or "").strip():
                    best_ts = ts
                    last_error = str(job.get("error") or "").strip()
        public = self._item_public(
            item, gens=gens, running_job=running, last_error=last_error
        )
        public["failed_jobs"] = self._failed_jobs_for_item(
            item_id, public.get("source_thumb_url")
        )
        return public

    def create_remodel_item(
        self,
        name: str | None = None,
        upload_id: str | None = None,
        photo: bytes | None = None,
    ) -> dict | None:
        if name is not None:
            if not isinstance(name, str):
                raise ValueError("name 必须是字符串")
            name = name.strip()
            if len(name) > 120:
                raise ValueError("项目名不能超过 120 个字符")
        provided_name = (name or "").strip()
        item_id = uuid.uuid4().hex
        item_dir = self._item_dir(item_id, create=True)
        if item_dir is None:
            raise RuntimeError("无法创建项目目录")
        # Creation never analyses the source. Img2img is ready immediately;
        # text mode runs its own analysis stage when a job is submitted. A
        # legacy upload sidecar may still supply the room type for the default
        # project name, but it is not attached as analysis.
        name_analysis = None
        if upload_id:
            info = self.upload_info(str(upload_id))
            if info is None:
                return None
            source_bytes = info["path"].read_bytes()
            inspected = self._inspect_image_bytes(source_bytes)
            ext = inspected["ext"]
            sidecar = self._read_analysis_sidecar(str(upload_id))
            if sidecar and str(sidecar.get("structure_en") or "").strip():
                name_analysis = dict(sidecar)
        elif photo is not None:
            inspected = self._inspect_image_bytes(photo)
            ext = inspected["ext"]
            source_bytes = photo
        else:
            raise ValueError("缺少照片或 upload_id")
        sid = uuid.uuid4().hex
        source_dir = item_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        source_target = source_dir / f"{sid}{ext}"
        atomic_write_bytes(source_target, source_bytes)
        self._ensure_remodel_thumb(
            item_id, f"src-{sid}.jpg", source_target
        )
        stamp = iso_now_ms()
        room_zh = room_type_zh(name_analysis)
        item_name = provided_name or default_item_name(item_id, room_zh, "")
        item = {
            "item_id": item_id,
            "name": item_name,
            "renamed_by_user": bool(provided_name),
            "created_at": stamp,
            "updated_at": stamp,
            "notes": "",
            "default_style": self._default_style_value(),
            "default_mode": REMODEL_DEFAULT_MODE,
            "source_current": sid,
            "source_ids": [sid],
            "cover_gen": None,
            "structure": None,
            "analysis_error": None,
            "gens": [],
            "legacy_upload_id": str(upload_id) if upload_id else None,
        }
        self._write_item(item)
        return self.get_remodel_item(item_id)

    def analyze_item(self, item_id: str, force: bool = False) -> dict | None:
        item = self._read_item(item_id)
        if item is None:
            return None
        sid = str(item.get("source_current") or "")
        structure = item.get("structure")
        if (
            not force
            and isinstance(structure, dict)
            and str(structure.get("structure_en") or "").strip()
        ):
            return self.get_remodel_item(item_id)
        source_path = self._item_source_path(item, sid)
        if source_path is None:
            raise RuntimeError("当前原图不存在")
        if SMOKE_MODE:
            analysis = self._normalize_analysis(self._stub_analysis())
            analysis["model"] = "smoke-stub"
        else:
            key, base_url = self._read_credentials()
            analysis = self._vision_analysis(source_path, key, base_url)
            analysis["model"] = REMODEL_VISION_MODEL
        analysis["analyzed_at"] = iso_now()
        with self.item_lock:
            live = self._read_item(item_id)
            if live is None:
                return None
            if str(live.get("source_current") or "") != sid:
                # The source changed while the model was running; discard stale
                # analysis instead of attaching it to the new photo.
                return self.get_remodel_item(item_id)
            live["structure"] = analysis
            live["analysis_error"] = None
            if not live.get("renamed_by_user") and DEFAULT_ITEM_NAME_RE.fullmatch(
                str(live.get("name") or "")
            ):
                room_zh = room_type_zh(analysis)
                if room_zh:
                    style_zh = latest_remodel_gen_style(
                        live, self._item_dir(item_id), fallback=False
                    )
                    live["name"] = default_item_name(item_id, room_zh, style_zh)
            live["updated_at"] = iso_now_ms()
            self._write_item(live)
        return self.get_remodel_item(item_id)

    def replace_item_source(self, item_id: str, photo: bytes) -> dict | None:
        item = self._read_item(item_id)
        if item is None:
            return None
        inspected = self._inspect_image_bytes(photo)
        sid = uuid.uuid4().hex
        with self.item_lock:
            live = self._read_item(item_id)
            if live is None:
                return None
            item_dir = self._item_dir(item_id)
            if item_dir is None:
                return None
            source_dir = item_dir / "source"
            source_dir.mkdir(parents=True, exist_ok=True)
            source_target = source_dir / f"{sid}{inspected['ext']}"
            atomic_write_bytes(source_target, photo)
            self._ensure_remodel_thumb(
                item_id, f"src-{sid}.jpg", source_target
            )
            source_ids = [sid] + [
                str(value)
                for value in (live.get("source_ids") or [])
                if str(value) != sid
            ]
            live["source_current"] = sid
            live["source_ids"] = source_ids
            live["structure"] = None
            live["analysis_error"] = None
            live.pop("room_function", None)
            live.pop("room_function_source", None)
            live["updated_at"] = iso_now_ms()
            self._write_item(live)
        return self.get_remodel_item(item_id)

    def update_remodel_item(self, item_id: str, patch: dict) -> dict | None:
        allowed = {"name", "notes", "default_style", "default_mode", "cover_gen"}
        unknown = [key for key in patch if key not in allowed]
        if unknown:
            raise ValueError("不允许修改字段：" + ", ".join(unknown))
        if not patch:
            raise ValueError("没有可更新的字段")
        with self.item_lock:
            item = self._read_item(item_id)
            if item is None:
                return None
            updated = dict(item)
            if "name" in patch:
                value = patch["name"]
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("name 必须是非空字符串")
                if len(value.strip()) > 120:
                    raise ValueError("项目名不能超过 120 个字符")
                new_name = value.strip()
                if new_name != str(updated.get("name") or ""):
                    updated["renamed_by_user"] = True
                updated["name"] = new_name
            if "notes" in patch:
                value = patch["notes"]
                if not isinstance(value, str):
                    raise ValueError("notes 必须是字符串")
                if len(value) > 10000:
                    raise ValueError("备注过长")
                updated["notes"] = value
            if "default_style" in patch:
                value = patch["default_style"]
                if not isinstance(value, str):
                    raise ValueError("default_style 必须是字符串")
                value = value.strip()
                if value and self._find_style_pack(value)[1] is None:
                    raise ValueError("该风格没有可用的风格包")
                updated["default_style"] = value
            if "default_mode" in patch:
                value = patch["default_mode"]
                if not isinstance(value, str) or value not in REMODEL_MODES:
                    raise ValueError("default_mode 必须是 img2img 或 text")
                updated["default_mode"] = value
            if "cover_gen" in patch:
                value = patch["cover_gen"]
                if value is not None:
                    if not isinstance(value, str) or value not in (
                        updated.get("gens") or []
                    ):
                        raise ValueError("cover_gen 必须是已有版本 ID 或 null")
                updated["cover_gen"] = value
            updated["updated_at"] = iso_now_ms()
            self._write_item(updated)
        return self.get_remodel_item(item_id)

    def delete_remodel_item(self, item_id: str, purge: bool = False) -> dict | None:
        if self._read_item(item_id) is None:
            return None
        with self.lock:
            for job in self.remodel_jobs.values():
                if str(job.get("item_id") or "") != item_id:
                    continue
                if job.get("status") in {"queued", "running"}:
                    job["status"] = "error"
                    job["stage"] = "error"
                    job["error"] = "项目已删除"
                    job["finished_mono"] = time.monotonic()
        with self.item_lock:
            # Re-check under the item lock so an analysis/generation finalizer
            # cannot recreate the directory after the move.
            if self._read_item(item_id) is None:
                return None
            item_dir = self._item_dir(item_id)
            if item_dir is None:
                return None
            if purge:
                shutil.rmtree(item_dir)
                return {"item_id": item_id, "purged": True}
            REMODEL_TRASH_DIR.mkdir(parents=True, exist_ok=True)
            target = REMODEL_TRASH_DIR / item_id
            if target.exists():
                raise FileExistsError("回收站中已有同名项目")
            shutil.move(str(item_dir), str(target))
            return {"item_id": item_id, "purged": False}

    def delete_item_gen(self, item_id: str, gid: str) -> dict | None:
        with self.item_lock:
            item = self._read_item(item_id)
            if item is None:
                return None
            if not GEN_ID_RE.fullmatch(str(gid or "")) or gid not in (
                item.get("gens") or []
            ):
                return None
            item_dir = self._item_dir(item_id)
            if item_dir is None:
                return None
            for suffix in (".png", ".json"):
                (item_dir / "gen" / f"{gid}{suffix}").unlink(missing_ok=True)
            (item_dir / "thumb" / f"{gid}.jpg").unlink(missing_ok=True)
            updated = dict(item)
            updated["gens"] = [
                value for value in item.get("gens") or [] if value != gid
            ]
            if str(updated.get("cover_gen") or "") == gid:
                updated["cover_gen"] = None
            updated["updated_at"] = iso_now_ms()
            self._write_item(updated)
        return self.get_remodel_item(item_id)

    def patch_item_gen(self, item_id: str, gid: str, note: str) -> dict | None:
        with self.item_lock:
            item = self._read_item(item_id)
            if item is None:
                return None
            if not GEN_ID_RE.fullmatch(str(gid or "")) or gid not in (
                item.get("gens") or []
            ):
                return None
            if not isinstance(note, str):
                raise ValueError("note 必须是字符串")
            if len(note) > 2000:
                raise ValueError("备注过长")
            item_dir = self._item_dir(item_id)
            if item_dir is None:
                return None
            meta = _read_json_file(item_dir / "gen" / f"{gid}.json")
            if meta is None:
                return None
            meta["note"] = note
            atomic_write_json(item_dir / "gen" / f"{gid}.json", meta)
            item["updated_at"] = iso_now_ms()
            self._write_item(item)
        return self._expanded_gen(item_id, gid)

    def list_item_gens(self, item_id: str) -> list[dict] | None:
        item = self._read_item(item_id)
        if item is None:
            return None
        gens = self._item_gens(item)
        self._ensure_item_thumbnails(item, gens=gens, all_gens=True)
        return gens

    def submit_item_remodel(
        self, item_id: str, body: dict, base: str = "source"
    ) -> dict | None:
        if base not in {"source", "current"}:
            raise ValueError("base 必须是 source 或 current")
        allowed = {
            "mode",
            "style",
            "prompt_override",
            "note",
            "detail_enabled",
            "detail_zh",
        }
        if SMOKE_MODE:
            allowed.add("smoke_check")
        unknown = [key for key in body if key not in allowed]
        if unknown:
            raise ValueError("不允许的字段：" + ", ".join(unknown))
        item = self._read_item(item_id)
        if item is None:
            return None
        refine_of = ""
        if base == "current":
            latest = self._latest_item_gen(item)
            if latest is None:
                raise ValueError("还没有生成结果，无法做细节优化")
            source_path = latest["path"]
            refine_of = str(latest["gid"])
        else:
            source_path = self._item_source_path(item)
            if source_path is None:
                raise ValueError("当前原图不存在")
        structure = item.get("structure")
        structure_en = (
            str(structure.get("structure_en") or "").strip()
            if isinstance(structure, dict)
            else ""
        )
        mode_value = body.get("mode", item.get("default_mode") or REMODEL_DEFAULT_MODE)
        if not isinstance(mode_value, str) or mode_value not in REMODEL_MODES:
            raise ValueError("mode 必须是 img2img 或 text")
        style_value = body.get("style", item.get("default_style") or "")
        style_zh, pack = self._find_style_pack(str(style_value or ""))
        if pack is None:
            raise ValueError("该风格没有可用的风格包")
        prompt_override = body.get("prompt_override", "")
        if prompt_override is None:
            prompt_override = ""
        if not isinstance(prompt_override, str):
            raise ValueError("prompt_override 必须是字符串")
        prompt_override = prompt_override.strip()
        if len(prompt_override) > 500:
            raise ValueError("补充要求不能超过 500 个字符")
        note = body.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise ValueError("note 必须是字符串")
        note = note.strip()
        if len(note) > 500:
            raise ValueError("备注不能超过 500 个字符")
        width, height = _image_dimensions(source_path)
        if not width or not height:
            raise RuntimeError(
                "无法读取当前结果图尺寸"
                if base == "current"
                else "无法读取当前原图尺寸"
            )
        aspect = width / height
        label, mapping = describe_aspect(aspect)
        sid = str(item.get("source_current") or "")
        info = {
            "upload_id": sid,
            "path": source_path,
            "width": width,
            "height": height,
            "aspect": round(aspect, 4),
            "aspect_label": label,
            "preview_url": (
                self._item_gen_url(item_id, refine_of)
                if base == "current"
                else self._item_source_url(item_id, source_path.name)
            ),
            "output_size": mapping["size"],
            "aspect_exact": mapping["exact"],
            "mapping": mapping,
        }
        gid = self._new_gen_id(item_id)
        job_body = {
            "style": style_zh,
            "mode": mode_value,
        }
        if structure_en:
            job_body["structure_en"] = structure_en
        elif mode_value == "text":
            # Text mode cannot build its geometry-first prompt without an
            # analysis; the job analyses the source before its first request.
            job_body["analyze_source"] = True
        if prompt_override:
            job_body["extra_zh"] = prompt_override
        job_body["detail_enabled"] = body.get("detail_enabled", False)
        job_body["detail_zh"] = body.get("detail_zh", "")
        if SMOKE_MODE and "smoke_check" in body:
            job_body["smoke_check"] = body.get("smoke_check")
        result = self.submit_remodel(
            job_body,
            item=item,
            source_info=info,
            gen_id=gid,
            source_sid=sid,
            item_note=note,
            base=base,
            refine_of=refine_of,
        )
        if result is None:
            return None
        return {
            "job_id": result["job_id"],
            "item_id": item_id,
            "gen_id": gid,
            "base": result.get("base", base),
            "refine_of": result.get("refine_of", refine_of),
        }

    def submit_item_detail_optimize(
        self, item_id: str, body: dict
    ) -> dict | None:
        """Refine the newest result image into a new version (img2img).

        Reuses the normal item generation pipeline with ``base="current"``;
        only the validation and defaults are specific to this entry point.
        """
        allowed = {"detail_zh", "note"}
        unknown = [key for key in body if key not in allowed]
        if unknown:
            raise ValueError("不允许的字段：" + ", ".join(unknown))
        item = self._read_item(item_id)
        if item is None:
            return None
        detail_zh = body.get("detail_zh", "")
        if detail_zh is None:
            detail_zh = ""
        if not isinstance(detail_zh, str):
            raise ValueError("detail_zh 必须是字符串")
        detail_zh = detail_zh.strip()
        if len(detail_zh) > 300:
            raise ValueError("细节提示不能超过 300 个字符")
        note = body.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise ValueError("note 必须是字符串")
        note = note.strip()
        if len(note) > 500:
            raise ValueError("备注不能超过 500 个字符")
        latest = self._latest_item_gen(item)
        if latest is None:
            raise ValueError("还没有生成结果，无法做细节优化")
        style = str(latest["meta"].get("style") or "").strip()
        if not style or self._find_style_pack(style)[1] is None:
            style = str(item.get("default_style") or "").strip()
        if not style or self._find_style_pack(style)[1] is None:
            style = self._default_style_value()
        if not style:
            raise ValueError("该风格没有可用的风格包")
        return self.submit_item_remodel(
            item_id,
            {
                "mode": "img2img",
                "style": style,
                "detail_enabled": True,
                "detail_zh": detail_zh,
                "note": note or "细节优化",
            },
            base="current",
        )

    @staticmethod
    def _preview_url(filename: str) -> str:
        return "/remodel/uploads/" + quote(filename, safe="")

    def _upload_sidecar(self, upload_id: str) -> Path:
        return REMODEL_UPLOAD_DIR / f"{upload_id}.json"

    def _upload_path(self, upload_id: str) -> Path | None:
        if not UPLOAD_ID_RE.fullmatch(str(upload_id or "")):
            return None
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = REMODEL_UPLOAD_DIR / f"{upload_id}{ext}"
            if candidate.is_file():
                return candidate
        return None

    def save_upload(self, data: bytes) -> dict:
        """Validate bytes with Pillow and persist under an opaque uuid filename."""
        if not PIL_AVAILABLE:
            raise ValueError("缺少 Pillow，无法处理上传图片")
        if not data:
            raise ValueError("图片内容为空")
        if len(data) > MAX_REMODEL_UPLOAD_BYTES:
            raise ValueError("图片不能超过 12 MB")
        fmt = ""
        try:
            with Image.open(io.BytesIO(data)) as opened:
                fmt = str(opened.format or "").upper()
                opened.verify()
        except Exception:
            raise ValueError("无法识别的图片文件（仅支持 JPEG/PNG/WebP）") from None
        if fmt not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("只支持 JPEG、PNG、WebP 图片")
        try:
            with Image.open(io.BytesIO(data)) as opened:
                width, height = ImageOps.exif_transpose(opened).size
        except Exception:
            raise ValueError("无法读取图片尺寸") from None
        if width <= 0 or height <= 0:
            raise ValueError("图片尺寸无效")
        ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[fmt]
        upload_id = uuid.uuid4().hex
        REMODEL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = REMODEL_UPLOAD_DIR / f"{upload_id}{ext}"
        atomic_write_bytes(target, data)
        aspect = width / height
        label, mapping = describe_aspect(aspect)
        return {
            "upload_id": upload_id,
            "width": width,
            "height": height,
            "aspect": round(aspect, 4),
            "aspect_label": label,
            "preview_url": self._preview_url(target.name),
            "output_size": mapping["size"],
            "aspect_exact": mapping["exact"],
        }

    def upload_info(self, upload_id: str) -> dict | None:
        """Return the upload's real pixel size and aspect; never reads pixel data for generation."""
        path = self._upload_path(upload_id)
        if path is None:
            return None
        if not PIL_AVAILABLE:
            raise RuntimeError("缺少 Pillow，无法读取上传图片尺寸")
        try:
            with Image.open(path) as opened:
                width, height = ImageOps.exif_transpose(opened).size
        except Exception:
            raise RuntimeError("无法读取上传图片尺寸") from None
        if width <= 0 or height <= 0:
            raise RuntimeError("上传图片尺寸无效")
        aspect = width / height
        label, mapping = describe_aspect(aspect)
        return {
            "upload_id": upload_id,
            "path": path,
            "width": width,
            "height": height,
            "aspect": round(aspect, 4),
            "aspect_label": label,
            "preview_url": self._preview_url(path.name),
            "output_size": mapping["size"],
            "aspect_exact": mapping["exact"],
            "mapping": mapping,
        }

    def _read_analysis_sidecar(self, upload_id: str) -> dict | None:
        path = self._upload_sidecar(upload_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _cached_structure(self, upload_id: str) -> str:
        data = self._read_analysis_sidecar(upload_id) or {}
        return str(data.get("structure_en") or "").strip()

    @staticmethod
    def _stub_analysis() -> dict:
        return {
            "schema_version": REMODEL_SCHEMA_VERSION,
            "room_type_en": "living room",
            "shape_en": "rectangular, wider than deep",
            "ceiling_en": "flat ceiling at standard 2.8 m height, plain white, no beams",
            "walls_en": "flat painted walls, one continuous plane each side, skirting at floor",
            "floor_en": "light oak planks laid lengthwise, continuous floor plane, no steps or level changes",
            "geometry": {
                "room_type_en": "living room",
                "shape_en": "rectangular, wider than deep",
                "width_m": 4.2,
                "depth_m": 3.1,
                "ceiling_m": 2.8,
                "floor_level_changes": False,
            },
            "openings": [
                {
                    "side": "left",
                    "type": "open doorway",
                    "width_m": 0.95,
                    "height_m": 2.05,
                    "sill_m": 0.0,
                    "center_offset_fraction": 0.42,
                    "detail": "no door leaf, dark threshold, leads to corridor",
                },
                {
                    "side": "front-right",
                    "type": "floor-to-ceiling sliding glass door",
                    "width_m": 2.4,
                    "height_m": 2.4,
                    "sill_m": 0.0,
                    "center_offset_fraction": 0.62,
                    "detail": "black slim frame, four panes, transom window above",
                },
            ],
            "camera": {
                "height_m": 1.5,
                "distance_to_far_wall_m": 3.6,
                "lens_mm": 24,
                "tilt_deg": -3,
                "horizon_fraction": 0.55,
                "perspective": "one-point, straight at the far wall",
            },
            "framing": {
                "front_wall_and_opening_span": {"from_x": 0.10, "to_x": 0.90},
                "right_wall_span": {"from_x": 0.78, "to_x": 1.00},
                "left_wall_span": {"from_x": 0.00, "to_x": 0.12},
                "ceiling_bottom_y": 0.22,
                "floor_top_y": 0.68,
                "opening_width_over_wall_width": 0.85,
            },
            "openings_total": 2,
            "fixed_features": [
                {
                    "key": "ceiling cornice",
                    "detail": "plain 80 mm plaster cornice along all walls",
                },
                {
                    "key": "skirting board",
                    "detail": "painted timber skirting, 90 mm high",
                },
            ],
            "camera_en": "camera at 1.5 m height, one-point perspective straight at the far wall, slight downward tilt, 24 mm equivalent wide lens",
            "light_en": "main daylight from the front-right glass door, soft fill from the ceiling fixture",
            "structure_en": "Living room with a rectangular plan wider than deep, a flat 2.8 m white ceiling without beams, continuous painted wall planes with skirting, and a level floor. The left wall holds an open standard-height doorway to a corridor; the front-right wall has a floor-to-ceiling four-pane sliding glass door with a transom above. Camera at 1.5 m height, one-point perspective facing the far wall, slight downward tilt, 24 mm equivalent wide lens.",
            "immovable_zh": ["左墙标准门洞，通向走廊", "右前方落地推拉门带亮窗", "平顶 2.8 米，无吊顶"],
            "movable_zh": ["布艺沙发", "地毯", "电视柜"],
        }

    @staticmethod
    def _clean_chip_list(value, limit: int) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            text = " ".join(str(item or "").split())
            if text and text not in out:
                out.append(text)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _clean_fixed_features(value, limit: int = 6) -> list[dict]:
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            return []
        out: list[dict] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            key = " ".join(str(item.get("key") or "").split()).strip(" .")
            detail = " ".join(str(item.get("detail") or "").split()).strip(" .")
            if CJK_RE.search(key + " " + detail):
                continue
            if not key and not detail:
                continue
            if not key:
                key = detail
                detail = ""
            out.append({"key": key[:120], "detail": detail[:240]})
            if len(out) >= limit:
                break
        return out

    @classmethod
    def _normalize_framing(cls, value) -> dict:
        """Validate measured wall spans; return {valid, note, block}.

        Invalid measurements are dropped rather than clamped: a plausible but
        invented framing block is worse than no framing block at all.
        """
        raw = value if isinstance(value, dict) else {}
        notes: list[str] = []
        block: dict = {}

        legacy = [
            key
            for key in (
                "far_wall_width_fraction",
                "ceiling_fraction",
                "floor_fraction",
                "left_wall_fraction",
                "right_wall_fraction",
            )
            if key in raw
        ]
        if legacy:
            notes.append("旧版帧占比格式已停用，需要重新分析")
        if not raw:
            notes.append("未提供帧占比测量")

        def read_value(container: dict, key: str):
            number = coerce_float(container.get(key), None)
            if number is None or not 0.0 <= number <= 1.0:
                return None
            return round(number, 3)

        def read_fraction(key: str):
            return read_value(raw, key)

        spans: dict[str, dict] = {}
        for key in FRAMING_SPAN_KEYS:
            if key not in raw:
                continue
            entry = raw.get(key)
            if not isinstance(entry, dict):
                notes.append(f"{key} 不是对象")
                continue
            start = read_value(entry, "from_x")
            end = read_value(entry, "to_x")
            if start is None or end is None:
                notes.append(f"{key} 的 from_x/to_x 缺失或超出 0-1")
                continue
            if end <= start:
                notes.append(f"{key} 的 to_x 必须大于 from_x")
                continue
            spans[key] = {"from_x": start, "to_x": end}

        def scalar(key: str, low: float, high: float, label: str):
            number = read_fraction(key)
            if number is None:
                notes.append(f"{label} 缺失或超出 {low}-{high}")
                return None
            if not low <= number <= high:
                notes.append(f"{label} {number} 超出 {low}-{high}")
                return None
            return number

        opening = scalar(
            "opening_width_over_wall_width",
            0.05,
            1.0,
            "opening_width_over_wall_width",
        )
        ceiling = scalar("ceiling_bottom_y", 0.05, 0.45, "ceiling_bottom_y")
        floor = scalar("floor_top_y", 0.40, 0.95, "floor_top_y")
        if ceiling is not None and floor is not None and floor - ceiling < 0.25:
            notes.append("floor_top_y 与 ceiling_bottom_y 的高度差小于 0.25")

        if notes:
            return {"valid": False, "note": "；".join(notes), "block": {}}

        block.update(spans)
        block["ceiling_bottom_y"] = ceiling
        block["floor_top_y"] = floor
        block["opening_width_over_wall_width"] = opening
        return {"valid": True, "note": "", "block": block}

    @staticmethod
    def _opening_span_sentence(value) -> str:
        number = coerce_float(value, None, 0.0, 1.0)
        if number is None:
            return ""
        if number >= 0.8:
            return (
                "the opening is nearly wall-to-wall, leaving only narrow piers "
                "at each end"
            )
        if number >= 0.5:
            return (
                "the opening covers about half the wall with solid wall on both sides"
            )
        return "the opening is a modest window set in a mostly solid wall"

    @classmethod
    def _framing_lock(cls, framing: dict) -> str:
        opening = coerce_float(framing.get("opening_width_over_wall_width"), None)
        ceiling = coerce_float(framing.get("ceiling_bottom_y"), None)
        floor = coerce_float(framing.get("floor_top_y"), None)
        opening_pct = int(round((opening or 0) * 100))

        def axis(value) -> str:
            number = coerce_float(value, None)
            return f"{number:.2f}" if number is not None else "0.00"

        pieces: list[str] = []
        front = framing.get("front_wall_and_opening_span") or {}
        if front:
            pieces.append(
                "the wall carrying the main opening runs from "
                f"x={axis(front.get('from_x'))} to x={axis(front.get('to_x'))} "
                f"and the opening fills about {opening_pct}% of that wall"
            )
        else:
            pieces.append(f"the opening fills about {opening_pct}% of its wall")
        right = framing.get("right_wall_span") or {}
        if right:
            pieces.append(
                "the right wall plane runs from "
                f"x={axis(right.get('from_x'))} to x={axis(right.get('to_x'))}"
            )
        left = framing.get("left_wall_span") or {}
        if left:
            pieces.append(
                "the left wall plane from "
                f"x={axis(left.get('from_x'))} to x={axis(left.get('to_x'))}"
            )
        pieces.append(
            f"the ceiling line sits at y={axis(ceiling)} and the "
            f"far-wall floor line at y={axis(floor)}"
        )
        text = (
            "Wall proportions measured on the source photo (fractions of image "
            "width/height): " + "; ".join(pieces) + ". Match these proportions: "
            "the opening keeps its width relative to its wall, the room is not "
            "widened, and the ceiling is not raised or lowered."
        )
        sentence = cls._opening_span_sentence(opening)
        if sentence:
            text += " " + sentence + "."
        return text

    @staticmethod
    def _structure_fixed_sentence(fixed_features: list[dict] | None) -> str:
        if not fixed_features:
            return ""
        parts = []
        for entry in fixed_features:
            key = " ".join(str(entry.get("key") or "").split()).strip(" .")
            detail = " ".join(str(entry.get("detail") or "").split()).strip(" .")
            if not key and not detail:
                continue
            parts.append(f"{key}: {detail}" if detail else key)
        if not parts:
            return ""
        return "Fixed features that stay: " + "; ".join(parts) + "."

    @classmethod
    def _build_structure_en(
        cls,
        geometry: dict,
        openings: list[dict],
        camera: dict,
        fixed_features: list[dict] | None = None,
        opening_width_over_wall_width=None,
        floor_en: str = "",
    ) -> str:
        """Format the locked numeric fields into one deterministic English paragraph."""
        room = " ".join(str(geometry.get("room_type_en") or "interior room").split()).strip(" .")
        room = (room[:1].upper() + room[1:]) if room else "Interior room"
        shape = " ".join(str(geometry.get("shape_en") or "rectangular").split()).strip(" .")
        floor_text = " ".join(str(floor_en or "").split()).strip(" .")
        if floor_text:
            floor = (
                floor_text
                if floor_text.casefold().startswith("floor")
                else "floor: " + floor_text
            )
        else:
            floor = (
                "floor with a visible level change"
                if geometry.get("floor_level_changes")
                else "continuous floor with no level changes"
            )
        base = (
            f"{room}, {shape}, {fmt_number(geometry.get('width_m'))} m x "
            f"{fmt_number(geometry.get('depth_m'))} m; ceiling "
            f"{fmt_number(geometry.get('ceiling_m'))} m; {floor}."
        )

        def opening_sentence(entry: dict, include_detail: bool) -> str:
            side = " ".join(str(entry.get("side") or "wall").split()).strip(" .")
            typ = " ".join(str(entry.get("type") or "opening").split()).strip(" .")
            label = (side[:1].upper() + side[1:]) if side else "Wall"
            if side.casefold() not in {"ceiling", "floor"}:
                label += " wall"
            offset = int(
                round(coerce_float(entry.get("center_offset_fraction"), 0.0, 0.0, 1.0) * 100)
            )
            sentence = (
                f"{label}: {typ} {fmt_number(entry.get('width_m'))} m wide, "
                f"{fmt_number(entry.get('height_m'))} m high, "
                f"sill {fmt_number(entry.get('sill_m'))} m, "
                f"centred {offset}% along the wall from its left edge"
            )
            detail = " ".join(str(entry.get("detail") or "").split()).strip(" .")
            if include_detail and detail:
                sentence += ", " + detail
            return sentence + "."

        height = fmt_number(coerce_float(camera.get("height_m"), 1.5, 0.5, 3.0))
        distance = fmt_number(
            coerce_float(
                camera.get("distance_to_far_wall_m"),
                coerce_float(geometry.get("depth_m"), 3.0, 1.0, 30.0),
                0.5,
                40.0,
            )
        )
        lens = fmt_number(coerce_float(camera.get("lens_mm"), 24.0, 8.0, 200.0), 0)
        tilt = fmt_number(coerce_float(camera.get("tilt_deg"), 0.0, -45.0, 45.0))
        horizon = int(
            round(coerce_float(camera.get("horizon_fraction"), 0.55, 0.0, 1.0) * 100)
        )
        perspective = " ".join(
            str(
                camera.get("perspective") or "one-point perspective straight at the far wall"
            ).split()
        ).strip(" .")
        camera_sentence = (
            f"Camera {height} m high, {distance} m from the far wall, {lens} mm lens, "
            f"tilt {tilt} degrees, horizon {horizon}% of frame height, {perspective}."
        )
        count_sentence = (
            f"The number of openings is exactly {len(openings)}; "
            "do not add or remove any opening."
        )

        def compact_opening_sentence(entry: dict) -> str:
            side = " ".join(str(entry.get("side") or "wall").split()).strip(" .")
            typ = " ".join(str(entry.get("type") or "opening").split()).strip(" .")
            offset = int(
                round(coerce_float(entry.get("center_offset_fraction"), 0.0, 0.0, 1.0) * 100)
            )
            return (
                f"{side[:1].upper() + side[1:] if side else 'Wall'} {typ} "
                f"{fmt_number(entry.get('width_m'))} x {fmt_number(entry.get('height_m'))} m, "
                f"sill {fmt_number(entry.get('sill_m'))} m, {offset}% along the wall."
            )

        def render(include_detail: bool) -> str:
            parts = [base]
            if not openings:
                parts.append("There are no wall openings.")
            parts.extend(opening_sentence(entry, include_detail) for entry in openings)
            parts.append(camera_sentence)
            parts.append(count_sentence)
            return " ".join(parts)

        def render_compact() -> str:
            parts = [base]
            if not openings:
                parts.append("There are no wall openings.")
            parts.extend(compact_opening_sentence(entry) for entry in openings)
            parts.append(
                f"Camera {height} m high, {distance} m to the far wall, {lens} mm lens, "
                f"tilt {tilt} degrees, horizon {horizon}% of frame height."
            )
            parts.append(count_sentence)
            return " ".join(parts)

        fixed_sentence = cls._structure_fixed_sentence(fixed_features)
        opening_span = cls._opening_span_sentence(opening_width_over_wall_width)
        opening_span_sentence = (
            f"Opening proportion: {opening_span}." if opening_span else ""
        )
        extra_words = len(fixed_sentence.split()) + len(opening_span_sentence.split())
        text = render(True)
        if len(text.split()) + extra_words > 130:
            text = render(False)
        if len(text.split()) + extra_words > 130:
            text = render_compact()
        for filler in (
            "All wall planes, the ceiling plane and the floor plane are fixed and unchangeable.",
            "Do not invent openings, niches, beams, columns, steps or wall panels.",
            "The room proportions are fixed exactly as stated above.",
        ):
            if len(text.split()) + extra_words >= 80:
                break
            text += " " + filler
        if opening_span_sentence:
            text += " " + opening_span_sentence
        if fixed_sentence:
            text += " " + fixed_sentence
        return text

    @classmethod
    def _normalize_analysis(cls, obj: dict) -> dict:
        if not isinstance(obj, dict):
            raise RuntimeError("视觉分析顶层不是对象")

        def text(value) -> str:
            return " ".join(str(value or "").split())

        geometry_raw = obj.get("geometry") if isinstance(obj.get("geometry"), dict) else {}
        camera_raw = obj.get("camera") if isinstance(obj.get("camera"), dict) else {}

        room_type = text(geometry_raw.get("room_type_en")) or text(obj.get("room_type_en"))
        shape = text(geometry_raw.get("shape_en")) or text(obj.get("shape_en"))
        if not room_type:
            raise RuntimeError("视觉分析缺少 room_type_en")
        if not shape:
            raise RuntimeError("视觉分析缺少 shape_en")

        floor_level_changes = coerce_bool(geometry_raw.get("floor_level_changes"), False)
        geometry = {
            "room_type_en": room_type,
            "shape_en": shape,
            "width_m": round(coerce_float(geometry_raw.get("width_m"), 4.0, 1.0, 30.0), 2),
            "depth_m": round(coerce_float(geometry_raw.get("depth_m"), 3.0, 1.0, 30.0), 2),
            "ceiling_m": round(coerce_float(geometry_raw.get("ceiling_m"), 2.7, 1.8, 6.0), 2),
            "floor_level_changes": floor_level_changes,
        }

        openings: list[dict] = []
        raw_openings = obj.get("openings")
        if isinstance(raw_openings, list):
            for entry in raw_openings:
                if not isinstance(entry, dict):
                    continue
                side = text(entry.get("side"))
                typ = text(entry.get("type"))
                if not side or not typ:
                    continue
                lowered = typ.casefold()
                default_sill = (
                    0.9
                    if ("window" in lowered or "transom" in lowered)
                    and "door" not in lowered
                    else 0.0
                )
                width = coerce_float(entry.get("width_m"), 0.9, 0.1, 12.0)
                height = coerce_float(entry.get("height_m"), 2.05, 0.1, 6.0)
                sill = coerce_float(entry.get("sill_m"), default_sill, 0.0, height)
                if sill > height:
                    sill = 0.0
                openings.append(
                    {
                        "side": side,
                        "type": typ,
                        "width_m": round(width, 2),
                        "height_m": round(height, 2),
                        "sill_m": round(sill, 2),
                        "center_offset_fraction": round(
                            coerce_float(
                                entry.get("center_offset_fraction"), 0.5, 0.0, 1.0
                            ),
                            3,
                        ),
                        "detail": text(entry.get("detail")),
                    }
                )
                if len(openings) >= 12:
                    break

        camera = {
            "height_m": round(coerce_float(camera_raw.get("height_m"), 1.5, 0.5, 3.0), 2),
            "distance_to_far_wall_m": round(
                coerce_float(
                    camera_raw.get("distance_to_far_wall_m"),
                    geometry["depth_m"],
                    0.5,
                    40.0,
                ),
                2,
            ),
            "lens_mm": round(coerce_float(camera_raw.get("lens_mm"), 24.0, 8.0, 200.0), 1),
            "tilt_deg": round(coerce_float(camera_raw.get("tilt_deg"), 0.0, -45.0, 45.0), 1),
            "horizon_fraction": round(
                coerce_float(camera_raw.get("horizon_fraction"), 0.55, 0.05, 0.95), 3
            ),
            "perspective": text(camera_raw.get("perspective"))
            or "one-point, straight at the far wall",
        }

        floor_en = text(obj.get("floor_en")) or (
            "floor with a visible level change"
            if floor_level_changes
            else "continuous floor with no level changes"
        )
        framing_result = cls._normalize_framing(obj.get("framing"))
        framing = framing_result["block"]
        fixed_features = cls._clean_fixed_features(obj.get("fixed_features"))
        structure_en = cls._build_structure_en(
            geometry,
            openings,
            camera,
            fixed_features,
            framing.get("opening_width_over_wall_width"),
            floor_en=floor_en,
        )
        if CJK_RE.search(structure_en):
            raise RuntimeError("视觉分析返回了非英文结构描述")

        ceiling_en = text(obj.get("ceiling_en")) or (
            f"flat ceiling at {fmt_number(geometry['ceiling_m'])} m height"
        )
        walls_en = text(obj.get("walls_en")) or (
            "flat painted walls, one continuous plane each side"
        )
        camera_en = text(obj.get("camera_en")) or (
            f"camera at {fmt_number(camera['height_m'])} m height, "
            f"{fmt_number(camera['distance_to_far_wall_m'])} m from the far wall, "
            f"{fmt_number(camera['lens_mm'], 0)} mm lens, tilt "
            f"{fmt_number(camera['tilt_deg'])} degrees"
        )
        light_en = text(obj.get("light_en")) or "natural daylight from the visible openings"
        return {
            "schema_version": REMODEL_SCHEMA_VERSION,
            "room_type_en": room_type,
            "room_function": _normalize_room_function(obj.get("room_function")),
            "shape_en": shape,
            "ceiling_en": ceiling_en,
            "walls_en": walls_en,
            "floor_en": floor_en,
            "geometry": geometry,
            "openings": openings,
            "openings_total": len(openings),
            "camera": camera,
            "framing": framing,
            "framing_valid": framing_result["valid"],
            "framing_note": framing_result["note"],
            "fixed_features": fixed_features,
            "camera_en": camera_en,
            "light_en": light_en,
            "structure_en": structure_en,
            "structure_en_model": text(obj.get("structure_en")),
            "immovable_zh": cls._clean_chip_list(obj.get("immovable_zh"), 5),
            "movable_zh": cls._clean_chip_list(obj.get("movable_zh"), 6),
        }

    @classmethod
    def _upgrade_analysis(cls, previous: dict) -> dict:
        """Lift a v2/v3 cached sidecar to v4 without calling the vision model."""
        data = dict(previous)
        geometry = (
            dict(previous.get("geometry"))
            if isinstance(previous.get("geometry"), dict)
            else {}
        )
        openings = [
            dict(entry)
            for entry in (previous.get("openings") or [])
            if isinstance(entry, dict)
        ]
        camera = (
            dict(previous.get("camera"))
            if isinstance(previous.get("camera"), dict)
            else {}
        )
        camera.setdefault("perspective", "one-point, straight at the far wall")
        fixed_features = cls._clean_fixed_features(previous.get("fixed_features"))
        framing_result = cls._normalize_framing(previous.get("framing"))
        framing = framing_result["block"]
        floor_en = " ".join(str(previous.get("floor_en") or "").split()).strip(" .")
        if not floor_en:
            floor_en = (
                "floor with a visible level change"
                if geometry.get("floor_level_changes")
                else "continuous floor with no level changes"
            )
        data.update(
            {
                "schema_version": REMODEL_SCHEMA_VERSION,
                "geometry": geometry,
                "openings": openings,
                "openings_total": len(openings),
                "camera": camera,
                "framing": framing,
                "framing_valid": framing_result["valid"],
                "framing_note": framing_result["note"],
                "fixed_features": fixed_features,
                "floor_en": floor_en,
                "structure_en": cls._build_structure_en(
                    geometry,
                    openings,
                    camera,
                    fixed_features,
                    framing.get("opening_width_over_wall_width"),
                    floor_en=floor_en,
                ),
            }
        )
        return data

    def _image_data_url(self, path: Path) -> str:
        """Encode an upload for the vision model."""
        if not PIL_AVAILABLE:
            raise RuntimeError("缺少 Pillow，无法读取上传图片")
        try:
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=85, optimize=False)
        except Exception:
            raise RuntimeError("无法读取上传图片") from None
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return "data:image/jpeg;base64," + encoded

    @staticmethod
    def _img2img_data_url(path: Path) -> tuple[str, int, int]:
        """Downscale the user's photo for img2img and return (data URL, long edge, bytes).

        The long edge is capped at 1024 px (never upscaled) and the JPEG is
        written to memory at q=90. This is the only image part ever sent to the
        image model; style always comes from the prompt.
        """
        if not PIL_AVAILABLE:
            raise RuntimeError("缺少 Pillow，无法读取上传图片")
        try:
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                image.thumbnail(
                    (REMODEL_INPUT_LONG_EDGE, REMODEL_INPUT_LONG_EDGE),
                    Image.Resampling.LANCZOS,
                )
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=90, optimize=False)
                long_edge = max(image.size)
        except Exception:
            raise RuntimeError("无法读取上传图片") from None
        data = buffer.getvalue()
        if not data:
            raise RuntimeError("img2img 输入图片编码失败")
        encoded = base64.b64encode(data).decode("ascii")
        return "data:image/jpeg;base64," + encoded, long_edge, len(data)

    def _check_image_data_url(self, path: Path) -> str:
        """Self-check images are capped at 1024 px; larger inputs only burn tokens."""
        if not PIL_AVAILABLE:
            raise RuntimeError("缺少 Pillow，无法读取上传图片")
        try:
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=88, optimize=False)
        except Exception:
            raise RuntimeError("无法读取上传图片") from None
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return "data:image/jpeg;base64," + encoded

    @staticmethod
    def _chat_completion(base_url: str, key: str, payload: dict, timeout: float = 180) -> dict:
        request = Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        def fetch() -> dict:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)

        try:
            result = call_with_deadline(
                fetch,
                timeout,
                remodel_timeout_message(timeout, "text"),
            )
        except RemodelTimeoutError:
            raise
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "ignore")[:240]
            except Exception:
                pass
            detail = re.sub(r"Bearer\s+\S+", "Bearer ***", detail, flags=re.IGNORECASE)
            raise RuntimeError(
                f"文本模型返回 HTTP {exc.code}" + (f"：{detail}" if detail else "")
            ) from None
        except URLError as exc:
            raise RuntimeError(f"无法连接文本模型：{exc.reason}") from None
        except Exception as exc:
            raise RuntimeError(f"文本模型请求失败：{clean_error(exc)}") from None
        if not isinstance(result, dict):
            raise RuntimeError("文本模型响应格式异常")
        return result

    def _vision_analysis(self, path: Path, key: str, base_url: str) -> dict:
        data_url = self._image_data_url(path)
        payload = {
            "model": REMODEL_VISION_MODEL,
            "temperature": 0.2,
            "max_tokens": 2000,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": REMODEL_VISION_PROMPT},
                    ],
                }
            ],
        }
        response = self._chat_completion(
            base_url, key, payload, timeout=REMODEL_ANALYSIS_TIMEOUT
        )
        content, finish_reason = completion_content(response)
        if finish_reason == "length":
            raise RuntimeError("视觉分析响应被截断")
        try:
            obj = parse_model_json(content)
        except Exception as exc:
            raise RuntimeError(f"视觉分析 JSON 解析失败：{exc}") from None
        return self._normalize_analysis(obj)

    def _vision_room_function(self, path: Path) -> str:
        """One short vision call: classify the source room's function.

        Raises on transport/parse failure; ``_detect_room_function`` treats
        every failure as ``unknown`` so generation is never blocked.
        """
        if SMOKE_MODE:
            return "unknown"
        key, base_url = self._read_credentials()
        data_url = self._image_data_url(path)
        payload = {
            "model": REMODEL_VISION_MODEL,
            "temperature": 0.0,
            "max_tokens": 80,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": REMODEL_ROOM_FUNCTION_VISION_PROMPT},
                    ],
                }
            ],
        }
        response = self._chat_completion(base_url, key, payload, timeout=45)
        content, finish_reason = completion_content(response)
        if finish_reason == "length":
            return "unknown"
        obj = parse_model_json(content)
        if not isinstance(obj, dict):
            return "unknown"
        return _normalize_room_function(obj.get("room_function"))

    def _cache_room_function(
        self, item: dict, room_function: str, source: str
    ) -> None:
        """Persist the detection result on the project, best effort only."""
        item["room_function"] = room_function
        item["room_function_source"] = source
        item_id = str(item.get("item_id") or "")
        if not item_id:
            return
        with self.item_lock:
            live = self._read_item(item_id)
            if live is None:
                return
            if (
                live.get("room_function") == room_function
                and live.get("room_function_source") == source
            ):
                return
            live["room_function"] = room_function
            live["room_function_source"] = source
            live["updated_at"] = iso_now_ms()
            self._write_item(live)

    def _detect_room_function(
        self, item: dict, allow_vision: bool = True
    ) -> str:
        """Resolve the room function with structure > project name > vision.

        The result is cached on ``item.json``. Any vision failure degrades to
        ``unknown`` instead of failing the generation.
        """
        if not isinstance(item, dict):
            return "unknown"
        structure = (
            item.get("structure") if isinstance(item.get("structure"), dict) else {}
        )
        detected = _normalize_room_function(structure.get("room_function"))
        if detected:
            try:
                self._cache_room_function(item, detected, "analysis")
            except Exception:
                pass
            return detected
        detected = _room_function_from_name(item.get("name"))
        if detected:
            try:
                self._cache_room_function(item, detected, "name")
            except Exception:
                pass
            return detected
        cached = _normalize_room_function(item.get("room_function"))
        if cached and str(item.get("room_function_source") or "") == "vision":
            return cached
        if not allow_vision:
            return "unknown"
        try:
            source_path = self._item_source_path(item)
        except Exception:
            source_path = None
        if source_path is None:
            return "unknown"
        try:
            detected = _normalize_room_function(
                self._vision_room_function(source_path)
            )
        except Exception:
            # No key, timeout or invalid response: degrade to unknown and do
            # not cache the failure, so a later run can retry.
            return "unknown"
        if not detected:
            return "unknown"
        try:
            self._cache_room_function(item, detected, "vision")
        except Exception:
            pass
        return detected

    def analyze_upload(self, upload_id: str, force: bool = False) -> dict | None:
        path = self._upload_path(upload_id)
        if path is None:
            return None
        sidecar = self._upload_sidecar(upload_id)
        previous = self._read_analysis_sidecar(upload_id)
        if previous and not force and str(previous.get("structure_en") or "").strip():
            result = dict(previous)
            try:
                version = int(previous.get("schema_version") or 1)
            except (TypeError, ValueError):
                version = 1
            if 2 <= version < REMODEL_SCHEMA_VERSION:
                try:
                    result = self._upgrade_analysis(previous)
                    result["schema_version"] = REMODEL_SCHEMA_VERSION
                    atomic_write_json(sidecar, result)
                    version = REMODEL_SCHEMA_VERSION
                except Exception:
                    result = dict(previous)
            elif version >= REMODEL_SCHEMA_VERSION:
                framing_result = self._normalize_framing(previous.get("framing"))
                if "framing_valid" not in result:
                    result["framing_valid"] = framing_result["valid"]
                if "framing_note" not in result:
                    result["framing_note"] = framing_result["note"]
                if not result.get("framing_valid"):
                    result["framing"] = {}
            result["schema_version"] = version
            result["cached"] = True
            result["upload_id"] = upload_id
            result["preview_url"] = self._preview_url(path.name)
            return result
        previous_calls = 0
        if previous:
            try:
                previous_calls = int(previous.get("vision_calls") or 0)
            except (TypeError, ValueError):
                previous_calls = 0
        if SMOKE_MODE:
            analysis = self._normalize_analysis(self._stub_analysis())
            model = "smoke-stub"
        else:
            key, base_url = self._read_credentials()
            analysis = self._vision_analysis(path, key, base_url)
            model = REMODEL_VISION_MODEL
        payload = dict(analysis)
        payload.update(
            {
                "upload_id": upload_id,
                "preview_url": self._preview_url(path.name),
                "model": model,
                "vision_calls": previous_calls + 1,
                "analyzed_at": iso_now(),
            }
        )
        atomic_write_json(sidecar, payload)
        result = dict(payload)
        result["cached"] = False
        return result

    def _analysis_job_values(
        self, analysis: dict | None, structure: str
    ) -> dict:
        """Derive every analysis-derived job field from one analysis payload.

        Used both when a job is submitted with an existing analysis and after
        a text-mode job analyses its source at execution time.
        """
        analysis = analysis if isinstance(analysis, dict) else {}
        structure = " ".join(str(structure or "").split())
        raw_openings = analysis.get("openings")
        openings = (
            [dict(entry) for entry in raw_openings if isinstance(entry, dict)]
            if isinstance(raw_openings, list)
            else []
        )
        geometry = (
            dict(analysis.get("geometry"))
            if isinstance(analysis.get("geometry"), dict)
            else {}
        )
        camera = (
            dict(analysis.get("camera"))
            if isinstance(analysis.get("camera"), dict)
            else {}
        )
        openings_total = len(openings)
        if openings_total == 0 and structure:
            count_match = re.search(
                r"The number of openings is exactly\s+(\d+)",
                structure,
                flags=re.IGNORECASE,
            )
            if count_match:
                openings_total = int(count_match.group(1))
        if "horizon_fraction" not in camera and structure:
            horizon_match = re.search(
                r"horizon\s+(\d+(?:\.\d+)?)\s*%\s+of\s+(?:the\s+)?frame\s+height",
                structure,
                flags=re.IGNORECASE,
            )
            if horizon_match:
                camera["horizon_fraction"] = round(
                    coerce_float(horizon_match.group(1), 55.0, 0.0, 100.0) / 100.0,
                    3,
                )
        framing_result = self._normalize_framing(analysis.get("framing"))
        framing = framing_result["block"]
        framing_valid = framing_result["valid"]
        framing_note = framing_result["note"]
        if not framing_valid and str(analysis.get("framing_note") or "").strip():
            framing_note = str(analysis["framing_note"]).strip()
        opening_sentence = (
            self._opening_span_sentence(
                framing.get("opening_width_over_wall_width")
            )
            if framing_valid
            else ""
        )
        if structure and opening_sentence and opening_sentence not in structure:
            structure = (
                structure.rstrip(" .")
                + ". Opening proportion: "
                + opening_sentence
                + "."
            )
        fixed_features = self._clean_fixed_features(analysis.get("fixed_features"))
        floor_en = " ".join(str(analysis.get("floor_en") or "").split()).strip(" .")
        if not floor_en:
            floor_en = (
                "floor with a visible level change"
                if geometry.get("floor_level_changes")
                else "continuous floor with no level changes"
            )
        return {
            "structure_en": structure,
            "geometry": geometry,
            "camera": camera,
            "framing": framing,
            "framing_valid": framing_valid,
            "framing_note": framing_note,
            "fixed_features": fixed_features,
            "floor_en": floor_en,
            "openings": openings,
            "openings_total": openings_total,
            "immovable_zh": analysis.get("immovable_zh")
            if isinstance(analysis.get("immovable_zh"), list)
            else [],
            "movable_zh": analysis.get("movable_zh")
            if isinstance(analysis.get("movable_zh"), list)
            else [],
            "daylight_direction_en": self._daylight_direction_sentence(
                {"openings": openings}
            ),
        }

    def submit_remodel(
        self,
        body: dict,
        item: dict | None = None,
        source_info: dict | None = None,
        gen_id: str | None = None,
        source_sid: str | None = None,
        item_note: str = "",
        base: str = "source",
        refine_of: str = "",
    ) -> dict | None:
        if base not in {"source", "current"}:
            raise ValueError("base 必须是 source 或 current")
        # Hard guard: clients may never inject an image. img2img reads only the
        # server-side upload and never accepts a style reference.
        forbidden = sorted(
            key
            for key in body
            if str(key).lower() in FORBIDDEN_IMAGE_KEYS
            or any(
                token in str(key).lower()
                for token in ("image", "img", "photo", "file", "ref_")
            )
        )
        if forbidden:
            raise ValueError(
                "客户端不能传入图片；img2img 只读取服务端保存的上传原图（"
                + ", ".join(forbidden)
                + "）"
            )

        analysis_source: dict | None = None
        if item is not None:
            info = source_info
            if info is None:
                raise RuntimeError("缺少项目原图信息")
            upload_id = str(item.get("item_id") or "")
            analysis_source = (
                dict(item.get("structure"))
                if isinstance(item.get("structure"), dict)
                else {}
            )
        else:
            upload_id = str(body.get("upload_id") or "").strip()
            info = self.upload_info(upload_id)
            if info is None:
                return None

        style_zh, pack = self._find_style_pack(str(body.get("style") or ""))
        if pack is None:
            raise ValueError("该风格没有可用的风格包")

        analyze_source = bool(body.get("analyze_source") or False)
        structure_value = body.get("structure_en", None)
        if structure_value is None:
            if analysis_source is not None:
                structure = str(analysis_source.get("structure_en") or "").strip()
            else:
                structure = self._cached_structure(upload_id)
        else:
            if not isinstance(structure_value, str):
                raise ValueError("structure_en 必须是字符串")
            structure = " ".join(structure_value.split())
        if CJK_RE.search(structure):
            raise ValueError("结构描述必须是英文（不能包含中文）")

        extra_zh = body.get("extra_zh", "")
        if extra_zh is None:
            extra_zh = ""
        if not isinstance(extra_zh, str):
            raise ValueError("extra_zh 必须是字符串")
        extra_zh = extra_zh.strip()
        if len(extra_zh) > 500:
            raise ValueError("补充要求不能超过 500 个字符")

        detail_enabled_value = body.get("detail_enabled", False)
        if detail_enabled_value is None:
            detail_enabled_value = False
        if isinstance(detail_enabled_value, bool):
            detail_enabled = detail_enabled_value
        elif (
            isinstance(detail_enabled_value, int)
            and detail_enabled_value in (0, 1)
        ):
            detail_enabled = bool(detail_enabled_value)
        elif isinstance(detail_enabled_value, str) and detail_enabled_value in {
            "true",
            "false",
        }:
            detail_enabled = detail_enabled_value == "true"
        else:
            raise ValueError("detail_enabled 必须是 True/False/true/false/1/0")

        detail_zh = body.get("detail_zh", "")
        if detail_zh is None:
            detail_zh = ""
        if not isinstance(detail_zh, str):
            raise ValueError("detail_zh 必须是字符串")
        detail_zh = detail_zh.strip()
        if len(detail_zh) > 300:
            raise ValueError("细节提示不能超过 300 个字符")

        model = str(
            body.get("model") or (self.config["models"][0] if self.config["models"] else "")
        )
        if model not in self.config.get("models", []):
            raise ValueError("不支持的模型")

        variants = body.get("variants", 1)
        if (
            isinstance(variants, bool)
            or not isinstance(variants, int)
            or not 1 <= variants <= 4
        ):
            raise ValueError("variants 必须是 1 到 4 的整数")

        mode_value = body.get("mode", REMODEL_DEFAULT_MODE)
        if mode_value is None:
            mode_value = REMODEL_DEFAULT_MODE
        if not isinstance(mode_value, str) or mode_value not in REMODEL_MODES:
            raise ValueError("mode 必须是 img2img 或 text")
        mode = mode_value
        if mode == "text":
            strict = body.get("strict", True)
            if not isinstance(strict, bool):
                raise ValueError("strict 必须是布尔值")
        else:
            # The photo carries the structure; the strict toggle does not apply.
            strict = None
        if not structure and not (mode == "img2img" or analyze_source):
            raise ValueError("结构描述不能为空，请先分析图片或填写 structure_en")

        mapping = info["mapping"]
        analysis = (
            analysis_source
            if analysis_source is not None
            else (self._read_analysis_sidecar(upload_id) or {})
        )
        analysis_values = self._analysis_job_values(analysis, structure)
        structure = analysis_values["structure_en"]
        analysis_openings = analysis_values["openings"]
        analysis_geometry = analysis_values["geometry"]
        analysis_camera = analysis_values["camera"]
        analysis_framing = analysis_values["framing"]
        analysis_framing_valid = analysis_values["framing_valid"]
        analysis_framing_note = analysis_values["framing_note"]
        analysis_openings_total = analysis_values["openings_total"]
        analysis_fixed_features = analysis_values["fixed_features"]
        smoke_check = ""
        if SMOKE_MODE:
            candidate = str(body.get("smoke_check") or "").strip().lower()
            if candidate in {"high", "low", "error", "truncated", "truncate", "timeout"}:
                smoke_check = "truncated" if candidate.startswith("truncat") else candidate

        floor_en = analysis_values["floor_en"]
        lighting_summary = self._lighting_summary(pack)
        decor_summary = self._decor_summary(pack)
        daylight_direction_en = analysis_values["daylight_direction_en"]

        input_data_url = None
        input_long_edge = None
        input_bytes = None
        if mode == "img2img":
            if base == "current":
                # Refine pass: the input is the newest finished render, never
                # the uploaded original photo.
                if item is None:
                    raise ValueError("base=current 需要项目上下文")
                latest = self._latest_item_gen(item)
                if latest is None:
                    raise ValueError("还没有生成结果，无法做细节优化")
                upload_path = latest["path"]
                refine_of = str(latest["gid"])
            else:
                upload_path = (
                    source_info.get("path")
                    if source_info is not None
                    else self._upload_path(upload_id)
                )
            if upload_path is None:
                return None
            input_data_url, input_long_edge, input_bytes = self._img2img_data_url(
                upload_path
            )
        job_id = uuid.uuid4().hex
        with self.lock:
            self.remodel_jobs[job_id] = {
                "id": job_id,
                "kind": "remodel",
                "upload_id": upload_id,
                "item_id": str(item.get("item_id") or "") if item is not None else None,
                "gen_id": gen_id,
                "source_sid": source_sid,
                "note": item_note,
                "style": style_zh,
                "style_en": str(pack.get("style_en") or ""),
                "status": "queued",
                "created_wall": time.time(),
                "created_at": iso_now_ms(),
                "created_ts": time.time(),
                "created_mono": time.monotonic(),
                "stage_log": [],
                "started_mono": None,
                "finished_mono": None,
                "seconds": 0.0,
                "image_url": None,
                "file": None,
                "files": [],
                "images": [],
                "error": None,
                "model": model,
                "size": mapping["size"],
                "size_requested": mapping["size"],
                "size_final": None,
                "width": info["width"],
                "height": info["height"],
                "width_final": None,
                "height_final": None,
                "aspect": info["aspect"],
                "aspect_final": None,
                "aspect_label": info["aspect_label"],
                "aspect_exact": False,
                "aspect_note": mapping["note"],
                "variants": variants,
                "mode": mode,
                "strict": strict,
                "analyze_source": analyze_source,
                "input_data_url": input_data_url,
                "input_long_edge": input_long_edge,
                "input_bytes": input_bytes,
                "model_seconds": 0.0,
                "request_parts": [],
                "geometry": analysis_geometry,
                "camera": analysis_camera,
                "framing": analysis_framing,
                "framing_valid": analysis_framing_valid,
                "framing_note": analysis_framing_note,
                "fixed_features": analysis_fixed_features,
                "floor_en": floor_en,
                "openings": analysis_openings,
                "openings_total": analysis_openings_total,
                "lighting_plan_en": str(pack.get("lighting_plan_en") or ""),
                "layering_plan_en": str(pack.get("layering_plan_en") or ""),
                "lighting_summary": lighting_summary,
                "decor_summary": decor_summary,
                "daylight_direction_en": daylight_direction_en,
                "structure_en": structure,
                "extra_zh": extra_zh,
                "detail_enabled": detail_enabled,
                "detail_zh": detail_zh,
                "base": base,
                "refine_of": str(refine_of or ""),
                "prompt": "",
                "negative": REMODEL_NEGATIVE,
                "attempts": 0,
                "checks": [],
                "final_score": None,
                "final_attempt": None,
                "check_error": None,
                "smoke_check": smoke_check,
                "preview_url": info["preview_url"],
                "immovable_zh": analysis.get("immovable_zh")
                if isinstance(analysis.get("immovable_zh"), list)
                else [],
                "movable_zh": analysis.get("movable_zh")
                if isinstance(analysis.get("movable_zh"), list)
                else [],
                "smoke": SMOKE_MODE,
            }
        # Persist the queued record immediately: a running or failed job must
        # be visible in history even if the process never finishes it.
        self._set_remodel_stage(job_id, "queued")
        self.job_queue.put(job_id)
        return {
            "job_id": job_id,
            "upload_id": upload_id,
            "item_id": str(item.get("item_id") or "") if item is not None else None,
            "gen_id": gen_id,
            "style": style_zh,
            "style_en": str(pack.get("style_en") or ""),
            "size": mapping["size"],
            "size_requested": mapping["size"],
            "size_final": None,
            "width": info["width"],
            "height": info["height"],
            "aspect": info["aspect"],
            "aspect_label": info["aspect_label"],
            "aspect_exact": False,
            "aspect_note": mapping["note"],
            "model": model,
            "variants": variants,
            "mode": mode,
            "strict": strict,
            "input_long_edge": input_long_edge,
            "input_bytes": input_bytes,
            "model_seconds": 0.0,
            "detail_enabled": detail_enabled,
            "detail_zh": detail_zh,
            "base": base,
            "refine_of": str(refine_of or ""),
            "framing": analysis_framing,
            "framing_valid": analysis_framing_valid,
            "framing_note": analysis_framing_note,
            "negative": REMODEL_NEGATIVE,
            "preview_url": info["preview_url"],
            "status": "queued",
        }

    def remodel_job_snapshot(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.remodel_jobs.get(job_id)
            if not job:
                return None
            seconds = float(job.get("seconds") or 0)
            if job.get("status") == "running" and job.get("started_mono") is not None:
                seconds = time.monotonic() - job["started_mono"]
            stage_seconds = float(job.get("stage_seconds") or 0)
            if (
                job.get("status") in {"queued", "running"}
                and job.get("stage_started_mono") is not None
            ):
                stage_seconds = time.monotonic() - job["stage_started_mono"]
            return {
                "status": job.get("status"),
                "stage": job.get("stage") or "queued",
                "stage_started_at": job.get("stage_started_at"),
                "stage_seconds": round(stage_seconds, 1),
                "heartbeat_at": job.get("heartbeat_at"),
                "seconds": round(seconds, 1),
                "image_url": job.get("image_url"),
                "file": job.get("file"),
                "error": job.get("error"),
                "job_id": job_id,
                "item_id": job.get("item_id"),
                "gen_id": job.get("gen_id"),
                "source_sid": job.get("source_sid"),
                "note": job.get("note") or "",
                "upload_id": job.get("upload_id"),
                "style": job.get("style"),
                "style_en": job.get("style_en"),
                "size": job.get("size"),
                "size_requested": job.get("size_requested") or job.get("size"),
                "size_final": job.get("size_final"),
                "width": job.get("width"),
                "height": job.get("height"),
                "width_final": job.get("width_final"),
                "height_final": job.get("height_final"),
                "aspect": job.get("aspect"),
                "aspect_final": job.get("aspect_final"),
                "aspect_label": job.get("aspect_label"),
                "aspect_exact": job.get("aspect_exact"),
                "aspect_note": job.get("aspect_note"),
                "mode": job.get("mode") or "text",
                "strict": job.get("strict"),
                "input_long_edge": job.get("input_long_edge"),
                "input_bytes": job.get("input_bytes"),
                "model_seconds": round(float(job.get("model_seconds") or 0), 1),
                "prompt": job.get("prompt") or "",
                "negative": job.get("negative") or "",
                "model": job.get("model"),
                "created_at": job.get("created_at"),
                "result_url": job.get("image_url") if job.get("status") == "done" else None,
                "preview_url": job.get("preview_url"),
                "structure_en": job.get("structure_en"),
                "detail_enabled": bool(job.get("detail_enabled")),
                "detail_zh": str(job.get("detail_zh") or ""),
                "extra_zh": str(job.get("extra_zh") or ""),
                "extra_en": str(job.get("extra_en") or ""),
                "extra_verbatim": bool(job.get("extra_verbatim")),
                "base": str(job.get("base") or "source"),
                "refine_of": str(job.get("refine_of") or ""),
                "geometry": dict(job.get("geometry") or {}),
                "camera": dict(job.get("camera") or {}),
                "framing": dict(job.get("framing") or {}),
                "framing_valid": bool(job.get("framing_valid")),
                "framing_note": str(job.get("framing_note") or ""),
                "fixed_features": [
                    dict(entry) for entry in (job.get("fixed_features") or [])
                ],
                "floor_en": str(job.get("floor_en") or ""),
                "openings": [dict(entry) for entry in (job.get("openings") or [])],
                "openings_total": job.get("openings_total"),
                "lighting_plan_en": str(job.get("lighting_plan_en") or ""),
                "layering_plan_en": str(job.get("layering_plan_en") or ""),
                "lighting_summary": dict(job.get("lighting_summary") or {}),
                "decor_summary": dict(job.get("decor_summary") or {}),
                "daylight_direction_en": str(job.get("daylight_direction_en") or ""),
                "room_function": str(job.get("room_function") or ""),
                "room_function_source": str(job.get("room_function_source") or ""),
                "attempts": job.get("attempts") or 0,
                "checks": [
                    {
                        "attempt": entry.get("attempt"),
                        "score": entry.get("score"),
                        "items": [
                            dict(item) for item in (entry.get("items") or [])
                        ],
                        "repair_en": entry.get("repair_en") or "",
                        "error": str(entry.get("error") or ""),
                        "finish_reason": str(entry.get("finish_reason") or ""),
                        "usage": dict(entry.get("usage") or {})
                        if isinstance(entry.get("usage"), dict)
                        else {},
                        "short_form": bool(entry.get("short_form")),
                    }
                    for entry in (job.get("checks") or [])
                ],
                "final_score": job.get("final_score"),
                "final_attempt": job.get("final_attempt"),
                "check_error": job.get("check_error"),
                "immovable_zh": list(job.get("immovable_zh") or []),
                "movable_zh": list(job.get("movable_zh") or []),
                "variants": job.get("variants"),
                "files": list(job.get("files") or []),
                "images": [dict(entry) for entry in (job.get("images") or [])],
            }

    def _translate_extra_to_english(
        self, extra_zh: str, allow_verbatim: bool = True
    ) -> tuple[str, bool]:
        """Translate a Chinese requirement, returning ``(text, verbatim)``.

        ``verbatim`` means the model's output looked like an invented/expanded
        request, so the original Chinese is returned unchanged and the prompt
        marks it as verbatim instead of silently applying a different task.
        """
        text = str(extra_zh or "").strip()
        if not text:
            return "", False
        if not CJK_RE.search(text):
            return text, False
        legacy_fallback = "Integrate the requested addition into the furnishing plan."
        translated = ""
        if SMOKE_MODE:
            if "书架" in text:
                translated = "Add a bookshelf."
            elif "扩写" in text:
                translated = (
                    "Add a sideboard to the dining room and completely redesign "
                    "all the wall decor."
                )
            elif not allow_verbatim:
                return legacy_fallback, False
        else:
            try:
                key, base_url = self._read_credentials()
                prompt = (
                    "Translate the Chinese text below into one short English sentence. "
                    "Translate literally. Do not add, invent, expand or complete any "
                    "request that is not in the original text. If the text is only a "
                    "room name or a fragment, translate it as-is. Output the English "
                    "sentence only.\n\n" + text
                )
                response = self._chat_completion(
                    base_url,
                    key,
                    {
                        "model": REMODEL_TEXT_MODEL,
                        "temperature": 0,
                        "max_tokens": 200,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=60,
                )
                translated, _finish = completion_content(response)
                translated = translated.strip().strip('"').strip()
            except Exception:
                translated = ""
        if translated and not CJK_RE.search(translated):
            # A faithful one-sentence translation stays short. Longer output for
            # a short input is an expansion invented by the model, not a
            # translation, so it must never reach the image prompt.
            if len(translated) <= max(40, len(text) * 3):
                return translated[:300], False
        if allow_verbatim:
            return text, True
        return legacy_fallback, False

    @staticmethod
    def _strip_openings_count_sentence(text: str) -> str:
        cleaned = re.sub(
            r"\s*The number of openings is exactly\s+\d+;\s*do not add or remove any opening\.",
            " ",
            str(text or ""),
            flags=re.IGNORECASE,
        )
        return " ".join(cleaned.split())

    @staticmethod
    def _perspective_type(value: str) -> str:
        match = re.search(r"\b(one|two|three)[- ]point\b", str(value or ""), re.IGNORECASE)
        return f"{match.group(1).lower()}-point" if match else ""

    @staticmethod
    def _perspective_phrase(value: str) -> str:
        phrase = " ".join(str(value or "").split()).strip(" .")
        if not phrase:
            phrase = "one-point, straight at the far wall"
        if "perspective" in phrase.casefold():
            return phrase
        match = re.match(
            r"(one|two|three)[- ]point\b[,\s]*(.*)$", phrase, re.IGNORECASE
        )
        if match:
            rest = match.group(2).strip(" ,")
            phrase = f"{match.group(1).lower()}-point perspective"
            return phrase + (", " + rest if rest else "")
        return phrase + " perspective"

    @staticmethod
    def _opening_sides(analysis: dict | None) -> list[str]:
        """Distinct non-ceiling/floor opening sides, in source order."""
        if not isinstance(analysis, dict):
            return []
        raw_openings = analysis.get("openings")
        if not isinstance(raw_openings, list):
            return []
        sides: list[str] = []
        for entry in raw_openings:
            if not isinstance(entry, dict):
                continue
            side = " ".join(str(entry.get("side") or "").split()).strip(" .,").lower()
            if not side:
                continue
            if side.casefold() in {"ceiling", "floor"}:
                continue
            if side not in sides:
                sides.append(side)
        return sides

    @staticmethod
    def _daylight_direction_sentence(analysis: dict | None) -> str:
        sides = GalleryStore._opening_sides(analysis)
        if sides:
            if len(sides) == 1:
                side_list = sides[0]
            elif len(sides) == 2:
                side_list = sides[0] + " and " + sides[1]
            else:
                side_list = ", ".join(sides[:-1]) + " and " + sides[-1]
            lead = (
                "The room is lit by one key light: daylight entering from the "
                f"{side_list} opening(s)."
            )
            return lead + " Every shadow in the room runs away from that wall."
        # A recorded, empty openings list means the source really has no window
        # (or only ceiling/floor light sources). Never assert one exists.
        if isinstance(analysis, dict) and isinstance(analysis.get("openings"), list):
            return (
                "No exterior window or glass door is visible anywhere in the source "
                "photograph. Light the room with soft diffused ambient daylight that "
                "enters from outside the frame; the ambient level matches the source "
                "photo. Every shadow in the room is soft and follows the same direction "
                "as in the source. Do not add, imply or invent a window, glass door or "
                "any new opening to justify the light."
            )
        # No structure analysis at all: we do not know whether there is a window,
        # so defer to the light already visible in the photo.
        return (
            "Match the daylight direction, softness and shadow direction already "
            "visible in the source photograph; that existing light is the only key "
            "light. Do not invent a window, glass door or any new opening to justify "
            "the light."
        )

    @staticmethod
    def _strip_competing_light(text: str) -> str:
        """Remove styling-DNA wording that asks for a second key light or lamp glow."""
        value = " ".join(str(text or "").split())
        patterns = (
            r"\s+under\s+soft\s+diffused\s+daylight",
            r"\s*,?\s*soft\s+diffused\s+daylight"
            r"(?:\s+and\s+(?:warm\s+)?(?:lamp\s+)?glow)?",
            r"\s*,?\s*(?:warm\s+)?lamp\s+glow",
            r"\s*,?\s*soft\s+diffused\s+light",
            r"\s*,?\s*warm\s+side\s+daylight",
            r"\s*,?\s*diffused\s+side\s+daylight",
            r"\s*,?\s*soft\s+shadow(?:s|\s+falloff)?",
        )
        for pattern in patterns:
            value = re.sub(pattern, "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*,\s*(?=[.,;])", "", value)
        value = re.sub(r",\s*,+", ",", value)
        value = re.sub(r"\s+([.,;])", r"\1", value)
        value = re.sub(r"([.,;])\s*([.,;])", r"\1", value)
        value = " ".join(value.split())
        return value.strip(" ,;")

    @staticmethod
    def _with_article(phrase: str) -> str:
        value = " ".join(str(phrase or "").split()).strip(" .,;:-")
        if not value:
            return value
        if re.match(r"^(?:a|an|the|two|three|four)\b", value, flags=re.IGNORECASE):
            return value
        one = re.match(r"^one\s+(.+)$", value, flags=re.IGNORECASE)
        if one:
            value = one.group(1)
        article = "an" if value[0].lower() in "aeiou" else "a"
        return f"{article} {value}"

    @classmethod
    def _style_lighting_sentence(cls, pack: dict) -> str:
        """One style sentence: at most two fixture families plus material reveal."""
        plan = " ".join(str(pack.get("lighting_plan_en") or "").split())
        layering = " ".join(str(pack.get("layering_plan_en") or "").split())
        fixture_re = re.compile(
            r"\b(?:[a-z][a-z-]*\s+){0,4}(?:pendants?|sconces?|table lamps?|"
            r"floor lamps?|lamps?|lanterns?|spotlights?|spot lights?|track spots?|"
            r"downlights?|uplights?|cove leds?|led strips?|wall lights?)\b",
            flags=re.IGNORECASE,
        )
        fixtures: list[str] = []
        for match in fixture_re.finditer(plan):
            value = " ".join(match.group(0).split()).strip(" .,;:-")
            while True:
                cleaned = re.sub(
                    r"^(?:including|with|and|plus|so|the)\s+",
                    "",
                    value,
                    flags=re.IGNORECASE,
                )
                if cleaned == value:
                    break
                value = cleaned
            key = re.sub(r"\W+", " ", value.casefold()).strip()
            if value and key not in {
                re.sub(r"\W+", " ", item.casefold()).strip() for item in fixtures
            }:
                fixtures.append(value)
            if len(fixtures) >= 2:
                break
        if not fixtures:
            slot = re.search(
                r"Lighting fixtures:\s*(.*?)(?:\.\s*(?:Density target:|$)|$)",
                layering,
                flags=re.IGNORECASE,
            )
            if slot:
                for piece in re.split(r",|\band\b", slot.group(1)):
                    value = " ".join(piece.split()).strip(" .,;:-")
                    if value and fixture_re.search(value):
                        fixtures.append(value)
                    if len(fixtures) >= 2:
                        break
        if not fixtures:
            fixtures = ["warm table lamp"]

        material_re = re.compile(
            r"\b(?:[a-z][a-z-]*\s+){0,2}"
            r"(?:nap|grain|weave|fibers?|fibres?|texture|patina)\b",
            flags=re.IGNORECASE,
        )
        materials: list[str] = []
        for match in material_re.finditer(plan):
            value = " ".join(match.group(0).split()).strip(" .,;:-")
            while True:
                cleaned = re.sub(
                    r"^(?:and|but|so|which|that|the|a|an|its|their|this|these|"
                    r"those|with|across|along|over|under|into|onto|through|"
                    r"reveals?|lifts?|opens?|reads?|traces?|shows?|lets?|"
                    r"gentle|warm|cool)\s+",
                    "",
                    value,
                    flags=re.IGNORECASE,
                )
                if cleaned == value:
                    break
                value = cleaned
            value = value.strip(" .,;:-")
            key = value.casefold()
            if value and key not in {item.casefold() for item in materials}:
                materials.append(value)
            if len(materials) >= 3:
                break
        if not materials:
            materials = ["natural material grain", "textile weave"]

        fixture_text = " and ".join(cls._with_article(item) for item in fixtures[:2])
        verb = "are" if len(fixtures[:2]) > 1 else "is"
        if len(materials) == 1:
            material_text = materials[0]
        else:
            material_text = ", ".join(materials[:-1]) + " and " + materials[-1]
        return (
            f"Style-specific lighting: {fixture_text} {verb} the only practical "
            f"fixtures, and the daylight reveals {material_text}."
        )

    @staticmethod
    def _dedupe_sentences(*blocks: str) -> str:
        seen: set[str] = set()
        sentences: list[str] = []
        for block in blocks:
            text = " ".join(str(block or "").split())
            if not text:
                continue
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                sentence = sentence.strip()
                if not sentence:
                    continue
                key = re.sub(r"\W+", " ", sentence.casefold()).strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                sentences.append(sentence)
        return " ".join(sentences)

    @classmethod
    def _lighting_section(
        cls, pack: dict, analysis: dict | None, mode: str
    ) -> str:
        if not str(pack.get("lighting_plan_en") or "").strip():
            pack = dict(pack)
            pack["lighting_plan_en"] = REMODEL_LIGHTING_FALLBACK
        style_sentence = cls._style_lighting_sentence(pack)
        has_openings = bool(cls._opening_sides(analysis))
        parts = [
            "LIGHTING DESIGN",
            cls._daylight_direction_sentence(analysis),
            (
                "Practical fixtures are decorative only: at most two visible "
                "fixtures, warm 2700 K, low output. They do not cast shadows, do not "
                "add fill light, and do not glow."
            ),
            (
                "No bloom, no halo, no light spill on the ceiling, no lens flare, no "
                "double shadows. The window stays readable, never blown to white."
                if has_openings
                else "No specular blowout anywhere: keep wall and floor values "
                "readable, no white clipping."
            ),
            "Interior exposure is balanced for the room, not for the view outside.",
        ]
        if style_sentence:
            parts.append(style_sentence)
        if mode == "img2img":
            parts.append(
                "You may change the lighting completely; the original photo's "
                "light is not a constraint."
            )
        parts.append(
            "Keep window openings physically unchanged while changing what comes "
            "through them."
            if has_openings
            else "No opening may be added, widened, moved or implied — every wall "
            "surface stays exactly as solid as in the source."
        )
        parts.append(
            "HARD RULE: never add, move, resize, remove or invent an opening of any "
            "kind — window, door, doorway, transom, arch, niche or glass panel — even "
            "if the lighting, composition or style would benefit from it."
        )
        return "\n".join(parts)

    @staticmethod
    def _style_materials_line(pack: dict) -> str:
        """Concrete palette + finish DNA for rooms without a furniture plan."""
        palette = pack.get("palette") if isinstance(pack, dict) else None
        swatches: list[str] = []
        if isinstance(palette, list):
            for entry in palette[:6]:
                if not isinstance(entry, dict):
                    continue
                hex_value = " ".join(str(entry.get("hex") or "").split())
                if not hex_value:
                    continue
                name = " ".join(str(entry.get("name_en") or "").split())
                swatches.append(f"{hex_value} {name}".strip())
        materials = " ".join(str((pack or {}).get("materials_en") or "").split())
        parts: list[str] = []
        if swatches:
            parts.append(
                "Colour and material palette, applied to this room's own pieces: "
                + ", ".join(swatches)
                + "."
            )
        if materials:
            parts.append(f"Finishes: {materials}.")
        return " ".join(parts)

    @classmethod
    def _furnishing_section(
        cls, pack: dict, extra: str, room_function: str = "living"
    ) -> str:
        style_en = " ".join(str(pack.get("style_en") or "the selected style").split())
        extra = " ".join(str(extra or "").split())
        requirement = _additional_requirement_sentence(extra)
        function = _normalize_room_function(room_function) or "living"

        if function == "living":
            # The living-room copy is the original behaviour: keep it
            # character-for-character so existing renders do not regress.
            dna = cls._strip_competing_light(pack.get("style_dna_en"))
            layering = " ".join(str(pack.get("layering_plan_en") or "").split())
            plan = " ".join(str(pack.get("furniture_plan_en") or "").split())
            if requirement:
                plan = (plan + " " + requirement).strip()
            layering = re.sub(
                r"\s*Density target:.*$", "", layering, flags=re.IGNORECASE
            ).strip()
            body = cls._dedupe_sentences(layering, plan)
            parts = ["FURNISHING AND DECOR"]
            if dna:
                parts.append(f"Interior styling, in {style_en}: {dna}")
            if body:
                parts.append(body)
            parts.append(REMODEL_DENSITY_TARGET)
            return "\n".join(parts)

        parts = ["FURNISHING AND DECOR", "ROOM FUNCTION"]
        if function in {"unknown", "other"}:
            parts.append(
                "The room's function could not be confirmed. Keep the room's "
                "existing function and its existing furniture set; restyle "
                "surfaces, materials, colours and lighting only. Do not convert "
                "the room to another function."
            )
            if style_en and style_en != "the selected style":
                parts.append(
                    f"The selected style is {style_en}. Apply the style's "
                    "materials, colours and hardware to the existing pieces."
                )
            else:
                parts.append(
                    "Apply the style's materials, colours and hardware to the "
                    "existing pieces."
                )
            style_materials = cls._style_materials_line(pack)
            if style_materials:
                parts.append(style_materials)
            if requirement:
                parts.append(requirement)
            parts.append(REMODEL_DENSITY_TARGET)
            return "\n".join(parts)

        label = REMODEL_ROOM_FUNCTION_LABELS.get(function, "room")
        parts.append(f"This room is a {label}; furnish it as a {label}.")
        furniture = _REMODEL_ROOM_FUNCTION_FURNITURE.get(function, "")
        if furniture:
            sentence = "Furnish the room with " + furniture + "."
            if requirement:
                sentence += " " + requirement
            parts.append(sentence)
        elif requirement:
            parts.append(requirement)
        if style_en and style_en != "the selected style":
            parts.append(
                f"The selected style is {style_en}. Apply the style's materials, "
                "colours and hardware to these pieces."
            )
        else:
            parts.append(
                "Apply the style's materials, colours and hardware to these "
                "pieces."
            )
        style_materials = cls._style_materials_line(pack)
        if style_materials:
            parts.append(style_materials)
        parts.append(REMODEL_DENSITY_TARGET)
        return "\n".join(parts)

    @classmethod
    def _interior_details_section(cls, detail_en: str) -> str:
        """Optional small decor on existing surfaces; never architecture."""
        detail_en = " ".join(str(detail_en or "").split())
        parts = [
            "INTERIOR DETAILS (allowed additions)",
            (
                "You may add small interior details on existing surfaces: potted "
                "plants and greenery, framed wall art, stacked books and magazines, "
                "ceramics and vases, candles, woven baskets, layered textiles "
                "(throws, cushions, rugs), small tabletop objects and small table "
                "lamps."
            ),
            (
                "Every added item must be small and secondary: no new built-ins, "
                "no large furniture, no structural elements. Do not change wall "
                "planes, opening positions or sizes, ceiling height, floor level, "
                "camera position, perspective or framing. Details must sit on "
                "existing surfaces and must never block a doorway, window or "
                "walkway."
            ),
        ]
        if detail_en:
            parts.append(f"Requested details: {detail_en}")
        return "\n".join(parts)

    @classmethod
    def _build_refine_prompt(
        cls, pack: dict, detail_en: str, size_label: str
    ) -> str:
        """Refine pass: keep the finished render, only add small decor."""
        detail_en = " ".join(str(detail_en or "").split())
        style_en = " ".join(str((pack or {}).get("style_en") or "").split())
        parts = [
            "REFINE PASS. The input image is already a finished photorealistic "
            "interior render, not an original old-room photograph.",
            (
                "Keep everything already in the image unchanged: same "
                "architecture, same wall planes and openings, same ceiling "
                "height, same floor level, same furniture, same materials, "
                "colours, lighting and shadows, same camera position, "
                "perspective, framing and aspect ratio. Do not move, remove, "
                "replace or resize anything."
            ),
            (
                "Only add small decorative layers sitting on existing "
                "surfaces: potted plants and greenery, framed wall art, "
                "stacked books and magazines, ceramics and vases, candles, "
                "woven baskets, layered textiles (throws, cushions, rugs), "
                "small tabletop objects and small table lamps."
            ),
            (
                "Every addition must stay small and secondary. Do not block "
                "doorways, window openings or walkways. Do not introduce "
                "large furniture, cabinets, built-in storage or structural "
                "elements."
            ),
        ]
        if detail_en:
            parts.append(f"Requested details: {detail_en}")
        if style_en:
            parts.append(
                f"The existing interior style is {style_en}; any added decor "
                "must match it."
            )
        parts.append(REMODEL_REALISM)
        parts.append(ASPECT_HINTS.get(size_label, "Vertical 3:4 composition."))
        parts.append(
            "The result must look like the same photograph with a few small "
            "objects added, not a re-rendered new space."
        )
        prompt = "\n\n".join(part for part in parts if part.strip())
        if CJK_RE.search(prompt):
            raise ValueError("生成提示词必须是英文")
        return prompt

    @staticmethod
    def _lighting_summary(pack: dict) -> dict:
        text = " ".join(str(pack.get("lighting_plan_en") or "").split())
        kelvin: list[int] = []
        for match in re.finditer(r"\b(\d{3,4})\s*(?:K|kelvin)\b", text, re.IGNORECASE):
            value = int(match.group(1))
            if value not in kelvin:
                kelvin.append(value)
        time_terms = (
            ("late afternoon", "傍晚斜阳"),
            ("soft overcast noon", "阴天正午"),
            ("golden hour", "黄金时刻"),
            ("early morning", "清晨"),
            ("overcast", "阴天柔光"),
            ("morning", "上午"),
            ("midday", "正午"),
            ("noon", "正午"),
            ("afternoon", "午后"),
            ("evening", "夜晚"),
            ("dusk", "黄昏"),
            ("twilight", "暮色"),
            ("night", "夜晚"),
            ("daylight", "自然光"),
        )
        time_en = ""
        time_zh = ""
        lowered = text.casefold()
        for term, label in time_terms:
            if term in lowered:
                time_en = term
                time_zh = label
                break
        if not time_en and text:
            first = re.split(r"(?<=[.!?])\s+", text)[0]
            words = first.split()
            time_en = " ".join(words[:10]).strip(" .,")
            time_zh = time_en
        return {
            "time_en": time_en,
            "time_zh": time_zh,
            "kelvin": kelvin,
            "kelvin_text": " / ".join(f"{value} K" for value in kelvin),
        }

    @staticmethod
    def _count_decor_items(layering_plan_en: str) -> int:
        body = re.split(
            r"\bDensity target:", str(layering_plan_en or ""), maxsplit=1, flags=re.IGNORECASE
        )[0]
        seen: set[str] = set()
        count = 0
        for slot in re.split(r"[;.]", body):
            if ":" in slot:
                _label, slot = slot.split(":", 1)
            for piece in re.split(r",", slot):
                item = " ".join(piece.split()).strip(" .;:-")
                item = re.sub(
                    r"^(?:a|an|the|one|two|three|four|five|six|seven|eight)\s+",
                    "",
                    item,
                    flags=re.IGNORECASE,
                ).strip()
                if not item or not re.search(r"[A-Za-z]", item):
                    continue
                words = item.split()
                if len(words) > 9:
                    continue
                key = re.sub(r"\W+", " ", item.casefold()).strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                count += 1
        return count

    @classmethod
    def _decor_summary(cls, pack: dict) -> dict:
        layering = " ".join(str(pack.get("layering_plan_en") or "").split())
        item_match = re.search(
            r"at least\s+(\d+)\s+distinct furnishing and decor items",
            layering,
            flags=re.IGNORECASE,
        )
        surface_match = re.search(
            r"at least\s+(\d+)\s+styled surfaces", layering, flags=re.IGNORECASE
        )
        item_target = int(item_match.group(1)) if item_match else 12
        surface_count = int(surface_match.group(1)) if surface_match else 3
        item_count = cls._count_decor_items(layering)
        if item_count <= 0:
            item_count = item_target
        return {
            "item_count": item_count,
            "item_target": item_target,
            "surface_count": surface_count,
            "density_target": REMODEL_DENSITY_TARGET,
        }

    @classmethod
    def _build_remodel_prompt(
        cls,
        pack: dict,
        structure_en: str,
        extra_en: str,
        size: str,
        analysis: dict | None = None,
        strict: bool = True,
        repair_en: str = "",
        mode: str = "text",
        detail_enabled: bool = False,
        detail_en: str = "",
        room_function: str = "living",
        extra_verbatim: bool = False,
    ) -> str:
        """Geometry-first prompt. Style words come after the architectural locks."""
        if mode not in REMODEL_MODES:
            raise ValueError("mode 必须是 img2img 或 text")
        analysis = analysis if isinstance(analysis, dict) else {}
        structure = " ".join(str(structure_en or "").split())
        repair = " ".join(str(repair_en or "").split())
        lighting_section = cls._lighting_section(pack, analysis, mode)
        furnishing_section = cls._furnishing_section(
            pack, extra_en, room_function
        )
        detail_section = (
            cls._interior_details_section(detail_en) if detail_enabled else ""
        )

        if mode == "img2img":
            # The photo itself carries geometry. No composition lock and no
            # wall-proportion numbers: contradicting the pixels pulls the
            # result away from the source. When no text analysis exists, the
            # structure sentence is omitted entirely rather than left empty.
            parts = [REMODEL_IMG2IMG_INSTRUCTION]
            if structure:
                parts.append("The input photograph shows: " + structure)
            parts.extend([lighting_section, furnishing_section])
            if detail_section:
                parts.append(detail_section)
            parts.append(
                "Everything in the original room except the architectural shell is "
                "replaced: all furniture, soft furnishings, light fittings, textiles, "
                "decor and colours are new."
            )
            if repair:
                if not repair.lower().startswith("hard corrections"):
                    repair = (
                        "Hard corrections, these override other instructions: " + repair
                    )
                parts.append(repair)
            parts.append(REMODEL_REALISM)
            parts.append(ASPECT_HINTS.get(size, "Vertical 3:4 composition."))
            prompt = "\n\n".join(part for part in parts if part.strip())
            if _prompt_has_disallowed_cjk(prompt, extra_verbatim):
                raise ValueError("生成提示词必须是英文")
            return prompt

        if not structure:
            raise ValueError("结构描述为空，无法生成纯文本提示词")
        camera = analysis.get("camera") if isinstance(analysis.get("camera"), dict) else {}
        horizon = coerce_float(camera.get("horizon_fraction"), None, 0.0, 1.0)
        if horizon is None:
            horizon_match = re.search(
                r"horizon\s+(\d+(?:\.\d+)?)\s*%\s+of\s+(?:the\s+)?frame\s+height",
                structure,
                flags=re.IGNORECASE,
            )
            horizon = (
                coerce_float(horizon_match.group(1), 55.0, 0.0, 100.0) / 100.0
                if horizon_match
                else 0.55
            )
        horizon_pct = int(round(horizon * 100))

        perspective_raw = " ".join(str(camera.get("perspective") or "").split()).strip(" .")
        if not perspective_raw:
            perspective_match = re.search(
                r"\b(?:one|two|three)[- ]point(?:\s+perspective)?[^.;]*",
                structure,
                flags=re.IGNORECASE,
            )
            perspective_raw = (
                perspective_match.group(0).strip()
                if perspective_match
                else "one-point, straight at the far wall"
            )
        perspective_phrase = cls._perspective_phrase(perspective_raw)
        perspective_type = cls._perspective_type(perspective_raw)

        lens = coerce_float(camera.get("lens_mm"), None, 8.0, 200.0)
        if lens is None:
            lens_match = re.search(
                r"(\d+(?:\.\d+)?)\s*mm", structure, flags=re.IGNORECASE
            )
            lens = coerce_float(lens_match.group(1), 24.0, 8.0, 200.0) if lens_match else 24.0
        tilt = coerce_float(camera.get("tilt_deg"), None, -45.0, 45.0)
        if tilt is None:
            tilt_match = re.search(
                r"tilt\s+(-?\d+(?:\.\d+)?)\s*degrees?", structure, flags=re.IGNORECASE
            )
            tilt = coerce_float(tilt_match.group(1), 0.0, -45.0, 45.0) if tilt_match else 0.0
        lens_text = fmt_number(lens, 0)
        tilt_text = fmt_number(tilt, 1)

        opening_count = analysis.get("openings_total")
        if isinstance(opening_count, bool) or not isinstance(opening_count, int):
            raw_openings = analysis.get("openings")
            opening_count = (
                len(raw_openings) if isinstance(raw_openings, list) else None
            )
        if opening_count is None or opening_count == 0:
            count_match = re.search(
                r"The number of openings is exactly\s+(\d+)", structure, flags=re.IGNORECASE
            )
            if count_match:
                opening_count = int(count_match.group(1))
        if opening_count is None:
            opening_count = 0
        opening_count = max(0, opening_count)

        if strict:
            shell = "Architectural shell, fixed and unchangeable: " + structure
        else:
            shell = (
                "Architectural shell, fixed and unchangeable: "
                + cls._strip_openings_count_sentence(structure)
            )

        parts = [shell]
        if strict:
            parts.append(
                "Composition lock: single frame from the same camera position; "
                f"{perspective_phrase}; horizon line at {horizon_pct}% of frame height; "
                f"verticals truly vertical; {tilt_text} degree tilt; the room is not "
                "wider, deeper or taller than the dimensions above; mild "
                f"{lens_text} mm wide-angle perspective only, no fisheye, no added space "
                "at the frame edges."
            )
            framing_result = cls._normalize_framing(analysis.get("framing"))
            framing_valid = analysis.get("framing_valid")
            if not isinstance(framing_valid, bool):
                framing_valid = framing_result["valid"]
            if framing_valid and framing_result["valid"]:
                parts.append(cls._framing_lock(framing_result["block"]))
            parts.append(
                f"Openings count lock: exactly {opening_count} openings, on the described "
                "walls, at the described offsets and sizes; no added doors, no added "
                "windows, no added wall panels, no removed openings."
            )
        parts.append(lighting_section)
        parts.append(furnishing_section)
        if detail_section:
            parts.append(detail_section)
        parts.append(
            "Everything in the original room except the architectural shell is replaced: "
            "all furniture, soft furnishings, light fittings, textiles, decor and colours "
            "are new."
        )
        if repair:
            if not repair.lower().startswith("hard corrections"):
                repair = "Hard corrections, these override other instructions: " + repair
            parts.append(repair)
        parts.append(REMODEL_REALISM)
        parts.append(ASPECT_HINTS.get(size, "Vertical 3:4 composition."))
        prompt = "\n\n".join(part for part in parts if part.strip())
        if perspective_type and perspective_type != "one-point":
            prompt = re.sub(
                r"\bone[- ]point\s+perspective\b",
                f"{perspective_type} perspective",
                prompt,
                flags=re.IGNORECASE,
            )
        if _prompt_has_disallowed_cjk(prompt, extra_verbatim):
            raise ValueError("生成提示词必须是英文")
        return prompt

    @classmethod
    def _self_check_prompt(cls, analysis: dict, short: bool = False) -> str:
        geometry = analysis.get("geometry") if isinstance(analysis.get("geometry"), dict) else {}
        camera = analysis.get("camera") if isinstance(analysis.get("camera"), dict) else {}
        openings = analysis.get("openings") if isinstance(analysis.get("openings"), list) else []
        framing_result = cls._normalize_framing(analysis.get("framing"))
        fixed_features = cls._clean_fixed_features(analysis.get("fixed_features"))
        room = " ".join(str(geometry.get("room_type_en") or "room").split()).strip(" .")
        shape = " ".join(str(geometry.get("shape_en") or "rectangular").split()).strip(" .")
        opening_bits = []
        for entry in openings:
            if not isinstance(entry, dict):
                continue
            opening_bits.append(
                "{} {} {} x {} m, sill {} m, {}% along the wall".format(
                    entry.get("side") or "wall",
                    entry.get("type") or "opening",
                    fmt_number(entry.get("width_m")),
                    fmt_number(entry.get("height_m")),
                    fmt_number(entry.get("sill_m")),
                    int(
                        round(
                            coerce_float(
                                entry.get("center_offset_fraction"), 0.5, 0.0, 1.0
                            )
                            * 100
                        )
                    ),
                )
            )
        fixed_bits = []
        for entry in fixed_features:
            key = str(entry.get("key") or "").strip()
            detail = str(entry.get("detail") or "").strip()
            fixed_bits.append(f"{key}: {detail}" if detail else key)
        framing_valid = analysis.get("framing_valid")
        if not isinstance(framing_valid, bool):
            framing_valid = framing_result["valid"]
        if framing_valid and framing_result["valid"]:
            block = framing_result["block"]
            pieces = []
            front = block.get("front_wall_and_opening_span")
            if front:
                pieces.append(
                    "the wall carrying the main opening runs from x="
                    f"{fmt_number(front.get('from_x'), 2)} to "
                    f"x={fmt_number(front.get('to_x'), 2)}"
                )
            pieces.append(
                "the opening fills about "
                f"{int(round(float(block.get('opening_width_over_wall_width') or 0) * 100))}% "
                "of that wall"
            )
            for key, label in (
                ("right_wall_span", "the right wall plane runs from"),
                ("left_wall_span", "the left wall plane runs from"),
            ):
                span = block.get(key)
                if span:
                    pieces.append(
                        f"{label} x={fmt_number(span.get('from_x'), 2)} to "
                        f"x={fmt_number(span.get('to_x'), 2)}"
                    )
            pieces.append(
                f"the ceiling line sits at y={fmt_number(block.get('ceiling_bottom_y'), 2)} "
                f"and the far-wall floor line at y={fmt_number(block.get('floor_top_y'), 2)}"
            )
            framing_fact = "- framing measured on the source photo: " + "; ".join(pieces)
        else:
            framing_fact = (
                "- framing: not measured on the source photo ("
                + str(analysis.get("framing_note") or framing_result["note"])
                + ")"
            )
        horizon = int(
            round(coerce_float(camera.get("horizon_fraction"), 0.55, 0.0, 1.0) * 100)
        )
        floor_en = " ".join(str(analysis.get("floor_en") or "").split()).strip(" .")
        if not floor_en:
            floor_en = (
                "floor with a visible level change"
                if geometry.get("floor_level_changes")
                else "continuous floor with no level changes"
            )
        daylight_direction = cls._daylight_direction_sentence(analysis)
        has_locked_analysis = bool(
            analysis.get("geometry")
            or analysis.get("camera")
            or openings
            or fixed_features
            or str(analysis.get("floor_en") or "").strip()
            or analysis.get("framing")
        )
        if has_locked_analysis:
            facts = "\n".join(
                [
                    f"- room: {room}, {shape}, {fmt_number(geometry.get('width_m'))} m x "
                    f"{fmt_number(geometry.get('depth_m'))} m, ceiling "
                    f"{fmt_number(geometry.get('ceiling_m'))} m",
                    f"- floor material and laying pattern (fixed): {floor_en}",
                    "- floor level changes: "
                    + ("yes" if geometry.get("floor_level_changes") else "no"),
                    f"- openings (exactly {len(openings)}): "
                    + ("; ".join(opening_bits) if opening_bits else "none"),
                    f"- daylight direction: {daylight_direction}",
                    f"- camera: {camera.get('perspective') or 'one-point, straight at the far wall'}; "
                    f"horizon {horizon}% of frame height; tilt "
                    f"{fmt_number(camera.get('tilt_deg'))} degrees; "
                    f"{fmt_number(camera.get('lens_mm'), 0)} mm lens",
                    framing_fact,
                    "- fixed features that must stay: "
                    + ("; ".join(fixed_bits) if fixed_bits else "none recorded"),
                ]
            )
        else:
            # img2img may run without any text analysis. The self-check is
            # image-to-image, so compare the generated image directly to the
            # source instead of inventing zero-valued geometry facts.
            facts = (
                "- no text analysis is available; compare IMAGE 2 directly to "
                "IMAGE 1 for the fixed architecture, openings, camera, framing, "
                "lighting, furnishing richness and photographic realism."
            )
        item_lines = [
            f'    {{"key": "{key}", "ok": true, "note": "short note"}},'
            if index < len(REMODEL_CHECK_KEYS) - 1
            else f'    {{"key": "{key}", "ok": true, "note": "short note"}}'
            for index, key in enumerate(REMODEL_CHECK_KEYS)
        ]
        if short:
            json_hint = (
                '{\n'
                '  "score": 0.0,\n'
                '  "items": [\n'
                + "\n".join(item_lines)
                + "\n  ]\n}"
            )
            rules = (
                f"- Include all {len(REMODEL_CHECK_KEYS)} checklist keys exactly once.\n"
                f"- note must be {SELF_CHECK_SHORT_NOTE_WORDS} words or fewer.\n"
                "- The server computes the weighted score from the item booleans.\n"
                "- Be strict. A missing cornice, a narrower opening, a changed floor "
                "pattern or a changed perspective is not ok.\n"
            )
        else:
            json_hint = (
                '{\n'
                '  "score": 0.0,\n'
                '  "items": [\n'
                + "\n".join(item_lines)
                + '\n  ],\n'
                '  "repair_en": "one or two imperative English sentences"\n'
                '}'
            )
            rules = (
                f"- Include all {len(REMODEL_CHECK_KEYS)} checklist keys exactly once.\n"
                "- The server computes the weighted score from the item booleans.\n"
                "- Be strict. A missing cornice, a narrower opening, a changed floor "
                "pattern, a wider room or a changed perspective is not ok.\n"
                "- repair_en must be English and say exactly what to fix, naming the failed "
                "item. Leave it empty only when everything is ok.\n"
            )
        return (
            "You are a strict architectural QA reviewer for a photorealistic interior remodel.\n\n"
            "IMAGE 1 is the original uploaded photograph. IMAGE 2 is the generated remodel. "
            "Compare the generated image to the source photo and to the locked source facts "
            "below. Judge the fixed structure, the lighting consistency, the furnishing "
            "richness and photographic realism.\n\n"
            "Locked source facts:\n"
            + facts
            + "\n\nChecklist keys:\n"
            "- openings: every analysed opening is present on the correct wall with its type.\n"
            "- count: the number of openings is exactly the analysed number; none added or removed.\n"
            "- framing: the main opening keeps the same width relative to the wall that carries "
            "it; the wall spans and ceiling/floor lines keep similar positions in the frame; the "
            "room does not read wider.\n"
            "- perspective: the same perspective type and camera angle as the source photo.\n"
            "- ceiling: the ceiling construction matches the source (flat or beamed, height).\n"
            "- floor: the floor material and laying pattern match the source exactly "
            "(plank direction, tile grid, herringbone, terrazzo) and any level change is "
            "unchanged.\n"
            "- fixed_features: every fixed feature named above is visible and unchanged.\n"
            "- storage: at least one bookshelf, shelf unit or sideboard with visible "
            "books is present, plus one desk, console or other work surface.\n"
            "- lighting: a single shadow direction runs away from the window wall; no "
            "visible halo or bloom around fixtures; the window is not blown out; "
            "practical fixtures add no extra shadows or fill light.\n"
            "- richness: at least 10 distinct furnishing/decor items and at least 3 styled "
            "surfaces are visible; no empty showroom surfaces.\n"
            "- realism: visible material micro-texture such as weave, grain and fabric "
            "nap; no uniform plastic surfaces; no HDR halo; subtle grain and surface "
            "imperfections present; reads as a photograph.\n\n"
            + SELF_CHECK_INSTRUCTION
            + "\n"
            + json_hint
            + "\n\nRules:\n"
            + rules
        )

    @classmethod
    def _normalize_check(cls, obj: dict) -> dict:
        raw_items = obj.get("items") if isinstance(obj, dict) else None
        if not isinstance(raw_items, list) or not raw_items:
            raise RuntimeError("自检 JSON 缺少 items")

        def text(value) -> str:
            return " ".join(str(value or "").split())

        found: dict[str, dict] = {}
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            key = text(entry.get("key")).lower()
            if key not in REMODEL_CHECK_KEYS or key in found:
                continue
            found[key] = {
                "key": key,
                "ok": coerce_bool(entry.get("ok"), False),
                "note": text(entry.get("note")),
            }
        items = []
        for key in REMODEL_CHECK_KEYS:
            items.append(
                found.get(key)
                or {
                    "key": key,
                    "ok": False,
                    "note": "self-check did not report this item",
                }
            )
        by_key = {item["key"]: item for item in items}
        architecture_ok = sum(
            1 for key in REMODEL_ARCHITECTURE_KEYS if by_key[key]["ok"]
        )
        architecture_score = architecture_ok / max(1, len(REMODEL_ARCHITECTURE_KEYS))
        component_scores = {
            "architecture": architecture_score,
            "lighting": 1.0 if by_key["lighting"]["ok"] else 0.0,
            "richness": 1.0 if by_key["richness"]["ok"] else 0.0,
            "realism": 1.0 if by_key["realism"]["ok"] else 0.0,
        }
        score = round(
            sum(
                component_scores[name] * float(weight)
                for name, weight in REMODEL_SCORE_WEIGHTS.items()
            ),
            3,
        )
        repair = text(obj.get("repair_en"))
        if CJK_RE.search(repair):
            repair = ""
        failed = [item for item in items if not item["ok"]]
        if not repair and failed:
            notes = []
            for item in failed:
                note = item["note"]
                if (
                    not note
                    or note == "self-check did not report this item"
                    or note.casefold().strip(" .") in {"ok", "match", "matches", "fine"}
                ):
                    note = REMODEL_CHECK_PLAIN_EN.get(item["key"], "")
                if note:
                    notes.append(f"{item['key']}: {note}")
            repair = "; ".join(notes[:4])
            if repair and not repair.endswith("."):
                repair += "."
        if not repair and failed:
            repair = "Restore every failed structural and styling detail exactly as in the source photo."
        return {"score": score, "items": items, "repair_en": repair}

    @staticmethod
    def _smoke_check_payload(attempt: int, mode: str) -> dict:
        if mode == "error":
            raise RuntimeError("smoke self-check error")
        low = mode == "low" and attempt <= 1
        notes = {
            "openings": "openings match the source",
            "count": "opening count matches",
            "framing": "far wall is narrower, room reads wider" if low else "framing matches",
            "perspective": "perspective matches",
            "ceiling": "ceiling matches",
            "floor": "floor material and plank direction match",
            "fixed_features": "cornice is missing" if low else "fixed features match",
            "storage": "bookshelf missing" if low else "bookshelf with books and a writing desk",
            "lighting": "one key light from the window side, no blown window",
            "richness": "thirteen decor items across four styled surfaces",
            "realism": "natural material grain and soft shadow falloff",
        }
        items = [
            {
                "key": key,
                "ok": not (low and key in {"framing", "fixed_features", "storage"}),
                "note": notes[key],
            }
            for key in REMODEL_CHECK_KEYS
        ]
        return {
            "score": sum(1 for item in items if item["ok"]) / len(items),
            "items": items,
            "repair_en": (
                "Keep the far wall wider in frame; restore the plaster cornice along the "
                "ceiling."
                if low
                else ""
            ),
        }

    @staticmethod
    def _check_failure_entry(exc: BaseException, response: dict | None = None) -> dict:
        finish_reason = str((response or {}).get("finish_reason") or "")
        usage = (response or {}).get("usage")
        usage = usage if isinstance(usage, dict) else {}
        detail = clean_error(exc)
        if finish_reason:
            detail += f"; finish_reason={finish_reason}"
        if usage:
            detail += "; usage=" + json.dumps(usage, ensure_ascii=False, sort_keys=True)
        return {
            "attempt": 0,
            "score": None,
            "items": [],
            "repair_en": "",
            "error": detail[:1000],
            "finish_reason": finish_reason,
            "usage": usage,
        }

    def _check_call(
        self,
        job: dict,
        upload_path: Path,
        result_path: Path,
        prompt: str,
        attempt: int,
        short: bool,
    ) -> dict:
        if SMOKE_MODE:
            return self._smoke_check_response(
                attempt, str(job.get("smoke_check") or "high"), short
            )
        upload_url = self._check_image_data_url(upload_path)
        result_url = self._check_image_data_url(result_path)
        key, base_url = self._read_credentials()
        payload = {
            "model": REMODEL_VISION_MODEL,
            "temperature": 0.1,
            "max_tokens": SELF_CHECK_MAX_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": upload_url}},
                        {"type": "image_url", "image_url": {"url": result_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        response = self._chat_completion(
            base_url, key, payload, timeout=REMODEL_SELF_CHECK_TIMEOUT
        )
        content, finish_reason = completion_content(response)
        usage = response.get("usage")
        return {
            "content": content,
            "finish_reason": finish_reason,
            "usage": usage if isinstance(usage, dict) else {},
        }

    def _parse_check_response(self, response: dict) -> dict:
        if str(response.get("finish_reason") or "") == "length":
            raise RemodelCheckResponseError("结构自检响应被截断")
        try:
            obj = parse_model_json(str(response.get("content") or ""))
        except Exception as exc:
            raise RemodelCheckResponseError(
                f"结构自检 JSON 解析失败：{exc}"
            ) from None
        return self._normalize_check(obj)

    def _self_check(
        self,
        job: dict,
        upload_path: Path,
        result_path: Path,
        attempt: int,
    ) -> dict:
        analysis = {
            "geometry": dict(job.get("geometry") or {}),
            "camera": dict(job.get("camera") or {}),
            "openings": list(job.get("openings") or []),
            "framing": dict(job.get("framing") or {}),
            "framing_valid": job.get("framing_valid"),
            "framing_note": str(job.get("framing_note") or ""),
            "fixed_features": list(job.get("fixed_features") or []),
            "floor_en": str(job.get("floor_en") or ""),
            "room_function": str(job.get("room_function") or ""),
        }
        diagnostics: list[dict] = []
        first: dict = {}
        first_error: BaseException | None = None
        stage_job_id = str(job.get("id") or "")
        self._touch_remodel_stage(stage_job_id, "checking")
        try:
            first = self._check_call(
                job,
                upload_path,
                result_path,
                self._self_check_prompt(analysis),
                attempt,
                short=False,
            )
            check = self._parse_check_response(first)
            short_form = False
        except Exception as exc:
            diagnostics.append(self._check_failure_entry(exc, first))
            if not isinstance(exc, RemodelCheckResponseError):
                raise RemodelCheckError(
                    "结构自检失败：" + clean_error(exc), diagnostics
                ) from None
            second: dict = {}
            self._touch_remodel_stage(stage_job_id, "checking")
            try:
                second = self._check_call(
                    job,
                    upload_path,
                    result_path,
                    self._self_check_prompt(analysis, short=True),
                    attempt,
                    short=True,
                )
                check = self._parse_check_response(second)
                short_form = True
            except Exception as second_exc:
                diagnostics.append(self._check_failure_entry(second_exc, second))
                raise RemodelCheckError(
                    "结构自检失败：" + clean_error(second_exc), diagnostics
                ) from None
        check["_diagnostics"] = diagnostics
        check["short_form"] = short_form
        return check

    @staticmethod
    def _smoke_check_response(attempt: int, mode: str, short: bool) -> dict:
        if mode == "error":
            raise RuntimeError("smoke self-check error")
        if mode == "truncated" and not short:
            return {
                "content": '{"score": 0.4, "items": [',
                "finish_reason": "length",
                "usage": {
                    "prompt_tokens": 1800,
                    "completion_tokens": SELF_CHECK_MAX_TOKENS,
                    "total_tokens": 1800 + SELF_CHECK_MAX_TOKENS,
                },
            }
        effective = "low" if mode == "truncated" else mode
        payload = GalleryStore._smoke_check_payload(attempt, effective)
        return {
            "content": json.dumps(payload, ensure_ascii=False),
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 1800, "completion_tokens": 180, "total_tokens": 1980},
        }

    @staticmethod
    def _fake_remodel_image(size: str) -> bytes:
        try:
            width, height = (int(part) for part in str(size).split("*", 1))
        except (TypeError, ValueError):
            width, height = 1080, 1440
        if not PIL_AVAILABLE:
            return b""
        with Image.new("RGB", (width, height), (172, 156, 132)) as image:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    @staticmethod
    def _save_remodel_png(
        job_id: str,
        index: int,
        data: bytes,
        target_ratio: float | None = None,
        attempt: int = 1,
    ) -> dict:
        REMODEL_JOB_DIR.mkdir(parents=True, exist_ok=True)
        if attempt > 1:
            name = (
                f"{job_id}-r{attempt}.png"
                if index == 0
                else f"{job_id}-r{attempt}-{index + 1}.png"
            )
        else:
            name = f"{job_id}.png" if index == 0 else f"{job_id}-{index + 1}.png"
        target = REMODEL_JOB_DIR / name
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        width = height = None
        cropped = False
        try:
            if PIL_AVAILABLE and data:
                try:
                    with Image.open(io.BytesIO(data)) as image:
                        fitted = ImageOps.exif_transpose(image).convert("RGB")
                        if target_ratio is not None:
                            fitted, cropped = crop_image_to_aspect(fitted, target_ratio)
                        width, height = fitted.size
                        fitted.save(tmp, format="PNG", optimize=True)
                except Exception:
                    tmp.write_bytes(data)
            else:
                tmp.write_bytes(data)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return {
            "path": target,
            "width": width,
            "height": height,
            "cropped": cropped,
            "aspect": round(width / height, 6) if width and height else None,
        }

    def _download_remodel_image(
        self,
        job_id: str,
        index: int,
        image_url: str,
        target_ratio: float | None = None,
        attempt: int = 1,
    ) -> dict:
        def fetch() -> bytes:
            with urlopen(
                Request(image_url, headers={"User-Agent": "homai-gallery-gui/1.0"}),
                timeout=REMODEL_DOWNLOAD_TIMEOUT,
            ) as response:
                return response.read()

        self._set_remodel_stage(job_id, "downloading")
        try:
            data = call_with_deadline(
                fetch,
                REMODEL_DOWNLOAD_TIMEOUT,
                f"改造图下载超时（已等待 {int(REMODEL_DOWNLOAD_TIMEOUT)} 秒），稍后重试",
            )
        except RemodelTimeoutError:
            raise
        except HTTPError as exc:
            raise RuntimeError(f"改造图下载失败（HTTP {exc.code}）") from None
        except Exception:
            raise RuntimeError("改造图下载失败") from None
        if not data:
            raise RuntimeError("改造图内容为空")
        self._set_remodel_stage(job_id, "saving")
        return self._save_remodel_png(job_id, index, data, target_ratio, attempt)

    def _job_source_path(self, job: dict) -> Path | None:
        item_id = str(job.get("item_id") or "")
        if item_id:
            item = self._read_item(item_id)
            if item is None:
                return None
            return self._item_source_path(item, str(job.get("source_sid") or ""))
        return self._upload_path(str(job.get("upload_id") or ""))

    def _finalize_item_generation(
        self,
        job: dict,
        primary: dict,
        chosen_prompt: str,
        checks: list[dict],
        final_score,
        attempts: int,
        size_final: str | None,
        model_seconds: float,
        elapsed: float,
    ) -> str:
        """Copy the chosen attempt into gen/<gid>.png + .json exactly once."""
        item_id = str(job.get("item_id") or "")
        item_dir = self._item_dir(item_id)
        if item_dir is None:
            raise RuntimeError("项目不存在或已删除")
        source_path = Path(str(primary.get("path") or ""))
        if not source_path.is_file():
            raise RuntimeError("生成结果文件不存在")
        data = source_path.read_bytes()
        with self.item_lock:
            item = self._read_item(item_id)
            if item is None:
                raise RuntimeError("项目不存在或已删除")
            gen_dir = item_dir / "gen"
            gen_dir.mkdir(parents=True, exist_ok=True)
            gid = str(job.get("gen_id") or "")
            if not GEN_ID_RE.fullmatch(gid):
                gid = self._new_gen_id(item_id)
            png_path = gen_dir / f"{gid}.png"
            json_path = gen_dir / f"{gid}.json"
            for _ in range(16):
                if not png_path.exists() and not json_path.exists():
                    break
                gid = self._new_gen_id(item_id)
                png_path = gen_dir / f"{gid}.png"
                json_path = gen_dir / f"{gid}.json"
            else:
                raise RuntimeError("无法分配新的版本 ID")
            try:
                with png_path.open("xb") as fh:
                    fh.write(data)
            except FileExistsError:
                raise RuntimeError("生成版本文件已存在，拒绝覆盖") from None
            self._ensure_gen_thumb(item_id, gid, png_path)
            created_at = iso_now_ms()
            width = primary.get("width")
            height = primary.get("height")
            meta = {
                "gid": gid,
                "item_id": item_id,
                "created_at": created_at,
                "mode": str(job.get("mode") or "text"),
                "style": str(job.get("style") or ""),
                "model": str(job.get("model") or ""),
                "prompt": str(chosen_prompt or ""),
                "negative": str(job.get("negative") or REMODEL_NEGATIVE),
                "score": final_score,
                "attempts": attempts,
                "size_requested": str(
                    job.get("size_requested") or job.get("size") or ""
                ),
                "size_final": str(size_final or ""),
                "width": width,
                "height": height,
                "model_seconds": round(float(model_seconds or 0), 1),
                "total_seconds": round(float(elapsed or 0), 1),
                "checks": checks,
                "source_sid": str(job.get("source_sid") or ""),
                "job_id": str(job.get("id") or ""),
                "note": str(job.get("note") or ""),
                "detail_enabled": bool(job.get("detail_enabled")),
                "detail_zh": str(job.get("detail_zh") or ""),
                "extra_zh": str(job.get("extra_zh") or ""),
                "extra_en": str(job.get("extra_en") or ""),
                "extra_verbatim": bool(job.get("extra_verbatim")),
                "refine_of": str(job.get("refine_of") or ""),
                "room_function": str(job.get("room_function") or ""),
                "room_function_source": str(job.get("room_function_source") or ""),
            }
            atomic_write_json(json_path, meta)
            updated = dict(item)
            updated.setdefault("renamed_by_user", False)
            updated["gens"] = [gid] + [
                value for value in (item.get("gens") or []) if value != gid
            ]
            if not updated.get("renamed_by_user"):
                room_zh = room_type_zh(item.get("structure"))
                current_name = str(item.get("name") or "")
                if is_default_item_name(
                    current_name, room_zh, self.style_packs.keys()
                ):
                    new_name = default_item_name(
                        item_id, room_zh, str(job.get("style") or "")
                    )
                    if new_name != current_name:
                        updated["name"] = new_name
            updated["updated_at"] = created_at
            self._write_item(updated)
        with self.lock:
            live = self.remodel_jobs.get(str(job.get("id") or ""))
            if live is not None:
                live["gen_id"] = gid
        return gid

    def _analyze_job_source(self, job: dict) -> dict:
        """Analyse the item source for a text job that was submitted without it."""
        item_id = str(job.get("item_id") or "")
        job_id = str(job.get("id") or "")
        if not item_id:
            raise RuntimeError("分析原图失败：任务缺少项目")
        expected_sid = str(job.get("source_sid") or "")
        current = self._read_item(item_id)
        if current is None:
            raise RuntimeError("分析原图失败：项目不存在或已删除")
        if expected_sid and str(current.get("source_current") or "") != expected_sid:
            raise RuntimeError("分析原图失败：原图已更换，请重新提交生成")
        try:
            item = self.analyze_item(item_id, force=True)
        except Exception as exc:
            raise RuntimeError("分析原图失败：" + clean_error(exc)) from None
        if item is None:
            raise RuntimeError("分析原图失败：项目不存在或已删除")
        if expected_sid and str(item.get("source_current") or "") != expected_sid:
            raise RuntimeError("分析原图失败：原图已更换，请重新提交生成")
        analysis = (
            item.get("structure")
            if isinstance(item.get("structure"), dict)
            else None
        )
        structure = str((analysis or {}).get("structure_en") or "").strip()
        if not structure:
            raise RuntimeError("分析原图失败：未返回结构描述")
        values = self._analysis_job_values(analysis, structure)
        with self.lock:
            live = self.remodel_jobs.get(job_id)
            if live is None:
                raise RuntimeError("改造任务不存在")
            if live.get("status") == "error":
                return {}
            live.update(values)
            live["analysis_ready"] = True
        return values

    def _run_remodel_job(self, job_id: str) -> None:
        with self.lock:
            job = dict(self.remodel_jobs.get(job_id) or {})
        if not job:
            raise RuntimeError("改造任务不存在")
        if job.get("analyze_source") and not str(
            job.get("structure_en") or ""
        ).strip():
            self._set_remodel_stage(job_id, "analyzing")
            values = self._analyze_job_source(job)
            if not values:
                return
            job.update(values)
        # Room function is resolved before the prompt is built. A supplementary
        # requirement that is only a room name is explicit user intent and
        # outranks the structure analysis, which outranks the project name,
        # which outranks one short vision call. The result is cached on
        # item.json so it is not repeated.
        extra_zh = str(job.get("extra_zh") or "").strip()
        extra_room = _extra_room_function_override(extra_zh)
        item = None
        if job.get("item_id"):
            item = self._read_item(str(job.get("item_id") or ""))
        if extra_room:
            room_function = extra_room
            room_function_source = "extra"
            if item is not None:
                try:
                    self._cache_room_function(item, extra_room, "extra")
                except Exception:
                    pass
        elif item is not None:
            room_function = self._detect_room_function(item)
            room_function_source = str(item.get("room_function_source") or "")
        else:
            room_function = "unknown"
            room_function_source = ""
        job["room_function"] = room_function
        job["room_function_source"] = room_function_source
        with self.lock:
            live = self.remodel_jobs.get(job_id)
            if live is not None:
                live["room_function"] = room_function
                live["room_function_source"] = room_function_source
        style_zh = str(job.get("style") or "")
        pack = self.style_packs.get(style_zh)
        if not pack:
            raise RuntimeError("风格包不存在")
        if extra_room:
            label_en = REMODEL_ROOM_FUNCTION_LABELS.get(extra_room, "room")
            extra_prompt_en = f"Additional requirement: {label_en}."
            extra_en = label_en
            extra_verbatim = False
        else:
            extra_en, extra_verbatim = self._translate_extra_to_english(extra_zh)
            if extra_verbatim:
                extra_prompt_en = EXTRA_VERBATIM_PREFIX + extra_zh
                extra_en = ""
            else:
                extra_prompt_en = extra_en
        job["extra_en"] = extra_en
        job["extra_verbatim"] = extra_verbatim
        with self.lock:
            live = self.remodel_jobs.get(job_id)
            if live is not None:
                live["extra_zh"] = extra_zh
                live["extra_en"] = extra_en
                live["extra_verbatim"] = extra_verbatim
        detail_enabled = bool(job.get("detail_enabled"))
        detail_zh = str(job.get("detail_zh") or "")
        detail_en = ""
        if detail_enabled and detail_zh:
            detail_en, _detail_verbatim = self._translate_extra_to_english(
                detail_zh, allow_verbatim=False
            )
        analysis_locks = {
            "geometry": dict(job.get("geometry") or {}),
            "camera": dict(job.get("camera") or {}),
            "framing": dict(job.get("framing") or {}),
            "framing_valid": job.get("framing_valid"),
            "framing_note": str(job.get("framing_note") or ""),
            "fixed_features": list(job.get("fixed_features") or []),
            "openings": list(job.get("openings") or []),
            "openings_total": job.get("openings_total"),
        }
        negative = str(job.get("negative") or REMODEL_NEGATIVE)
        upload_path = self._job_source_path(job)
        if upload_path is None:
            raise RuntimeError("项目原图不存在")
        try:
            upload_ratio = float(job.get("width")) / float(job.get("height"))
        except (TypeError, ValueError, ZeroDivisionError):
            upload_ratio = None
        variants = int(job.get("variants") or 1)
        mode = str(job.get("mode") or "text")
        if mode not in REMODEL_MODES:
            raise RuntimeError("未知的生成模式")
        request_timeout, retry_network = remodel_image_request_options(mode)
        input_data_url = (
            str(job.get("input_data_url") or "") if mode == "img2img" else None
        )
        if mode == "img2img" and not input_data_url:
            raise RuntimeError("img2img 缺少输入原图")
        model_seconds = 0.0

        def generate(attempt: int, repair_en: str) -> tuple[list[dict], str]:
            nonlocal model_seconds
            if str(job.get("base") or "source") == "current":
                # Refine pass: never use the remodeling prompt, which asks for
                # all furniture to be replaced.
                prompt = self._build_refine_prompt(
                    pack, detail_en, str(job.get("size") or "")
                )
                if repair_en:
                    if not repair_en.lower().startswith("hard corrections"):
                        repair_en = (
                            "Hard corrections, these override other instructions: "
                            + repair_en
                        )
                    prompt = prompt + "\n\n" + repair_en
            else:
                prompt = self._build_remodel_prompt(
                    pack,
                    str(job.get("structure_en") or ""),
                    extra_prompt_en,
                    str(job.get("size") or ""),
                    analysis=analysis_locks,
                    strict=bool(job.get("strict", True)) if mode == "text" else False,
                    repair_en=repair_en,
                    mode=mode,
                    detail_enabled=detail_enabled,
                    detail_en=detail_en,
                    room_function=room_function,
                    extra_verbatim=extra_verbatim,
                )
            full_prompt = prompt + "\n\nAvoid: " + negative
            if _prompt_has_disallowed_cjk(full_prompt, extra_verbatim):
                raise RuntimeError("生成提示词包含中文，已阻止发送到图片模型")
            with self.lock:
                if job_id in self.remodel_jobs:
                    self.remodel_jobs[job_id]["prompt"] = full_prompt
            request_stage = "repairing" if repair_en else "requesting"
            saved: list[dict] = []
            if SMOKE_MODE:
                # Build the real body so smoke tests can prove the part counts
                # without a paid call. The fake image still goes through the crop.
                payload = self._build_image_payload(
                    full_prompt,
                    str(job.get("model") or ""),
                    str(job.get("size") or ""),
                    input_data_url,
                )
                content = payload["messages"][0]["content"]
                image_parts = [part for part in content if "image" in part]
                text_parts = [part for part in content if "text" in part]
                self._set_remodel_stage(job_id, request_stage)
                if job.get("smoke_check") == "timeout":
                    # Smoke-only: simulate the single gateway call timing out.
                    with self.lock:
                        live = self.remodel_jobs.get(job_id)
                        if live is not None:
                            live.setdefault("request_parts", []).append(
                                {
                                    "attempt": attempt,
                                    "index": 0,
                                    "image_parts": len(image_parts),
                                    "text_parts": len(text_parts),
                                    "image_prefix": (
                                        str(image_parts[0].get("image") or "")[:23]
                                        if image_parts
                                        else ""
                                    ),
                                    "timeout": request_timeout,
                                    "retry_network": retry_network,
                                }
                            )
                            live["model_seconds"] = float(request_timeout)
                    model_seconds = float(request_timeout)
                    raise RemodelTimeoutError(
                        remodel_timeout_message(request_timeout, mode)
                    )
                fake = self._fake_remodel_image(str(job.get("size") or "1080*1440"))
                self._set_remodel_stage(job_id, "downloading")
                for index in range(variants):
                    with self.lock:
                        live = self.remodel_jobs.get(job_id)
                        if live is not None:
                            live.setdefault("request_parts", []).append(
                                {
                                    "attempt": attempt,
                                    "index": index,
                                    "image_parts": len(image_parts),
                                    "text_parts": len(text_parts),
                                    "image_prefix": (
                                        str(image_parts[0].get("image") or "")[:23]
                                        if image_parts
                                        else ""
                                    ),
                                    "timeout": request_timeout,
                                    "retry_network": retry_network,
                                }
                            )
                    self._set_remodel_stage(job_id, "saving")
                    saved.append(
                        self._save_remodel_png(
                            job_id, index, fake, upload_ratio, attempt
                        )
                    )
            else:
                key, base_url = self._read_credentials()
                for index in range(variants):
                    self._set_remodel_stage(job_id, request_stage)
                    call_started = time.monotonic()
                    try:
                        image_url = self._request_image(
                            base_url,
                            key,
                            full_prompt,
                            str(job.get("model") or ""),
                            str(job.get("size") or ""),
                            image_data_url=input_data_url,
                            timeout=request_timeout,
                            retry_network=retry_network,
                            timeout_mode=mode,
                        )
                    finally:
                        model_seconds += time.monotonic() - call_started
                        with self.lock:
                            live = self.remodel_jobs.get(job_id)
                            if live is not None:
                                live["model_seconds"] = model_seconds
                    saved.append(
                        self._download_remodel_image(
                            job_id, index, image_url, upload_ratio, attempt
                        )
                    )
            return saved, full_prompt

        started = time.monotonic()
        attempts = 0
        checks: list[dict] = []
        final_score = None
        final_attempt = None
        check_error = None
        chosen_saved: list[dict] = []
        chosen_prompt = ""
        all_saved: list[dict] = []
        repair_en = ""
        previous_score = None
        attempt = 1
        while attempt <= REMODEL_MAX_ATTEMPTS:
            attempts = attempt
            saved, full_prompt = generate(attempt, repair_en)
            all_saved.extend(saved)
            if attempt == 1:
                chosen_saved = saved
                chosen_prompt = full_prompt
                final_attempt = 1
            self._set_remodel_stage(job_id, "checking")
            try:
                check = self._self_check(
                    job, upload_path, saved[0]["path"], attempt
                )
            except Exception as exc:
                # A failed self-check never gates the image.
                check_error = clean_error(exc)
                chosen_saved = saved
                chosen_prompt = full_prompt
                final_attempt = attempt
                if isinstance(exc, RemodelCheckError):
                    checks = list(exc.checks)
                else:
                    checks = [self._check_failure_entry(exc)]
                final_score = None
                break
            diagnostics = check.pop("_diagnostics", [])
            checks.extend(diagnostics)
            check["attempt"] = attempt
            checks.append(check)
            if attempt == 1:
                final_score = check["score"]
                final_attempt = 1
            elif previous_score is not None and check["score"] >= previous_score:
                chosen_saved = saved
                chosen_prompt = full_prompt
                final_score = check["score"]
                final_attempt = attempt
            previous_score = check["score"]
            if check["score"] >= 0.85:
                break
            if attempt >= REMODEL_MAX_ATTEMPTS:
                break
            repair_en = check.get("repair_en") or (
                "Restore every failed structural detail exactly as in the source photo."
            )
            attempt += 1

        chosen_paths = {entry["path"] for entry in chosen_saved}
        for entry in all_saved:
            if entry["path"] in chosen_paths:
                continue
            try:
                entry["path"].unlink(missing_ok=True)
            except OSError:
                pass

        elapsed = time.monotonic() - started
        images = []
        for entry in chosen_saved:
            target = entry["path"]
            try:
                rel = target.relative_to(ROOT).as_posix()
            except ValueError:
                # HOMAI_REMODEL_DIR may point outside the project (tests).
                rel = "remodel/jobs/" + target.name
            images.append(
                {
                    "file": rel,
                    "url": "/remodel/jobs/" + quote(target.name, safe=""),
                    "width": entry.get("width"),
                    "height": entry.get("height"),
                    "aspect": entry.get("aspect"),
                    "cropped": bool(entry.get("cropped")),
                }
            )
        primary = images[0] if images else None
        primary_width = primary.get("width") if primary else None
        primary_height = primary.get("height") if primary else None
        size_final = (
            f"{primary_width}*{primary_height}"
            if primary_width and primary_height
            else None
        )
        aspect_final = (
            round(primary_width / primary_height, 6)
            if primary_width and primary_height
            else None
        )
        aspect_ok = bool(
            aspect_final is not None
            and upload_ratio is not None
            and abs(aspect_final - upload_ratio) <= 0.004
        )
        item_id = str(job.get("item_id") or "")
        if item_id:
            with self.lock:
                live = self.remodel_jobs.get(job_id)
                if live is None or live.get("status") == "error":
                    # Deleted or reaped while the model was running: never
                    # resurrect it and never write into a moved item tree.
                    return
            if primary is None or not chosen_saved:
                raise RuntimeError("没有可保存的生成结果")
            self._finalize_item_generation(
                job,
                chosen_saved[0],
                chosen_prompt,
                checks,
                final_score,
                attempts,
                size_final,
                model_seconds,
                elapsed,
            )
        with self.lock:
            live = self.remodel_jobs.get(job_id)
            if not live:
                return
            if live.get("status") == "error":
                # The reaper already failed this job; the abandoned worker
                # must not resurrect it.
                return
            live["status"] = "done"
            live["finished_mono"] = time.monotonic()
            live["seconds"] = elapsed
            live["model_seconds"] = model_seconds
            live.pop("input_data_url", None)
            live["files"] = [entry["file"] for entry in images]
            live["images"] = images
            live["image_url"] = primary["url"] if primary else None
            live["file"] = primary["file"] if primary else None
            live["size_final"] = size_final
            live["width_final"] = primary_width
            live["height_final"] = primary_height
            live["aspect_final"] = aspect_final
            live["aspect_exact"] = aspect_ok
            live["attempts"] = attempts
            live["checks"] = checks
            live["final_score"] = final_score
            live["final_attempt"] = final_attempt
            live["check_error"] = check_error
            live["prompt"] = chosen_prompt
        self._set_remodel_stage(job_id, "done", persist=False)
        self._write_remodel_job_json(job_id)

    def recheck_remodel(self, job_id: str) -> dict | None:
        """Re-run the full self-check on a finished job.

        The full check is allowed to return ``repair_en``; this endpoint only
        re-scores the existing image and never starts a paid generation.
        """
        with self.lock:
            job = dict(self.remodel_jobs.get(job_id) or {})
        if not job:
            return None
        if job.get("status") != "done":
            raise ValueError("只有已完成的改造任务可以复检")
        upload_path = self._job_source_path(job)
        if upload_path is None:
            raise RuntimeError("项目原图不存在")
        images = job.get("images") if isinstance(job.get("images"), list) else []
        primary = images[0] if images and isinstance(images[0], dict) else {}
        result_name = Path(str(primary.get("file") or job.get("file") or "")).name
        result_path = REMODEL_JOB_DIR / result_name if result_name else None
        if result_path is None or not result_path.is_file():
            raise RuntimeError("改造结果图片不存在")
        attempt = job.get("final_attempt") or job.get("attempts") or 1
        try:
            attempt = int(attempt)
        except (TypeError, ValueError):
            attempt = 1
        try:
            check = self._self_check(job, upload_path, result_path, attempt)
        except Exception as exc:
            entries = (
                list(exc.checks)
                if isinstance(exc, RemodelCheckError)
                else [self._check_failure_entry(exc)]
            )
            with self.lock:
                live = self.remodel_jobs.get(job_id)
                if not live:
                    return None
                live["checks"] = list(live.get("checks") or []) + entries
                live["check_error"] = clean_error(exc)
            self._write_remodel_job_json(job_id)
            return self.remodel_job_snapshot(job_id)
        diagnostics = check.pop("_diagnostics", [])
        check["attempt"] = attempt
        with self.lock:
            live = self.remodel_jobs.get(job_id)
            if not live:
                return None
            existing = [
                entry
                for entry in (live.get("checks") or [])
                if not (
                    entry.get("attempt") == attempt
                    and entry.get("score") is not None
                )
            ]
            live["checks"] = existing + diagnostics + [check]
            live["final_score"] = check.get("score")
            live["final_attempt"] = attempt
            live["check_error"] = None
        self._write_remodel_job_json(job_id)
        return self.remodel_job_snapshot(job_id)

    def _write_remodel_job_json(self, job_id: str) -> None:
        with self.lock:
            job = self.remodel_jobs.get(job_id)
            if not job:
                return
            job = dict(job)
            files = list(job.get("files") or [])
            images = [dict(entry) for entry in (job.get("images") or [])]
            stage_seconds = float(job.get("stage_seconds") or 0)
            if (
                job.get("status") in {"queued", "running"}
                and job.get("stage_started_mono") is not None
            ):
                stage_seconds = time.monotonic() - float(job["stage_started_mono"])
            stage_log = [
                dict(entry)
                for entry in (job.get("stage_log") or [])
                if isinstance(entry, dict)
            ]
        payload = {
            "job_id": job_id,
            "item_id": job.get("item_id"),
            "gen_id": job.get("gen_id"),
            "source_sid": job.get("source_sid"),
            "note": job.get("note") or "",
            "detail_enabled": bool(job.get("detail_enabled")),
            "detail_zh": str(job.get("detail_zh") or ""),
            "extra_zh": str(job.get("extra_zh") or ""),
            "extra_en": str(job.get("extra_en") or ""),
            "extra_verbatim": bool(job.get("extra_verbatim")),
            "base": str(job.get("base") or "source"),
            "refine_of": str(job.get("refine_of") or ""),
            "upload_id": job.get("upload_id"),
            "style": job.get("style"),
            "style_en": job.get("style_en"),
            "sizes": {
                "value": job.get("size"),
                "requested": job.get("size_requested") or job.get("size"),
                "final": job.get("size_final"),
                "width": job.get("width"),
                "height": job.get("height"),
                "final_width": job.get("width_final"),
                "final_height": job.get("height_final"),
            },
            "size": job.get("size"),
            "size_requested": job.get("size_requested") or job.get("size"),
            "size_final": job.get("size_final"),
            "width": job.get("width"),
            "height": job.get("height"),
            "width_final": job.get("width_final"),
            "height_final": job.get("height_final"),
            "aspect": job.get("aspect"),
            "aspect_final": job.get("aspect_final"),
            "aspect_label": job.get("aspect_label"),
            "aspect_exact": job.get("aspect_exact"),
            "aspect_note": job.get("aspect_note"),
            "mode": job.get("mode") or "text",
            "strict": job.get("strict"),
            "input_long_edge": job.get("input_long_edge"),
            "input_bytes": job.get("input_bytes"),
            "model_seconds": round(float(job.get("model_seconds") or 0), 1),
            "prompt": job.get("prompt") or "",
            "negative": job.get("negative") or "",
            "model": job.get("model"),
            "seconds": round(float(job.get("seconds") or 0), 1),
            "created_at": job.get("created_at"),
            "created_ts": job.get("created_ts"),
            "status": job.get("status"),
            "stage": job.get("stage") or "queued",
            "stage_started_at": job.get("stage_started_at"),
            "stage_seconds": round(stage_seconds, 1),
            "heartbeat_at": job.get("heartbeat_at"),
            "stage_log": stage_log,
            "error": job.get("error"),
            "file": job.get("file"),
            "files": files,
            "images": images,
            "result_url": job.get("image_url")
            if job.get("status") == "done"
            else None,
            "preview_url": job.get("preview_url"),
            "variants": job.get("variants"),
            "structure_en": job.get("structure_en"),
            "geometry": dict(job.get("geometry") or {}),
            "camera": dict(job.get("camera") or {}),
            "framing": dict(job.get("framing") or {}),
            "framing_valid": bool(job.get("framing_valid")),
            "framing_note": str(job.get("framing_note") or ""),
            "fixed_features": [
                dict(entry) for entry in (job.get("fixed_features") or [])
            ],
            "floor_en": str(job.get("floor_en") or ""),
            "openings": [dict(entry) for entry in (job.get("openings") or [])],
            "openings_total": job.get("openings_total"),
            "lighting_plan_en": str(job.get("lighting_plan_en") or ""),
            "layering_plan_en": str(job.get("layering_plan_en") or ""),
            "lighting_summary": dict(job.get("lighting_summary") or {}),
            "decor_summary": dict(job.get("decor_summary") or {}),
            "daylight_direction_en": str(job.get("daylight_direction_en") or ""),
            "room_function": str(job.get("room_function") or ""),
            "room_function_source": str(job.get("room_function_source") or ""),
            "attempts": job.get("attempts") or 0,
            "checks": [
                {
                    "attempt": entry.get("attempt"),
                    "score": entry.get("score"),
                    "items": [dict(item) for item in (entry.get("items") or [])],
                    "repair_en": entry.get("repair_en") or "",
                    "error": str(entry.get("error") or ""),
                    "finish_reason": str(entry.get("finish_reason") or ""),
                    "usage": dict(entry.get("usage") or {})
                    if isinstance(entry.get("usage"), dict)
                    else {},
                    "short_form": bool(entry.get("short_form")),
                }
                for entry in (job.get("checks") or [])
            ],
            "final_score": job.get("final_score"),
            "final_attempt": job.get("final_attempt"),
            "check_error": job.get("check_error"),
            "immovable_zh": list(job.get("immovable_zh") or []),
            "movable_zh": list(job.get("movable_zh") or []),
            "smoke": bool(job.get("smoke")),
        }
        if job.get("smoke"):
            # Smoke-only observability: proves the request part contract per
            # attempt without sending a paid call.
            payload["request_parts"] = [
                dict(entry) for entry in (job.get("request_parts") or [])
            ]
        atomic_write_json(REMODEL_JOB_DIR / f"{job_id}.json", payload)

    def remodel_history(self, limit: int = 30) -> list[dict]:
        limit = max(1, min(100, int(limit)))
        entries: list[dict] = []
        if not REMODEL_JOB_DIR.is_dir():
            return entries
        for path in REMODEL_JOB_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            status = str(data.get("status") or "done")
            if status not in {"queued", "running", "done", "error"}:
                continue
            result_name = ""
            if status == "done":
                result_name = Path(str(data.get("file") or "")).name
                if not result_name:
                    result_name = str(data.get("job_id") or path.stem) + ".png"
                result_path = REMODEL_JOB_DIR / result_name
                if (
                    not result_path.is_file()
                    or result_path.suffix.lower() not in REMODEL_IMAGE_EXTENSIONS
                ):
                    continue
            upload_id = str(data.get("upload_id") or "")
            item_id = str(data.get("item_id") or "")
            gen_id = str(data.get("gen_id") or "")
            preview_url = str(data.get("preview_url") or "")
            if not preview_url and upload_id:
                upload_path = self._upload_path(upload_id)
                if upload_path is not None:
                    preview_url = self._preview_url(upload_path.name)
            history_thumb_url = preview_url
            if item_id and gen_id:
                item_dir = self._item_dir(item_id)
                png_path = (
                    item_dir / "gen" / f"{gen_id}.png"
                    if item_dir is not None and GEN_ID_RE.fullmatch(gen_id)
                    else None
                )
                if png_path is not None and png_path.is_file():
                    self._ensure_gen_thumb(item_id, gen_id, png_path)
                    history_thumb_url = self._item_gen_thumb_url(item_id, gen_id)
                else:
                    item = self._read_item(item_id)
                    source_path = (
                        self._item_source_path(item) if item is not None else None
                    )
                    if item is not None and source_path is not None:
                        self._ensure_source_thumb(item, source_path=source_path)
                        sid = str(item.get("source_current") or "")
                        if ITEM_ID_RE.fullmatch(sid):
                            history_thumb_url = self._item_source_thumb_url(
                                item_id, sid
                            )
            elif item_id:
                item = self._read_item(item_id)
                source_path = self._item_source_path(item) if item is not None else None
                if item is not None and source_path is not None:
                    self._ensure_source_thumb(item, source_path=source_path)
                    sid = str(item.get("source_current") or "")
                    if ITEM_ID_RE.fullmatch(sid):
                        history_thumb_url = self._item_source_thumb_url(item_id, sid)
            try:
                created_ts = float(data.get("created_ts") or 0)
            except (TypeError, ValueError):
                created_ts = 0.0
            if not created_ts:
                created_ts = path.stat().st_mtime
            entries.append(
                {
                    "job_id": str(data.get("job_id") or path.stem),
                    "item_id": data.get("item_id"),
                    "gen_id": data.get("gen_id"),
                    "status": status,
                    "stage": str(data.get("stage") or status),
                    "error": str(data.get("error") or ""),
                    "thumb_url": history_thumb_url,
                    "result_url": (
                        str(
                            data.get("result_url")
                            or "/remodel/jobs/" + quote(result_name, safe="")
                        )
                        if status == "done" and result_name
                        else None
                    ),
                    "style": str(data.get("style") or ""),
                    "style_en": str(data.get("style_en") or ""),
                    "created_at": str(
                        data.get("created_at") or iso_from_timestamp(path.stat().st_mtime)
                    ),
                    "seconds": float(data.get("seconds") or 0),
                    "size": str(data.get("size") or ""),
                    "size_requested": str(
                        data.get("size_requested") or data.get("size") or ""
                    ),
                    "size_final": str(data.get("size_final") or ""),
                    "width": data.get("width"),
                    "height": data.get("height"),
                    "width_final": data.get("width_final"),
                    "height_final": data.get("height_final"),
                    "aspect": data.get("aspect"),
                    "aspect_final": data.get("aspect_final"),
                    "aspect_label": str(data.get("aspect_label") or ""),
                    "aspect_exact": data.get("aspect_exact"),
                    "mode": str(data.get("mode") or "text"),
                    "strict": data.get("strict"),
                    "input_long_edge": data.get("input_long_edge"),
                    "input_bytes": data.get("input_bytes"),
                    "model_seconds": round(float(data.get("model_seconds") or 0), 1),
                    "prompt": str(data.get("prompt") or ""),
                    "negative": str(data.get("negative") or ""),
                    "model": str(data.get("model") or ""),
                    "upload_id": upload_id,
                    "preview_url": preview_url,
                    "structure_en": str(data.get("structure_en") or ""),
                    "framing": dict(data.get("framing") or {})
                    if isinstance(data.get("framing"), dict)
                    else {},
                    "framing_valid": bool(data.get("framing_valid")),
                    "framing_note": str(data.get("framing_note") or ""),
                    "fixed_features": [
                        dict(entry)
                        for entry in (data.get("fixed_features") or [])
                        if isinstance(entry, dict)
                    ],
                    "floor_en": str(data.get("floor_en") or ""),
                    "openings": [
                        dict(entry)
                        for entry in (data.get("openings") or [])
                        if isinstance(entry, dict)
                    ],
                    "openings_total": data.get("openings_total"),
                    "lighting_plan_en": str(data.get("lighting_plan_en") or ""),
                    "layering_plan_en": str(data.get("layering_plan_en") or ""),
                    "lighting_summary": dict(data.get("lighting_summary") or {})
                    if isinstance(data.get("lighting_summary"), dict)
                    else {},
                    "decor_summary": dict(data.get("decor_summary") or {})
                    if isinstance(data.get("decor_summary"), dict)
                    else {},
                    "daylight_direction_en": str(
                        data.get("daylight_direction_en") or ""
                    ),
                    "attempts": data.get("attempts") or 0,
                    "checks": data.get("checks")
                    if isinstance(data.get("checks"), list)
                    else [],
                    "final_score": data.get("final_score"),
                    "final_attempt": data.get("final_attempt"),
                    "immovable_zh": list(data.get("immovable_zh") or []),
                    "movable_zh": list(data.get("movable_zh") or []),
                    "_sort": (created_ts, path.stat().st_mtime),
                }
            )
        entries.sort(key=lambda entry: entry["_sort"], reverse=True)
        for entry in entries:
            entry.pop("_sort", None)
        return entries[:limit]

    # ---------- thumbnails ----------

    def ensure_thumbnail(self, sid: str) -> tuple[Path | None, Path | None]:
        """Return (thumbnail, source). thumbnail is None when Pillow is absent."""
        meta = self.meta_by_id.get(sid)
        if not meta:
            return None, None
        local_path = str(meta.get("local_path") or "")
        source = self.resolve_media(local_path)
        if source is None or not source.is_file():
            return None, None
        if not PIL_AVAILABLE:
            return None, source
        thumb = THUMB_DIR / f"{sid}.jpg"
        lock = self.thumb_locks[sid]
        with lock:
            try:
                if thumb.is_file() and thumb.stat().st_size > 0 and thumb.stat().st_mtime >= source.stat().st_mtime:
                    return thumb, source
                with Image.open(source) as opened:
                    image = ImageOps.exif_transpose(opened)
                    image = image.convert("RGB")
                    image.thumbnail((480, 10000), Image.Resampling.LANCZOS)
                    THUMB_DIR.mkdir(parents=True, exist_ok=True)
                    tmp = thumb.with_name(f".{thumb.name}.{uuid.uuid4().hex}.tmp")
                    try:
                        image.save(tmp, format="JPEG", quality=84, optimize=True, progressive=True)
                        os.replace(tmp, thumb)
                    finally:
                        tmp.unlink(missing_ok=True)
                return thumb, source
            except Exception as exc:
                print(f"[gui] 缩略图生成失败 {sid}: {clean_error(exc)}", file=sys.stderr)
                return None, source

    def resolve_media(self, rel_path: str) -> Path | None:
        return resolve_under(IMAGES_DIR, rel_path, allow_root_prefix=ROOT)


def resolve_under(base: Path, rel_path: str, allow_root_prefix: Path | None = None) -> Path | None:
    if not rel_path:
        return None
    try:
        raw = unquote(str(rel_path)).replace("\x00", "")
        if raw.startswith("/"):
            candidate = Path(raw)
        elif allow_root_prefix is not None and raw.startswith("images/"):
            candidate = allow_root_prefix / raw
        else:
            candidate = base / raw
        resolved = candidate.resolve()
        resolved.relative_to(base.resolve())
    except (ValueError, OSError, RuntimeError):
        return None
    return resolved


def _copy_tree_missing(src: Path, dst: Path) -> None:
    """Copy every file under src into dst, preserving layout, never overwriting."""
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        target = dst / path.relative_to(src)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def seed_demo_data() -> None:
    """Import bundled demo data on first start.

    Existing runtime files always win, so repeated calls are idempotent and
    user-generated data is never overwritten.
    """
    if not DEMO_DIR.is_dir():
        return

    metadata_path = ROOT / "metadata.json"
    metadata_src = DEMO_DIR / "metadata.json"
    imported_metadata = False
    if not metadata_path.exists():
        if metadata_src.is_file():
            shutil.copy2(metadata_src, metadata_path)
            imported_metadata = True
        csv_src = DEMO_DIR / "metadata.csv"
        csv_path = ROOT / "metadata.csv"
        if csv_src.is_file() and not csv_path.exists():
            shutil.copy2(csv_src, csv_path)

    ai_dir = IMAGES_DIR / "ai"
    imported_images = 0
    if not (ai_dir.is_dir() and any(ai_dir.rglob("*.webp"))):
        demo_images = DEMO_DIR / "images"
        if demo_images.is_dir():
            _copy_tree_missing(demo_images, IMAGES_DIR)
            imported_images = sum(1 for _ in demo_images.rglob("*.webp"))

    # Demo data is always copied into the repository-relative remodel dir, not
    # HOMAI_REMODEL_DIR: an isolated runtime dir (e.g. smoke tests) must stay empty.
    items_dir = ROOT / "remodel" / "items"
    imported_items = 0
    if not (items_dir.is_dir() and any(p.is_dir() for p in items_dir.iterdir())):
        demo_remodel = DEMO_DIR / "remodel"
        if demo_remodel.is_dir():
            demo_items = demo_remodel / "items"
            if demo_items.is_dir():
                imported_items = sum(1 for p in demo_items.iterdir() if p.is_dir())
            _copy_tree_missing(demo_remodel, ROOT / "remodel")

    style_items_dir = STYLE_DIR / "items"
    imported_style_items = 0
    if not (style_items_dir.is_dir() and any(style_items_dir.glob("*.json"))):
        demo_style_items = DEMO_DIR / "style" / "items"
        if demo_style_items.is_dir():
            imported_style_items = sum(1 for _ in demo_style_items.glob("*.json"))
            _copy_tree_missing(demo_style_items, style_items_dir)

    style_palette_dir = STYLE_DIR / "palette"
    imported_palette = 0
    if not (style_palette_dir.is_dir() and any(style_palette_dir.glob("*.json"))):
        demo_palette = DEMO_DIR / "style" / "palette"
        if demo_palette.is_dir():
            imported_palette = sum(1 for _ in demo_palette.glob("*.json"))
            _copy_tree_missing(demo_palette, style_palette_dir)

    if imported_metadata:
        try:
            imported_images = len(json.loads(metadata_src.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass

    if (
        imported_metadata
        or imported_images
        or imported_items
        or imported_style_items
        or imported_palette
    ):
        print(
            f"已导入示例数据 {imported_images} 张图库 / {imported_items} 个改造项目"
            f" / {imported_style_items} 条风格拆解",
            flush=True,
        )


class GalleryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, store: GalleryStore):
        self.store = store
        super().__init__(server_address, handler_class)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HomaiGallery/1.0"
    sys_version = ""

    @property
    def store(self) -> GalleryStore:
        return self.server.store  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # Keep the console calm; errors are reported explicitly.
        return

    # ---------- plumbing ----------

    def _send_bytes(self, status: int, body: bytes, content_type: str, cache: str = "no-store", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _error_obj(self, status: int, code: str, message: str) -> None:
        self._send_json(
            status, {"error": {"code": code, "message": message}}
        )

    @staticmethod
    def _etag_matches(header: str | None, etag: str) -> bool:
        if not header:
            return False
        for token in header.split(","):
            token = token.strip()
            if token == "*":
                return True
            if token.startswith("W/"):
                token = token[2:].strip()
            if token == etag:
                return True
        return False

    @staticmethod
    def _not_modified_since(header: str | None, mtime: float) -> bool:
        if not header:
            return False
        try:
            parsed = parsedate_to_datetime(header)
        except (TypeError, ValueError, OverflowError):
            return False
        if parsed is None:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        # HTTP dates have one-second resolution; compare at that granularity.
        return int(mtime) <= int(parsed.timestamp())

    def _serve_file(self, path: Path, content_type: str | None = None, cache: str = "no-store") -> None:
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                raise FileNotFoundError
            file_stat = resolved.stat()
            size = file_stat.st_size
        except (OSError, FileNotFoundError):
            self._error(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        mime = content_type or mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        if mime in {"text/html", "text/css", "text/javascript", "application/javascript", "application/json"}:
            mime += "; charset=utf-8"
        etag = f'"{file_stat.st_mtime_ns:x}-{size:x}"'
        last_modified = formatdate(file_stat.st_mtime, usegmt=True)
        if self.command in {"GET", "HEAD"}:
            if_none_match = self.headers.get("If-None-Match")
            if if_none_match is not None:
                not_modified = self._etag_matches(if_none_match, etag)
            else:
                not_modified = self._not_modified_since(
                    self.headers.get("If-Modified-Since"), file_stat.st_mtime
                )
            if not_modified:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", last_modified)
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", cache)
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with resolved.open("rb") as fh:
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> tuple[dict | None, str | None]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return None, "Content-Length 无效"
        if length <= 0:
            return None, "请求体不能为空"
        if length > MAX_BODY_BYTES:
            return None, "请求体过大"
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "请求体不是有效 JSON"
        if not isinstance(data, dict):
            return None, "请求体必须是 JSON 对象"
        return data, None

    def _consume_body(self) -> None:
        """Drain a small optional request body so keep-alive stays in sync."""
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return
        if 0 < length <= MAX_BODY_BYTES:
            self.rfile.read(length)

    # ---------- dispatch ----------

    def do_GET(self):
        try:
            split = urlsplit(self.path)
            path = unquote(split.path)
            query = parse_qs(split.query, keep_blank_values=True)
            if path in {"/", "/index.html"}:
                self._serve_file(
                    GUI_DIR / "index.html",
                    "text/html; charset=utf-8",
                    cache=STATIC_CACHE,
                )
                return
            if path == "/style.css":
                self._serve_file(
                    GUI_DIR / "style.css", "text/css; charset=utf-8", cache=STATIC_CACHE
                )
                return
            if path == "/app.js":
                self._serve_file(
                    GUI_DIR / "app.js",
                    "text/javascript; charset=utf-8",
                    cache=STATIC_CACHE,
                )
                return
            if path == "/remodel.js":
                self._serve_file(
                    GUI_DIR / "remodel.js",
                    "text/javascript; charset=utf-8",
                    cache=STATIC_CACHE,
                )
                return
            if path == "/favicon.ico":
                self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return
            if path == "/api/credentials":
                self._send_json(HTTPStatus.OK, self.store.credentials_status())
                return
            if path == "/api/bootstrap":
                self._send_json(HTTPStatus.OK, self.store.bootstrap())
                return
            if path == "/api/items":
                self._send_json(HTTPStatus.OK, self.store.query_items(query))
                return
            if path == "/api/export.csv":
                data = self.store.export_csv()
                self._send_bytes(
                    HTTPStatus.OK,
                    data,
                    "text/csv; charset=utf-8",
                    extra={"Content-Disposition": 'attachment; filename="homai-gallery.csv"'},
                )
                return
            if path == "/api/remodel/packs":
                self._send_json(HTTPStatus.OK, self.store.remodel_packs())
                return
            if path == "/api/remodel/history":
                raw_limit = (query.get("limit", ["30"])[0] or "30").strip()
                try:
                    limit = int(raw_limit)
                except ValueError:
                    limit = 30
                self._send_json(HTTPStatus.OK, self.store.remodel_history(limit))
                return
            if path == "/api/remodel/items":
                raw_limit = (query.get("limit", ["50"])[0] or "50").strip()
                raw_offset = (query.get("offset", ["0"])[0] or "0").strip()
                try:
                    limit = int(raw_limit)
                except ValueError:
                    limit = 50
                try:
                    offset = int(raw_offset)
                except ValueError:
                    offset = 0
                self._send_json(
                    HTTPStatus.OK, self.store.list_remodel_items(limit, offset)
                )
                return
            item_gens = re.fullmatch(
                r"/api/remodel/items/([^/]+)/gens", path
            )
            if item_gens:
                item_id = item_gens.group(1)
                if not ITEM_ID_RE.fullmatch(item_id):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                    return
                gens = self.store.list_item_gens(item_id)
                if gens is None:
                    self._error_obj(
                        HTTPStatus.NOT_FOUND, "not_found", "改造项目不存在"
                    )
                    return
                self._send_json(HTTPStatus.OK, gens)
                return
            item_match = re.fullmatch(r"/api/remodel/items/([^/]+)", path)
            if item_match:
                item_id = item_match.group(1)
                if not ITEM_ID_RE.fullmatch(item_id):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                    return
                item = self.store.get_remodel_item(item_id)
                if item is None:
                    self._error_obj(
                        HTTPStatus.NOT_FOUND, "not_found", "改造项目不存在"
                    )
                    return
                self._send_json(HTTPStatus.OK, item)
                return
            if path.startswith("/api/remodel/items/"):
                remainder = path[len("/api/remodel/items/") :]
                first = remainder.split("/", 1)[0]
                if not ITEM_ID_RE.fullmatch(first):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                else:
                    self._error_obj(
                        HTTPStatus.NOT_FOUND, "not_found", "接口不存在"
                    )
                return
            if path.startswith("/api/remodel/jobs/"):
                job_id = path[len("/api/remodel/jobs/") :]
                if not job_id or "/" in job_id:
                    self._error(HTTPStatus.NOT_FOUND, "改造任务不存在")
                    return
                snapshot = self.store.remodel_job_snapshot(job_id)
                if snapshot is None:
                    self._error(HTTPStatus.NOT_FOUND, "改造任务不存在")
                    return
                self._send_json(HTTPStatus.OK, snapshot)
                return
            if path.startswith("/api/jobs/"):
                job_id = path[len("/api/jobs/") :]
                if not job_id or "/" in job_id:
                    self._error(HTTPStatus.NOT_FOUND, "任务不存在")
                    return
                snapshot = self.store.job_snapshot(job_id)
                if snapshot is None:
                    self._error(HTTPStatus.NOT_FOUND, "任务不存在")
                    return
                self._send_json(HTTPStatus.OK, snapshot)
                return
            if path.startswith("/api/items/"):
                sid = path[len("/api/items/") :]
                if not sid or "/" in sid:
                    self._error(HTTPStatus.NOT_FOUND, "条目不存在")
                    return
                record = self.store.merged_record(sid)
                if record is None:
                    self._error(HTTPStatus.NOT_FOUND, "条目不存在")
                    return
                self._send_json(HTTPStatus.OK, record)
                return
            if path.startswith("/remodel/uploads/"):
                self._serve_remodel_upload(path[len("/remodel/uploads/") :])
                return
            if path.startswith("/remodel/jobs/"):
                self._serve_remodel_job(path[len("/remodel/jobs/") :])
                return
            if path.startswith("/remodel/items/"):
                self._serve_remodel_item(path[len("/remodel/items/") :])
                return
            if path.startswith("/media/"):
                self._serve_media(path[len("/media/") :])
                return
            if path.startswith("/samples/"):
                self._serve_sample(path[len("/samples/") :])
                return
            if path.startswith("/thumbs/") and path.endswith(".jpg"):
                sid = path[len("/thumbs/") : -len(".jpg")]
                if not sid or "/" in sid:
                    self._error(HTTPStatus.NOT_FOUND, "缩略图不存在")
                    return
                self._serve_thumb(sid)
                return
            self._error(HTTPStatus.NOT_FOUND, "页面不存在")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            if "/api/remodel/items" in self.path:
                self._error_obj(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal",
                    "服务器内部错误",
                )
            else:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误")

    def do_HEAD(self):
        """HEAD mirrors GET headers; body writes are already skipped."""
        self.do_GET()

    def _serve_media(self, rel_path: str) -> None:
        path = resolve_under(IMAGES_DIR, rel_path, allow_root_prefix=ROOT)
        if path is None or not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "图片不存在")
            return
        self._serve_file(path, cache=IMMUTABLE_CACHE)

    def _serve_sample(self, rel_path: str) -> None:
        rel_path = unquote(rel_path)
        if rel_path.startswith("samples/"):
            rel_path = rel_path[len("samples/") :]
        path = resolve_under(SAMPLES_DIR, rel_path)
        if path is None or not path.is_file() or path.suffix.lower() not in SAMPLE_EXTENSIONS:
            self._error(HTTPStatus.NOT_FOUND, "样图不存在")
            return
        self._serve_file(path, cache="public, max-age=3600")

    def _serve_remodel_upload(self, rel_path: str) -> None:
        path = resolve_under(REMODEL_UPLOAD_DIR, rel_path)
        if (
            path is None
            or not path.is_file()
            or path.suffix.lower() not in REMODEL_IMAGE_EXTENSIONS
        ):
            self._error(HTTPStatus.NOT_FOUND, "上传图片不存在")
            return
        self._serve_file(path, cache=IMMUTABLE_CACHE)

    def _serve_remodel_job(self, rel_path: str) -> None:
        path = resolve_under(REMODEL_JOB_DIR, rel_path)
        if (
            path is None
            or not path.is_file()
            or path.suffix.lower() not in REMODEL_IMAGE_EXTENSIONS
        ):
            self._error(HTTPStatus.NOT_FOUND, "改造图不存在")
            return
        self._serve_file(path, cache=IMMUTABLE_CACHE)

    def _serve_remodel_item(self, rel_path: str) -> None:
        path = resolve_under(REMODEL_ITEM_DIR, rel_path)
        if (
            path is None
            or not path.is_file()
            or path.suffix.lower() not in REMODEL_IMAGE_EXTENSIONS
        ):
            self._error(HTTPStatus.NOT_FOUND, "项目图片不存在")
            return
        self._serve_file(path, cache=IMMUTABLE_CACHE)

    def _serve_thumb(self, sid: str) -> None:
        thumb, source = self.store.ensure_thumbnail(sid)
        if thumb is not None:
            self._serve_file(thumb, "image/jpeg", cache=IMMUTABLE_CACHE)
            return
        if source is not None:
            # Pillow missing: degrade to the original image instead of breaking the grid.
            local_path = str(self.store.meta_by_id[sid].get("local_path") or "")
            location = "/media/" + quote(local_path, safe="/")
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self._error(HTTPStatus.NOT_FOUND, "缩略图不存在")

    def do_PUT(self):
        try:
            split = urlsplit(self.path)
            path = unquote(split.path)
            match = re.fullmatch(r"/api/items/([^/]+)/prompt", path)
            if not match:
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            sid = match.group(1)
            if sid not in self.store.meta_by_id:
                self._error(HTTPStatus.NOT_FOUND, "条目不存在")
                return
            body, error = self._read_json()
            if error:
                self._error(HTTPStatus.BAD_REQUEST, error)
                return
            invalid = [key for key in body if key not in PROMPT_FIELDS]
            if invalid:
                self._error(HTTPStatus.BAD_REQUEST, "不允许修改字段：" + ", ".join(invalid))
                return
            if not body:
                self._error(HTTPStatus.BAD_REQUEST, "没有可保存的字段")
                return
            for field, value in body.items():
                if not isinstance(value, str):
                    self._error(HTTPStatus.BAD_REQUEST, f"{field} 必须是字符串")
                    return
                if len(value) > MAX_PROMPT_CHARS:
                    self._error(HTTPStatus.BAD_REQUEST, f"{field} 过长")
                    return
            record = self.store.save_prompt(sid, body)
            if record is None:
                self._error(HTTPStatus.NOT_FOUND, "条目不存在")
                return
            self._send_json(HTTPStatus.OK, record)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "保存失败")

    def _handle_remodel_upload(self) -> None:
        try:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "Content-Length 无效")
                return
            if length <= 0:
                self._error(HTTPStatus.BAD_REQUEST, "请求体不能为空")
                return
            if length > MAX_MULTIPART_BYTES:
                self._error(HTTPStatus.BAD_REQUEST, "图片不能超过 12 MB")
                return
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("multipart/form-data"):
                self._error(HTTPStatus.BAD_REQUEST, "请使用 multipart/form-data 上传")
                return
            raw = self.rfile.read(length)
            try:
                parts = parse_multipart(raw, content_type)
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            files = parts.get("file") or []
            if not files:
                self._error(HTTPStatus.BAD_REQUEST, "缺少 file 字段")
                return
            try:
                result = self.store.save_upload(files[0]["data"])
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json(HTTPStatus.OK, result)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "上传失败")

    def _handle_remodel_analyze(self) -> None:
        body, error = self._read_json()
        if error:
            self._error(HTTPStatus.BAD_REQUEST, error)
            return
        upload_id = str(body.get("upload_id") or "").strip()
        if not upload_id:
            self._error(HTTPStatus.BAD_REQUEST, "upload_id 不能为空")
            return
        force = bool(body.get("force"))
        try:
            result = self.store.analyze_upload(upload_id, force=force)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except RuntimeError as exc:
            self._error(HTTPStatus.BAD_GATEWAY, clean_error(exc))
            return
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "分析失败")
            return
        if result is None:
            self._error(HTTPStatus.NOT_FOUND, "上传不存在")
            return
        self._send_json(HTTPStatus.OK, result)

    def _handle_remodel_generate(self) -> None:
        body, error = self._read_json()
        if error:
            self._error(HTTPStatus.BAD_REQUEST, error)
            return
        try:
            result = self.store.submit_remodel(body)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except RuntimeError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, clean_error(exc))
            return
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "生成任务提交失败")
            return
        if result is None:
            self._error(HTTPStatus.NOT_FOUND, "上传不存在")
            return
        self._send_json(HTTPStatus.ACCEPTED, result)

    def _handle_remodel_recheck(self, job_id: str) -> None:
        try:
            result = self.store.recheck_remodel(job_id)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except RuntimeError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, clean_error(exc))
            return
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "结构复检失败")
            return
        if result is None:
            self._error(HTTPStatus.NOT_FOUND, "改造任务不存在")
            return
        self._send_json(HTTPStatus.OK, result)

    def _read_remodel_multipart(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return None, "Content-Length 无效"
        if length <= 0:
            return None, "请求体不能为空"
        if length > MAX_MULTIPART_BYTES:
            return None, "图片不能超过 12 MB"
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            return None, "请使用 multipart/form-data 上传"
        raw = self.rfile.read(length)
        try:
            parts = parse_multipart(raw, content_type)
        except ValueError as exc:
            return None, str(exc)
        return parts, None

    def _handle_remodel_item_create(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().startswith("multipart/form-data"):
            parts, error = self._read_remodel_multipart()
            if error:
                self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", error)
                return
            assert parts is not None
            unknown = [key for key in parts if key not in {"photo", "file", "name"}]
            if unknown:
                self._error_obj(
                    HTTPStatus.BAD_REQUEST,
                    "unknown_field",
                    "不允许的字段：" + ", ".join(unknown),
                )
                return
            files = (parts.get("photo") or parts.get("file") or [])
            if not files:
                self._error_obj(
                    HTTPStatus.BAD_REQUEST, "bad_request", "缺少 photo 字段"
                )
                return
            name = ""
            if parts.get("name"):
                name = parts["name"][0]["data"].decode("utf-8", "replace")
            try:
                item = self.store.create_remodel_item(
                    name=name, photo=files[0]["data"]
                )
            except ValueError as exc:
                self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
                return
            except Exception:
                self._error_obj(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "创建项目失败"
                )
                return
            if item is None:
                self._error_obj(HTTPStatus.NOT_FOUND, "not_found", "上传不存在")
                return
            self._send_json(HTTPStatus.CREATED, item)
            return
        body, error = self._read_json()
        if error:
            self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", error)
            return
        unknown = [key for key in body if key not in {"name", "upload_id"}]
        if unknown:
            self._error_obj(
                HTTPStatus.BAD_REQUEST,
                "unknown_field",
                "不允许的字段：" + ", ".join(unknown),
            )
            return
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            self._error_obj(
                HTTPStatus.BAD_REQUEST, "bad_request", "name 必须是字符串"
            )
            return
        upload_id = body.get("upload_id")
        if upload_id is not None and not isinstance(upload_id, str):
            self._error_obj(
                HTTPStatus.BAD_REQUEST, "bad_request", "upload_id 必须是字符串"
            )
            return
        if not upload_id and not (name or "").strip():
            self._error_obj(
                HTTPStatus.BAD_REQUEST,
                "bad_request",
                "需要 upload_id 或 multipart photo",
            )
            return
        try:
            item = self.store.create_remodel_item(
                name=name, upload_id=str(upload_id or "")
            )
        except ValueError as exc:
            self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
            return
        except Exception:
            self._error_obj(
                HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "创建项目失败"
            )
            return
        if item is None:
            self._error_obj(HTTPStatus.NOT_FOUND, "not_found", "上传不存在")
            return
        self._send_json(HTTPStatus.CREATED, item)

    def _handle_remodel_item_source(self, item_id: str) -> None:
        parts, error = self._read_remodel_multipart()
        if error:
            self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", error)
            return
        assert parts is not None
        unknown = [key for key in parts if key not in {"photo", "file"}]
        if unknown:
            self._error_obj(
                HTTPStatus.BAD_REQUEST,
                "unknown_field",
                "不允许的字段：" + ", ".join(unknown),
            )
            return
        files = parts.get("photo") or parts.get("file") or []
        if not files:
            self._error_obj(
                HTTPStatus.BAD_REQUEST, "bad_request", "缺少 photo 字段"
            )
            return
        try:
            item = self.store.replace_item_source(item_id, files[0]["data"])
        except ValueError as exc:
            self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
            return
        except Exception:
            self._error_obj(
                HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "替换原图失败"
            )
            return
        if item is None:
            self._error_obj(HTTPStatus.NOT_FOUND, "not_found", "改造项目不存在")
            return
        self._send_json(HTTPStatus.OK, item)

    def _handle_remodel_item_analyze(self, item_id: str) -> None:
        body, error = self._read_json()
        if error:
            self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", error)
            return
        unknown = [key for key in body if key not in {"force"}]
        if unknown:
            self._error_obj(
                HTTPStatus.BAD_REQUEST,
                "unknown_field",
                "不允许的字段：" + ", ".join(unknown),
            )
            return
        force = body.get("force", False)
        if not isinstance(force, bool):
            self._error_obj(
                HTTPStatus.BAD_REQUEST, "bad_request", "force 必须是布尔值"
            )
            return
        try:
            item = self.store.analyze_item(item_id, force=force)
        except ValueError as exc:
            self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
            return
        except RuntimeError as exc:
            self._error_obj(
                HTTPStatus.BAD_GATEWAY, "analysis_failed", clean_error(exc)
            )
            return
        except Exception:
            self._error_obj(
                HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "结构分析失败"
            )
            return
        if item is None:
            self._error_obj(HTTPStatus.NOT_FOUND, "not_found", "改造项目不存在")
            return
        self._send_json(HTTPStatus.OK, item)

    def _handle_remodel_item_generate(self, item_id: str) -> None:
        body, error = self._read_json()
        if error:
            self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", error)
            return
        try:
            result = self.store.submit_item_remodel(item_id, body)
        except ValueError as exc:
            self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
            return
        except RuntimeError as exc:
            self._error_obj(
                HTTPStatus.INTERNAL_SERVER_ERROR, "internal", clean_error(exc)
            )
            return
        except Exception:
            self._error_obj(
                HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "生成任务提交失败"
            )
            return
        if result is None:
            self._error_obj(HTTPStatus.NOT_FOUND, "not_found", "改造项目不存在")
            return
        self._send_json(HTTPStatus.ACCEPTED, result)

    def _handle_remodel_item_detail_optimize(self, item_id: str) -> None:
        body, error = self._read_json()
        if error:
            self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", error)
            return
        try:
            result = self.store.submit_item_detail_optimize(item_id, body)
        except ValueError as exc:
            self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
            return
        except RuntimeError as exc:
            self._error_obj(
                HTTPStatus.INTERNAL_SERVER_ERROR, "internal", clean_error(exc)
            )
            return
        except Exception:
            self._error_obj(
                HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "细节优化任务提交失败"
            )
            return
        if result is None:
            self._error_obj(HTTPStatus.NOT_FOUND, "not_found", "改造项目不存在")
            return
        self._send_json(HTTPStatus.ACCEPTED, result)

    def do_POST(self):
        try:
            split = urlsplit(self.path)
            path = unquote(split.path)
            if path == "/api/credentials":
                body, error = self._read_json()
                if error:
                    self._error(HTTPStatus.BAD_REQUEST, error)
                    return
                try:
                    state = self.store.save_credentials(
                        body.get("api_key"), body.get("base_url")
                    )
                except ValueError as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                except Exception:
                    self._error(
                        HTTPStatus.INTERNAL_SERVER_ERROR, "保存本地凭据失败"
                    )
                    return
                self._send_json(HTTPStatus.OK, state)
                return
            if path == "/api/remodel/thumbnails/backfill":
                self._consume_body()
                stats = self.store.backfill_remodel_thumbnails()
                self._send_json(HTTPStatus.OK, stats)
                return
            if path == "/api/remodel/items":
                self._handle_remodel_item_create()
                return
            item_source = re.fullmatch(
                r"/api/remodel/items/([^/]+)/source", path
            )
            if item_source:
                item_id = item_source.group(1)
                if not ITEM_ID_RE.fullmatch(item_id):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                    return
                self._handle_remodel_item_source(item_id)
                return
            item_analyze = re.fullmatch(
                r"/api/remodel/items/([^/]+)/analyze", path
            )
            if item_analyze:
                item_id = item_analyze.group(1)
                if not ITEM_ID_RE.fullmatch(item_id):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                    return
                self._handle_remodel_item_analyze(item_id)
                return
            item_generate = re.fullmatch(
                r"/api/remodel/items/([^/]+)/generate", path
            )
            if item_generate:
                item_id = item_generate.group(1)
                if not ITEM_ID_RE.fullmatch(item_id):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                    return
                self._handle_remodel_item_generate(item_id)
                return
            item_detail_optimize = re.fullmatch(
                r"/api/remodel/items/([^/]+)/detail-optimize", path
            )
            if item_detail_optimize:
                item_id = item_detail_optimize.group(1)
                if not ITEM_ID_RE.fullmatch(item_id):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                    return
                self._handle_remodel_item_detail_optimize(item_id)
                return
            if path.startswith("/api/remodel/items/"):
                remainder = path[len("/api/remodel/items/") :]
                first = remainder.split("/", 1)[0]
                if not ITEM_ID_RE.fullmatch(first):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                else:
                    self._error_obj(
                        HTTPStatus.NOT_FOUND, "not_found", "接口不存在"
                    )
                return
            if path == "/api/remodel/upload":
                self._handle_remodel_upload()
                return
            if path == "/api/remodel/analyze":
                self._handle_remodel_analyze()
                return
            if path == "/api/remodel/generate":
                self._handle_remodel_generate()
                return
            recheck = re.fullmatch(r"/api/remodel/recheck/([^/]+)", path)
            if recheck:
                self._handle_remodel_recheck(recheck.group(1))
                return
            restore = re.fullmatch(r"/api/items/([^/]+)/restore", path)
            generate = re.fullmatch(r"/api/items/([^/]+)/generate", path)
            if restore:
                sid = restore.group(1)
                record = self.store.restore_prompt(sid)
                if record is None:
                    self._error(HTTPStatus.NOT_FOUND, "条目不存在")
                    return
                self._send_json(HTTPStatus.OK, record)
                return
            if generate:
                sid = generate.group(1)
                if sid not in self.store.meta_by_id:
                    self._error(HTTPStatus.NOT_FOUND, "条目不存在")
                    return
                body, error = self._read_json()
                if error:
                    self._error(HTTPStatus.BAD_REQUEST, error)
                    return
                try:
                    job_id = self.store.submit_generation(sid, body)
                except ValueError as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self._send_json(HTTPStatus.ACCEPTED, {"job_id": job_id})
                return
            self._error(HTTPStatus.NOT_FOUND, "接口不存在")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            if "/api/remodel/items" in self.path:
                self._error_obj(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "操作失败"
                )
            else:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "操作失败")


    def do_PATCH(self):
        try:
            split = urlsplit(self.path)
            path = unquote(split.path)
            gen_match = re.fullmatch(
                r"/api/remodel/items/([^/]+)/gens/([^/]+)", path
            )
            if gen_match:
                item_id, gid = gen_match.group(1), gen_match.group(2)
                if not ITEM_ID_RE.fullmatch(item_id) or not GEN_ID_RE.fullmatch(gid):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_id", "无效的项目或版本 ID"
                    )
                    return
                body, error = self._read_json()
                if error:
                    self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", error)
                    return
                unknown = [key for key in body if key not in {"note"}]
                if unknown:
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST,
                        "unknown_field",
                        "不允许的字段：" + ", ".join(unknown),
                    )
                    return
                if "note" not in body:
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "bad_request", "没有可更新的字段"
                    )
                    return
                try:
                    gen = self.store.patch_item_gen(
                        item_id, gid, body.get("note")
                    )
                except ValueError as exc:
                    self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
                    return
                except Exception:
                    self._error_obj(
                        HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "更新版本失败"
                    )
                    return
                if gen is None:
                    self._error_obj(
                        HTTPStatus.NOT_FOUND, "not_found", "改造版本不存在"
                    )
                    return
                self._send_json(HTTPStatus.OK, gen)
                return
            item_match = re.fullmatch(r"/api/remodel/items/([^/]+)", path)
            if item_match:
                item_id = item_match.group(1)
                if not ITEM_ID_RE.fullmatch(item_id):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                    return
                body, error = self._read_json()
                if error:
                    self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", error)
                    return
                try:
                    item = self.store.update_remodel_item(item_id, body)
                except ValueError as exc:
                    self._error_obj(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
                    return
                except Exception:
                    self._error_obj(
                        HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "更新项目失败"
                    )
                    return
                if item is None:
                    self._error_obj(
                        HTTPStatus.NOT_FOUND, "not_found", "改造项目不存在"
                    )
                    return
                self._send_json(HTTPStatus.OK, item)
                return
            if path.startswith("/api/remodel/items/"):
                remainder = path[len("/api/remodel/items/") :]
                first = remainder.split("/", 1)[0]
                if not ITEM_ID_RE.fullmatch(first):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                else:
                    self._error_obj(
                        HTTPStatus.NOT_FOUND, "not_found", "接口不存在"
                    )
                return
            self._error_obj(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._error_obj(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "操作失败")

    def do_DELETE(self):
        try:
            split = urlsplit(self.path)
            path = unquote(split.path)
            query = parse_qs(split.query, keep_blank_values=True)
            if path == "/api/credentials":
                self._consume_body()
                self._send_json(HTTPStatus.OK, self.store.clear_credentials())
                return
            gen_match = re.fullmatch(
                r"/api/remodel/items/([^/]+)/gens/([^/]+)", path
            )
            if gen_match:
                item_id, gid = gen_match.group(1), gen_match.group(2)
                if not ITEM_ID_RE.fullmatch(item_id) or not GEN_ID_RE.fullmatch(gid):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_id", "无效的项目或版本 ID"
                    )
                    return
                try:
                    item = self.store.delete_item_gen(item_id, gid)
                except Exception:
                    self._error_obj(
                        HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "删除版本失败"
                    )
                    return
                if item is None:
                    self._error_obj(
                        HTTPStatus.NOT_FOUND, "not_found", "改造版本不存在"
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "item_id": item_id, "gid": gid},
                )
                return
            item_match = re.fullmatch(r"/api/remodel/items/([^/]+)", path)
            if item_match:
                item_id = item_match.group(1)
                if not ITEM_ID_RE.fullmatch(item_id):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                    return
                raw_purge = (query.get("purge", ["0"])[0] or "0").strip().lower()
                purge = raw_purge in {"1", "true", "yes", "on"}
                try:
                    result = self.store.delete_remodel_item(item_id, purge=purge)
                except FileExistsError as exc:
                    self._error_obj(
                        HTTPStatus.CONFLICT, "conflict", clean_error(exc)
                    )
                    return
                except Exception:
                    self._error_obj(
                        HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "删除项目失败"
                    )
                    return
                if result is None:
                    self._error_obj(
                        HTTPStatus.NOT_FOUND, "not_found", "改造项目不存在"
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path.startswith("/api/remodel/items/"):
                remainder = path[len("/api/remodel/items/") :]
                first = remainder.split("/", 1)[0]
                if not ITEM_ID_RE.fullmatch(first):
                    self._error_obj(
                        HTTPStatus.BAD_REQUEST, "invalid_item_id", "无效的项目 ID"
                    )
                else:
                    self._error_obj(
                        HTTPStatus.NOT_FOUND, "not_found", "接口不存在"
                    )
                return
            self._error_obj(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._error_obj(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "操作失败")


def main() -> int:
    parser = argparse.ArgumentParser(description="HOMAI 图库本地工作台")
    parser.add_argument("--host", default=None, help="监听地址（覆盖 gui/config.json）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（覆盖 gui/config.json）")
    args = parser.parse_args()
    config = load_config()
    host = args.host if args.host is not None else str(config.get("host") or "127.0.0.1")
    port = args.port if args.port is not None else int(config.get("port") or 8765)

    seed_demo_data()
    # 运行时目录先建好：上传 / 迁移 / 冒烟写入都假定它们存在
    for directory in (REMODEL_DIR, REMODEL_UPLOAD_DIR, REMODEL_JOB_DIR, REMODEL_ITEM_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    store = GalleryStore(config)
    server = GalleryHTTPServer((host, port), Handler, store)
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print(f"HOMAI 图库已启动：http://{display_host}:{port}/", flush=True)
    print(f"数据：{len(store.metadata)} 条；已出图：{len(store.generated_ids)} 条；提示词覆盖：{len(store.overrides)} 条", flush=True)
    if store.style_errors:
        print(f"警告：{len(store.style_errors)} 个风格文件读取失败", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())