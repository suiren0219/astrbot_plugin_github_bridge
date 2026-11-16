"""一次性识别旧版内置预设；不向新配置写入任何个人预设。"""

from hashlib import sha256
from typing import Any

SETTINGS_SCHEMA_VERSION = 2
# 固定指纹仅用于与旧配置做精确匹配，不用于新配置初始化。
LEGACY_PRESET_DIGESTS = {
    "github_owner": "f4e3a7838c3cea97ebcd680f121f3a4bf1e3c28c6354bd7f9ce64e53e1a90bd1",
    "issue_repositories": "7148b3521321bc1f2faf4ddbb9fccb39873e2fd79f2f3bbe4f90c83e6d4568a8",
    "target_umo": "3ad9ca89685c54a03041387324d1c8e7853b403685e9e7ddc30e00008e0891a1",
}


def clear_legacy_presets(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """只清理旧版配置中的精确内置值，保留用户自定义值。"""
    version = data.get("_schema_version", 0)
    if type(version) is int and version >= SETTINGS_SCHEMA_VERSION:
        return data, []
    cleaned = dict(data)
    changed = []
    for key, digest in LEGACY_PRESET_DIGESTS.items():
        value = data.get(key)
        if (
            isinstance(value, str)
            and sha256(value.encode("utf-8")).hexdigest() == digest
        ):
            cleaned[key] = ""
            changed.append(key)
    if changed:
        cleaned["enabled"] = False
        cleaned["_cleared_legacy_presets"] = changed
    return cleaned, changed
