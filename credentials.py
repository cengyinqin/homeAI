#!/usr/bin/env python3
"""模型凭据解析：环境变量 → 本地 credentials.json。

这是整个项目唯一的凭据解析入口。任何生成、视觉分析、自检或需要模型的
提示词构建都必须经过 ``resolve_model_credentials()``。

密钥只在进程内存中使用；对外只允许返回 ``mask_api_key()`` 的掩码形式，
不得写入日志或接口响应。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CREDENTIALS_PATH = ROOT / "credentials.json"

ENV_KEY = "HOMAI_DASHSCOPE_KEY"
ENV_BASE_URL = "HOMAI_DASHSCOPE_BASE_URL"
DEFAULT_BASE_URL = (
    "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
MISSING_MESSAGE = (
    "未找到模型凭据：请在网页右上角「设置」填写模型 Key，或设置环境变量 "
    "HOMAI_DASHSCOPE_KEY / HOMAI_DASHSCOPE_BASE_URL"
)


def mask_api_key(key: str | None) -> str:
    """Return the only key representation allowed outside the process."""
    value = str(key or "").strip()
    if not value:
        return ""
    if len(value) <= 4:
        return "…"
    prefix = "sk-…" if value.startswith("sk-") else "…"
    return prefix + value[-4:]


def _clean(value) -> str:
    return str(value or "").strip()


def _read_env() -> tuple[str, str]:
    key = _clean(os.environ.get(ENV_KEY))
    base_url = _clean(os.environ.get(ENV_BASE_URL)).rstrip("/")
    return key, base_url


def _read_local(credentials_path: str | Path | None = None) -> tuple[str, str]:
    path = Path(credentials_path) if credentials_path else CREDENTIALS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    key = _clean(data.get("dashscope_api_key"))
    base_url = _clean(data.get("dashscope_base_url")).rstrip("/")
    return key, base_url


def _resolve(
    credentials_path: str | Path | None = None,
) -> tuple[str, str, str, dict]:
    """Resolve once and return (key, base_url, source, flags).

    The key comes from the first source that has one. ``base_url`` likewise
    takes the first non-empty value in precedence order, so an env key can
    reuse a local base URL and the token-plan gateway remains the default.
    """
    env_key, env_base = _read_env()
    local_key, local_base = _read_local(credentials_path)

    key = ""
    source = "none"
    for candidate_source, candidate_key in (
        ("env", env_key),
        ("local", local_key),
    ):
        if candidate_key:
            key = candidate_key
            source = candidate_source
            break

    base_url = env_base or local_base or DEFAULT_BASE_URL
    flags = {
        "has_local": bool(local_key),
        "env_present": bool(env_key),
    }
    return key, base_url, source, flags


def resolve_model_credentials(
    credentials_path: str | Path | None = None,
) -> tuple[str, str, str]:
    """Return ``(api_key, base_url, source)`` for one model call.

    Raises ``RuntimeError`` with guidance when no key exists. Never call this
    for display purposes; use ``credentials_snapshot()``.
    """
    key, base_url, source, _flags = _resolve(credentials_path)
    if not key:
        raise RuntimeError(MISSING_MESSAGE)
    return key, base_url, source


def credentials_snapshot(
    credentials_path: str | Path | None = None,
) -> dict:
    """Return the safe, display-only credential state. Never includes the key."""
    key, base_url, source, flags = _resolve(credentials_path)
    return {
        "source": source,
        "key_masked": mask_api_key(key),
        "base_url": base_url,
        "has_local": flags["has_local"],
        "env_present": flags["env_present"],
    }


def save_local_credentials(
    api_key: str,
    base_url: str | None = None,
    credentials_path: str | Path | None = None,
) -> dict:
    """Persist a local key with 0600 permissions and return safe state."""
    key = _clean(api_key)
    if len(key) <= 20:
        raise ValueError("API Key 长度必须大于 20 个字符")
    path = Path(credentials_path) if credentials_path else CREDENTIALS_PATH
    existing_key, existing_base = _read_local(path)
    del existing_key  # 现有密钥只用于判断，绝不回传。
    resolved_base = _clean(base_url) or existing_base or DEFAULT_BASE_URL

    created_parent = not path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if created_parent:
        os.chmod(path.parent, 0o700)

    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "dashscope_api_key": key,
                    "dashscope_base_url": resolved_base,
                },
                fh,
                ensure_ascii=False,
                indent=2,
            )
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    return credentials_snapshot(path)


def delete_local_credentials(
    credentials_path: str | Path | None = None,
) -> dict:
    """Remove the local credential file and return the post-delete state."""
    path = Path(credentials_path) if credentials_path else CREDENTIALS_PATH
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return credentials_snapshot(path)