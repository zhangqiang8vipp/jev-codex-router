#!/usr/bin/env python3
"""Install the scoped exact-native-route hook into a Codex Router checkout.

Jev forwards a concrete native GPT tier back through Codex Router's authenticated
caller edge. Codex Router's native redirect normally catches every native slug,
including that already-routed tier. Reusing the router's existing
x-codex-router-exact-route probe as an exact-model signal lets the caller edge
skip only the native redirect for this authenticated request, without moving the
router-wide native-redirect state file.

The patch is intentionally tiny, idempotent, and fail-closed: if the upstream
source shape is no longer recognized, this script refuses to rewrite it. A
state marker is armed only after the Codex Router service successfully restarts
on the patched source, so Jev never trusts a source edit that is not live yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


PATCHED_CONDITION = "if (!registeredRoute && requestedModel && !exactRouteProbe) {"
ORIGINAL_CONDITION = "if (!registeredRoute && requestedModel) {"
EXACT_PROBE_DECLARATION = "const exactRouteProbe = exactRouteProbeRequested(request.headers);"
REDIRECT_ANCHOR = "const redirect = MODEL_BY_SLUG.get(readNativeRedirect());"
MARKER_NAME = "jev-exact-native-route.json"
MARKER_VERSION = 1


class PatchError(RuntimeError):
    pass


def source_supports_exact_native_route(text: str) -> bool:
    """Whether exact-route already bypasses the router-wide native redirect."""
    if PATCHED_CONDITION not in text:
        return False
    condition_at = text.find(PATCHED_CONDITION)
    redirect_at = text.find(REDIRECT_ANCHOR, condition_at)
    return redirect_at >= 0 and redirect_at - condition_at < 600


def patch_router_text(text: str) -> tuple[str, bool]:
    """Return (patched_text, changed), rejecting unfamiliar upstream shapes."""
    if EXACT_PROBE_DECLARATION not in text:
        raise PatchError("Codex Router exact-route probe declaration was not found.")
    if source_supports_exact_native_route(text):
        return text, False

    redirect_at = text.find(REDIRECT_ANCHOR)
    if redirect_at < 0:
        raise PatchError("Codex Router native redirect anchor was not found.")

    window_start = max(0, redirect_at - 500)
    before_redirect = text[window_start:redirect_at]
    relative = before_redirect.rfind(ORIGINAL_CONDITION)
    if relative < 0:
        raise PatchError("Codex Router native redirect condition is not a recognized shape.")

    condition_at = window_start + relative
    if text.find(ORIGINAL_CONDITION, condition_at + len(ORIGINAL_CONDITION), redirect_at) >= 0:
        raise PatchError("Codex Router native redirect condition is ambiguous.")

    patched = (
        text[:condition_at]
        + PATCHED_CONDITION
        + text[condition_at + len(ORIGINAL_CONDITION):]
    )
    if not source_supports_exact_native_route(patched):
        raise PatchError("Patched Codex Router source did not pass verification.")
    return patched, True


def patch_router_file(router_dir: Path) -> tuple[Path, bool]:
    router_path = router_dir / "src" / "router.mjs"
    if not router_path.is_file():
        raise PatchError(f"Codex Router source not found: {router_path}")

    raw = router_path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"Codex Router source is not UTF-8: {router_path}") from exc

    patched, changed = patch_router_text(text)
    if not changed:
        return router_path, False

    # read_bytes/decode/encode preserves the checkout's existing newline bytes.
    # The replacement changes only one condition and must not churn a large
    # upstream source file.
    encoded = patched.encode("utf-8")
    mode = stat.S_IMODE(router_path.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(prefix=".jev-router-patch-", dir=str(router_path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, router_path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return router_path, True


def source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def marker_path(state_dir: Path) -> Path:
    return state_dir / MARKER_NAME


def marker_matches(state_dir: Path, router_path: Path, sha256: str) -> bool:
    try:
        value = json.loads(marker_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and value.get("version") == MARKER_VERSION
        and value.get("router") == str(router_path.resolve())
        and value.get("router_sha256") == sha256
    )


def write_marker(state_dir: Path, router_path: Path, sha256: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = marker_path(state_dir)
    payload = {
        "version": MARKER_VERSION,
        "router": str(router_path.resolve()),
        "router_sha256": sha256,
        "mode": "exact-route-probe-bypasses-native-redirect",
    }
    fd, tmp_name = tempfile.mkstemp(prefix=".jev-exact-route-", dir=str(state_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def restart_router(router_dir: Path) -> None:
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        raise PatchError("Node.js was not found; Codex Router cannot be restarted.")
    service = router_dir / "src" / "service.mjs"
    if not service.is_file():
        raise PatchError(f"Codex Router service entrypoint not found: {service}")
    result = subprocess.run(
        [node, str(service), "restart"],
        cwd=str(router_dir),
        check=False,
    )
    if result.returncode != 0:
        raise PatchError(f"Codex Router service restart failed with status {result.returncode}.")


def ensure_patch(router_dir: Path, state_dir: Path, restart: bool) -> dict:
    router_path, changed = patch_router_file(router_dir)
    sha256 = source_sha256(router_path)
    armed = marker_matches(state_dir, router_path, sha256)
    restarted = False

    # A changed source or an unarmed pre-existing patch must be loaded by the
    # running Node service before Jev is allowed to skip the legacy suppression.
    if restart and (changed or not armed):
        restart_router(router_dir)
        write_marker(state_dir, router_path, sha256)
        restarted = True
        armed = True

    return {
        "ok": True,
        "changed": changed,
        "restarted": restarted,
        "armed": armed,
        "router": str(router_path),
        "router_sha256": sha256,
        "exact_native_route": armed,
    }


def check_patch(router_dir: Path, state_dir: Path) -> dict:
    router_path = router_dir / "src" / "router.mjs"
    if not router_path.is_file():
        raise PatchError(f"Codex Router source not found: {router_path}")
    raw = router_path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"Codex Router source is not UTF-8: {router_path}") from exc
    supported = source_supports_exact_native_route(text)
    sha256 = source_sha256(router_path)
    armed = supported and marker_matches(state_dir, router_path, sha256)
    return {
        "ok": armed,
        "changed": False,
        "restarted": False,
        "armed": armed,
        "router": str(router_path),
        "router_sha256": sha256,
        "exact_native_route": armed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--router-dir", required=True)
    parser.add_argument(
        "--state-dir",
        default=os.path.join(os.path.expanduser("~"), ".codex", "codex-router"),
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="restart Codex Router when the patch is new or not yet armed",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the live-arm marker without modifying source",
    )
    args = parser.parse_args(argv)

    router_dir = Path(args.router_dir).expanduser().resolve()
    state_dir = Path(args.state_dir).expanduser().resolve()
    try:
        if args.check:
            result = check_patch(router_dir, state_dir)
            print(json.dumps(result, separators=(",", ":")))
            return 0 if result["ok"] else 1
        result = ensure_patch(router_dir, state_dir, args.restart)
        print(json.dumps(result, separators=(",", ":")))
        return 0 if result["exact_native_route"] else 1
    except PatchError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "exact_native_route": False},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
