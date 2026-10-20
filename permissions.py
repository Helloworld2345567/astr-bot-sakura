"""Session-scoped permission gates for Sakura's agent tools.

AstrBot's built-in computer tools use ``event.role`` for their final admin
check.  This module only changes that role for a single private event after a
controlled administrator has explicitly granted a temporary session.  It
never changes AstrBot's global ``admins_id`` configuration.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.core.tools.web_search_tools import TavilyExtractWebPageTool, TavilyWebSearchTool


# Queries and URLs are data, not local commands. Only these actual builtin
# implementations are exempt; a plugin cannot obtain the exemption by name.
_PUBLIC_WEB_TOOL_TYPES = (TavilyWebSearchTool, TavilyExtractWebPageTool)


DEFAULT_ADMIN_USER_IDS = ()
DEFAULT_SESSION_TTL_SECONDS = 60 * 60
DEFAULT_CONFIRMATION_TTL_SECONDS = 2 * 60

_GRANT_RE = re.compile(
    r"(?:授权|允许|同意|开启|启用|提升|临时).{0,20}(?:管理员权限|管理权限|高权限|特权|admin)",
    re.IGNORECASE,
)
# A direct mode prefix is an instruction; mentioning or asking about the mode
# elsewhere in a sentence is not an elevation request.
_ADMIN_MODE_PREFIX_RE = re.compile(r"^(?:进入|开启|启用|切换到|使用)?管理员模式\s*[，,:：]\s*\S")
_REVOKE_RE = re.compile(
    r"(?:取消|撤销|关闭|结束|退出|降回|恢复).{0,20}(?:管理员权限|管理权限|高权限|特权|admin|授权)",
    re.IGNORECASE,
)
_CONFIRM_RE = re.compile(
    r"(?:确认|同意|批准|授权|允许).{0,12}(?:执行|操作|继续|进行)|(?:执行|操作|继续).{0,12}(?:确认|同意|批准)",
    re.IGNORECASE,
)
_HIGH_RISK_TEXT_RE = re.compile(
    r"(?:powershell|power\s*shell|cmd\.exe|shell|命令行|脚本|删除|清空|卸载|安装|修改系统|修改配置|管理员名单|权限配置|api[-_ ]?key|密钥|密码|凭据|外传|上传|下载|注册表|registry|sudo|rm\s+-|format\s+)",
    re.IGNORECASE,
)

# Built-in computer tools and other actions that can mutate the host or expose
# local data.  A tool is allowed only after a fresh, explicit confirmation.
_HIGH_RISK_TOOL_RE = re.compile(
    r"(?:execute_shell|shell_session|execute_python|execute_ipython|file_read|file_write|file_edit|grep|upload|download|mouse_click|keyboard_type|cua_screenshot|execute_browser|send_message_to_user|promote_skill|rollback_skill|sync_skill)",
    re.IGNORECASE,
)


@dataclass
class _SessionState:
    admin_until: float = 0.0
    pending_high_risk_until: float = 0.0


class PermissionManager:
    """Apply private-chat, explicit, temporary permission elevation."""

    ADMIN_ROLE_EXTRA = "_sakura_permission_role"
    HIGH_RISK_CONFIRMED_EXTRA = "_sakura_high_risk_confirmed"
    HIGH_RISK_BLOCKED_EXTRA = "_sakura_high_risk_blocked"

    def __init__(self, config_path: Path | None = None, *, config: dict | None = None) -> None:
        self.config_path = config_path
        config = dict(config) if config is not None else self._read_config()
        self.session_ttl = self._positive_seconds(
            config.get("admin_session_ttl_seconds"), DEFAULT_SESSION_TTL_SECONDS
        )
        self.confirmation_ttl = self._positive_seconds(
            config.get("high_risk_confirmation_ttl_seconds"),
            DEFAULT_CONFIRMATION_TTL_SECONDS,
        )
        self.admin_user_ids = self._load_admin_ids(config)
        self._sessions: dict[str, _SessionState] = {}

    def _read_config(self) -> dict[str, Any]:
        try:
            if self.config_path is not None and self.config_path.is_file():
                configured = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(configured, dict):
                    return configured
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        return {}

    @staticmethod
    def _positive_seconds(value: Any, fallback: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed > 0 else fallback

    def _load_admin_ids(self, configured: dict[str, Any]) -> set[str]:
        ids = configured.get("admin_user_ids")
        if not isinstance(ids, list):
            ids = list(DEFAULT_ADMIN_USER_IDS)
        env_ids = os.environ.get("SAKURA_ADMIN_USER_IDS", "")
        if env_ids.strip():
            ids.extend(part.strip() for part in env_ids.split(","))
        return {str(value).strip() for value in ids if str(value).strip()}

    @staticmethod
    def _sender_id(event: Any) -> str:
        return str(event.get_sender_id() or "").strip()

    @staticmethod
    def _session_key(event: Any) -> str:
        return str(
            getattr(event, "unified_msg_origin", None)
            or event.get_session_id()
            or f"{event.get_platform_name()}:{event.get_sender_id()}"
        )

    def _state(self, event: Any) -> _SessionState:
        key = self._session_key(event)
        state = self._sessions.setdefault(key, _SessionState())
        now = time.monotonic()
        if state.admin_until <= now:
            state.admin_until = 0.0
        if state.pending_high_risk_until <= now:
            state.pending_high_risk_until = 0.0
        return state

    def _set_role(self, event: Any, role: str, reason: str) -> None:
        event.role = role
        event.set_extra(self.ADMIN_ROLE_EXTRA, reason)

    def apply_event(self, event: Any) -> None:
        """Set the effective role before an LLM request is built."""
        text = str(event.get_message_str() or "").strip()
        sender_id = self._sender_id(event)
        private = bool(event.is_private_chat())

        # Group messages can never elevate, including messages from an ID that
        # is present in AstrBot's global admins_id list.
        if not private:
            self._set_role(event, "member", "group_chat_forced_member")
            event.set_extra(self.HIGH_RISK_CONFIRMED_EXTRA, False)
            return

        if sender_id not in self.admin_user_ids:
            # The Sakura gate has its own allow-list.  Do not let a different
            # global AstrBot admin identity accidentally inherit this session's
            # temporary elevation policy.
            self._set_role(event, "member", "uncontrolled_sender")
            event.set_extra(self.HIGH_RISK_CONFIRMED_EXTRA, False)
            return

        state = self._state(event)
        if _REVOKE_RE.search(text):
            state.admin_until = 0.0
            state.pending_high_risk_until = 0.0
            self._set_role(event, "member", "explicitly_revoked")
            event.set_extra(self.HIGH_RISK_CONFIRMED_EXTRA, False)
            return

        if _GRANT_RE.search(text) or _ADMIN_MODE_PREFIX_RE.search(text):
            state.admin_until = time.monotonic() + self.session_ttl

        active = state.admin_until > time.monotonic()
        confirmation = bool(_CONFIRM_RE.search(text))
        high_risk_text = bool(_HIGH_RISK_TEXT_RE.search(text))

        if active and state.pending_high_risk_until > time.monotonic() and confirmation:
            # A confirmation is single-use.  The tool hook consumes it again
            # when the first high-risk tool is actually selected.
            state.pending_high_risk_until = 0.0
            self._set_role(event, "admin", "explicit_high_risk_confirmation")
            event.set_extra(self.HIGH_RISK_CONFIRMED_EXTRA, True)
            return

        event.set_extra(self.HIGH_RISK_CONFIRMED_EXTRA, False)
        if active and not high_risk_text:
            self._set_role(event, "admin", "explicit_private_admin_session")
            return

        if active and high_risk_text:
            state.pending_high_risk_until = time.monotonic() + self.confirmation_ttl
            self._set_role(event, "member", "high_risk_confirmation_required")
            event.set_extra(self.HIGH_RISK_BLOCKED_EXTRA, True)
            return

        self._set_role(event, "member", "default_member")

    @classmethod
    def tool_is_high_risk(cls, tool: Any, tool_args: dict[str, Any] | None) -> bool:
        if type(tool) in _PUBLIC_WEB_TOOL_TYPES:
            return False
        name = str(getattr(tool, "name", ""))
        if _HIGH_RISK_TOOL_RE.search(name):
            return True
        try:
            values = json.dumps(tool_args or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            values = str(tool_args or "")
        return bool(_HIGH_RISK_TEXT_RE.search(values))

    def enforce_tool(self, event: Any, tool: Any, tool_args: dict[str, Any] | None) -> None:
        """Re-check high-risk permission immediately before tool execution."""
        if not self.tool_is_high_risk(tool, tool_args):
            return
        confirmed = event.get_extra(self.HIGH_RISK_CONFIRMED_EXTRA, False) is True
        if not confirmed or not event.is_private_chat() or self._sender_id(event) not in self.admin_user_ids:
            self._set_role(event, "member", "high_risk_denied")
            event.set_extra(self.HIGH_RISK_BLOCKED_EXTRA, True)
            return
        # A confirmation grants exactly one high-risk tool call.
        event.set_extra(self.HIGH_RISK_CONFIRMED_EXTRA, False)
