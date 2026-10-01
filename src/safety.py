"""Safety guards: domain allowlist and destructive-action detection."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from .models import SafetyConfig

# Generic (not site specific) wording that signals an irreversible action.
DESTRUCTIVE_PATTERNS = re.compile(
    r"\b(pay(ment)?( now)?|place (the |your )?order|purchase|buy now|checkout and pay|"
    r"confirm (payment|purchase|order)|delete|remove (my )?account|deactivate|"
    r"close account|cancel (my )?subscription|erase|destroy|wipe|unsubscribe all)\b",
    re.IGNORECASE,
)
# Argument keys that describe *what is being acted on* (not text being typed).
TARGET_KEYS = ("element", "target", "description", "name", "label")


def host_of(url: str) -> str:
    candidate = url if "://" in url else f"//{url}"
    return (urlparse(candidate).hostname or "").lower()


def effective_allowlist(configured: list[str], start_url: str) -> list[str]:
    """Configured domains, or just the start URL's host when none are configured."""
    domains = [d.strip().lower().lstrip("*.") for d in configured if d.strip()]
    if domains:
        return domains
    start = host_of(start_url)
    return [start] if start else []


def host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in allowlist)


class SafetyGuard:
    def __init__(self, cfg: SafetyConfig, start_url: str) -> None:
        self.cfg = cfg
        self.allowlist = effective_allowlist(cfg.allowed_domains, start_url)

    def check_url(self, url: str) -> str | None:
        """Return an error message if the URL is outside the allowlist."""
        host = host_of(url)
        if not host:
            return None  # about:blank, relative paths, etc.
        if not host_allowed(host, self.allowlist):
            return (f"Blocked: host '{host}' is not in the allowed domains "
                    f"({', '.join(self.allowlist) or 'none'}). Stay on allowed domains.")
        return None

    def check_call(self, name: str, args: dict[str, Any]) -> str | None:
        """Pre-execution check for a tool call. Returns an error message to block it."""
        url = args.get("url")
        if isinstance(url, str):
            err = self.check_url(url)
            if err:
                return err
        if not self.cfg.allow_destructive:
            for key in TARGET_KEYS:
                val = args.get(key)
                if isinstance(val, str) and DESTRUCTIVE_PATTERNS.search(val):
                    return (f"Blocked: '{val}' looks like a destructive action and "
                            "destructive actions are disabled. Do not perform it; "
                            "finish the test and report what you observed instead.")
        return None


# --- masking of typed values --------------------------------------------------
SENSITIVE_FIELD = re.compile(r"pass(word|wd|code)|secret|token|api[-_ ]?key|\bpin\b|cvv|otp",
                             re.IGNORECASE)
MASK = "••••"


def sanitize_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Copy of tool arguments that is safe to display or persist.

    Text typed into a field whose description looks sensitive (password, token, ...)
    is replaced by a mask, so typed secrets are never streamed even when they were
    not supplied through an env placeholder. Placeholders such as ``{{env:NAME}}``
    are kept as is: they carry no secret.
    """
    out = dict(args)
    described = " ".join(str(args.get(k, "")) for k in ("element", "target", "name", "label"))
    if "text" in out and SENSITIVE_FIELD.search(described):
        out["text"] = MASK
    fields = out.get("fields")
    if isinstance(fields, list):  # browser_fill_form
        clean = []
        for f in fields:
            if isinstance(f, dict) and (SENSITIVE_FIELD.search(str(f.get("name", "")))
                                        or str(f.get("type", "")).lower() == "password"):
                f = {**f, "value": MASK}
            clean.append(f)
        out["fields"] = clean
    return out
