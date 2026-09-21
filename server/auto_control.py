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


def backup_path(state_dir: str) -> str:
    return os.path.join(state_dir, "jev-auto-native-redirect-backup.json")


def _read_backup(state_dir: str):
    try:
        with open(backup_path(state_dir), encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None, False
    if not isinstance(value, dict) or value.get("version") != 1:
        return None, False
    model = value.get("previous_model")
    if model is None:
        return None, True
    if isinstance(model, str) and model.strip():
        return model.strip(), True
    return None, False


def _write_backup(state_dir: str, previous_model: Optional[str]) -> None:
    os.makedirs(state_dir, exist_ok=True)
    path = backup_path(state_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(
            {"version": 1, "previous_model": previous_model},
            fh,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _clear_backup(state_dir: str) -> None:
    try:
        os.remove(backup_path(state_dir))
    except FileNotFoundError:
        pass


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


def _run_control(script: str, action: str, model: Optional[str] = None):
    args = ["node", script, "native-redirect", action]
    if model:
        args.append(model)
    return subprocess.run(
        args,
        cwd=os.path.dirname(os.path.dirname(script)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=CONTROL_TIMEOUT_S,
        check=False,
        shell=False,
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

    current_before = read_redirect_model(state_dir)
    previous_model, has_backup = _read_backup(state_dir)

    if enabled:
        if current_before != AUTO_ROUTE and not has_backup:
            _write_backup(state_dir, current_before)
        action, target = "set", AUTO_ROUTE
    else:
        # If another tool changed the redirect while Auto was on, do not
        # overwrite that newer operator choice.
        if current_before != AUTO_ROUTE:
            _clear_backup(state_dir)
            return AutoStatus(
                enabled=False,
                redirect_model=current_before,
                available=True,
                error=None,
            )
        action = "set" if has_backup and previous_model else "clear"
        target = previous_model if action == "set" else None

    try:
        proc = _run_control(script, action, target)
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

    wanted = AUTO_ROUTE if enabled else (previous_model if has_backup else None)
    if current != wanted:
        return AutoStatus(
            enabled=current == AUTO_ROUTE,
            redirect_model=current,
            available=True,
            error="Codex Router control completed but native redirect state did not match",
        )

    if not enabled:
        _clear_backup(state_dir)

    return AutoStatus(
        enabled=enabled,
        redirect_model=current,
        available=True,
        error=None,
    )
