"""Turn raw tool calls into human readable actions for the viewer overlay."""
from __future__ import annotations

import re
from typing import Any

# Playwright MCP tool name -> overlay action. Unlisted tools show their own name.
ACTIONS = {
    "browser_navigate": "navigate", "browser_navigate_back": "navigate",
    "browser_click": "click", "browser_hover": "hover", "browser_drag": "drag",
    "browser_type": "type", "browser_fill_form": "type", "browser_press_key": "type",
    "browser_select_option": "select", "browser_file_upload": "upload",
    "browser_wait_for": "wait", "browser_snapshot": "observe",
    "browser_take_screenshot": "observe", "browser_tabs": "tabs",
}

# "- button "Sign in" [ref=e5] ..." lines of an accessibility snapshot
_REF_LINE = re.compile(r'^\s*-\s+([A-Za-z]+)(?:\s+"((?:[^"\\]|\\.)*)")?[^\n]*?\[ref=([A-Za-z0-9]+)\]',
                       re.MULTILINE)


class RefIndex:
    """Maps snapshot refs (e12) to (role, accessible name) so the target can be located."""

    def __init__(self) -> None:
        self._refs: dict[str, tuple[str, str]] = {}

    def update(self, snapshot_text: str) -> None:
        for role, name, ref in _REF_LINE.findall(snapshot_text):
            self._refs[ref] = (role, name.replace('\\"', '"'))

    def lookup(self, ref: str | None) -> dict[str, str] | None:
        if not ref or ref not in self._refs:
            return None
        role, name = self._refs[ref]
        return {"ref": ref, "role": role, "name": name}


def describe(name: str, args: dict[str, Any], target: dict | None) -> tuple[str, str]:
    """(action, label) for a tool call. ``args`` must already be sanitized."""
    action = ACTIONS.get(name, name.removeprefix("browser_"))
    detail = ""
    if action == "navigate" and args.get("url"):
        detail = str(args["url"])
    elif target:
        detail = f"{target['role']} '{target['name']}'" if target.get("name") else target["role"]
    elif args.get("element"):
        detail = str(args["element"])
    elif name == "browser_press_key" and args.get("key"):
        detail = str(args["key"])
    elif action == "wait":
        detail = str(args.get("text") or args.get("textGone") or args.get("time") or "")
    if action == "type" and args.get("text") not in (None, ""):
        detail = f"{detail} ← {args['text']}".strip()
    return action, f"{action} {detail}".strip()
