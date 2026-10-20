"""Portable deployment settings; credentials stay in the process environment."""
from dataclasses import dataclass
import math
import re
from collections.abc import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    persona_id: str = "猫娘小樱"
    image_base_url: str = "https://api.openai.com/v1"
    image_model: str = "gpt-image-1"
    image_api_key_env: str = "SAKURA_IMAGE_API_KEY"
    image_timeout_seconds: int = 300
    merge_window_seconds: float = 2.0
    reference_ttl_seconds: int = 600
    admin_user_ids: tuple[str, ...] = ()
    admin_session_ttl_seconds: int = 3600
    high_risk_confirmation_ttl_seconds: int = 120

    @classmethod
    def from_config(cls, config: Mapping | None = None):
        values = dict(config or {})
        defaults = cls()
        result = {}
        for key in ("persona_id", "image_base_url", "image_model", "image_api_key_env"):
            value = values.get(key, getattr(defaults, key))
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string")
            result[key] = value.strip()
        parsed = urlsplit(result["image_base_url"])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("image_base_url must be an HTTPS API URL without credentials, query or fragment")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result["image_api_key_env"]):
            raise ValueError("image_api_key_env must be an environment variable name")
        limits = {
            "image_timeout_seconds": (5, 300),
            "merge_window_seconds": (0.1, 10),
            "reference_ttl_seconds": (1, 3600),
            "admin_session_ttl_seconds": (1, 86400),
            "high_risk_confirmation_ttl_seconds": (1, 600),
        }
        for key, (lower, upper) in limits.items():
            value = values.get(key, getattr(defaults, key))
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"{key} must be between {lower} and {upper}")
            result[key] = float(value) if key == "merge_window_seconds" else int(value)
        ids = values.get("admin_user_ids", [])
        if not isinstance(ids, list) or any(not isinstance(item, str) or not item.strip() for item in ids):
            raise ValueError("admin_user_ids must be a list of non-empty strings")
        result["admin_user_ids"] = tuple(dict.fromkeys(item.strip() for item in ids))
        return cls(**result)

    def permissions_config(self):
        return {
            "admin_user_ids": list(self.admin_user_ids),
            "admin_session_ttl_seconds": self.admin_session_ttl_seconds,
            "high_risk_confirmation_ttl_seconds": self.high_risk_confirmation_ttl_seconds,
        }
