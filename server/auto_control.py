"""Local Auto-toggle control for Codex Router native redirect.

This module never edits Codex UI/config directly.  Auto ON asks the installed
Codex Router to redirect every native GPT request that reaches it to jev/auto;
Auto OFF clears that redirect so the user's native Codex model/effort selection
takes effect again.

The state file is owned by Codex Router.  We read it for cheap status polling
and invoke Codex Router's own control command for mutations so its validation
and file-protection rules remain authoritative.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

AUTO_ROUTE = "jev/auto"
CONTROL_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class AutoStatus:
    enabled: bool
    redirect_model: Optional[str]
    available: bool
    error: Optional[str] = None

    def as_dict(self):
        return {
            "auto": self.enabled,
            "redirect_model": self.redirect_model,
            "available": self.available,
            "error": self.error,
        }


def native_redirect_path(state_dir: str) -> str:
    return os.path.join(state_dir, "native-redirect.json")


def read_redirect_model(state_dir: str) -> Optional[str]:
    path = native_redirect_path(state_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    model = value.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def resolve_control_script(router_dir: Optional[str]) -> Optional[str]:
    if not router_dir:
        return None
    root = os.path.realpath(os.path.expanduser(router_dir))
    script = os.path.join(root, "src", "control.mjs")
    return script if os.path.isfile(script) else None


def status(state_dir: str, router_dir: Optional[str]) -> AutoStatus:
    model = read_redirect_model(state_dir)
    available = resolve_control_script(router_dir) is not None
    return AutoStatus(
        enabled=model == AUTO_ROUTE,
        redirect_model=model,
        available=available,
        error=None if available else "CODEX_ROUTER_DIR is not configured or invalid",
    )


def set_enabled(state_dir: str, router_dir: Optional[str], enabled: bool) -> AutoStatus:
    script = resolve_control_script(router_dir)
    if not script:
        return AutoStatus(
            enabled=False,
            redirect_model=read_redirect_model(state_dir),
            available=False,
            error="CODEX_ROUTER_DIR is not configured or invalid",
        )

    args = ["node", script, "native-redirect"]
    if enabled:
        args.extend(["set", AUTO_ROUTE])
    else:
        args.append("clear")

    try:
        proc = subprocess.run(
            args,
            cwd=os.path.dirname(os.path.dirname(script)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=CONTROL_TIMEOUT_S,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        current = read_redirect_model(state_dir)
        return AutoStatus(
            enabled=current == AUTO_ROUTE,
            redirect_model=current,
            available=True,
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )

    current = read_redirect_model(state_dir)
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or f"control exited {proc.returncode}").strip()
        return AutoStatus(
            enabled=current == AUTO_ROUTE,
            redirect_model=current,
            available=True,
            error=message[:240],
        )

    wanted = AUTO_ROUTE if enabled else None
    if current != wanted:
        return AutoStatus(
            enabled=current == AUTO_ROUTE,
            redirect_model=current,
            available=True,
            error="Codex Router control completed but native redirect state did not match",
        )

    return AutoStatus(
        enabled=enabled,
        redirect_model=current,
        available=True,
        error=None,
    )
