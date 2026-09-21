#!/usr/bin/env python3
"""Install the scoped exact-native-route hook into a Codex Router checkout.

Jev forwards a concrete native GPT tier back through Codex Router's authenticated
caller edge. Codex Router's native redirect normally catches every native slug,
including that already-routed tier. Reusing the router's existing
x-codex-router-exact-route probe as an exact-model signal lets the caller edge
skip only the native redirect for this authenticated request, without moving the
router-wide native-redirect state file.

The patch is intentionally tiny, idempotent, and fail-closed: if the upstream
source shape is no longer recognized, this script refuses to rewrite it.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


PATCHED_CONDITION = "if (!registeredRoute && requestedModel && !exactRouteProbe) {"
ORIGINAL_CONDITION = "if (!registeredRoute && requestedModel) {"
EXACT_PROBE_DECLARATION = "const exactRouteProbe = exactRouteProbeRequested(request.headers);"
REDIRECT_ANCHOR = "const redirect = MODEL_BY_SLUG.get(readNativeRedirect());"


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

    # Preserve the checkout's existing newline bytes. The replacement changes
    # only one condition and should not churn a large upstream source file.
    encoded = patched.encode("utf-8")
    mode = router_path.stat().st_mode
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--router-dir", required=True)
    parser.add_argument(
        "--restart",
        action="store_true",
        help="restart Codex Router only when this invocation changed router.mjs",
    )
    args = parser.parse_args(argv)

    router_dir = Path(args.router_dir).expanduser().resolve()
    try:
        router_path, changed = patch_router_file(router_dir)
        restarted = False
        if changed and args.restart:
            restart_router(router_dir)
            restarted = True
        result = {
            "ok": True,
            "changed": changed,
            "restarted": restarted,
            "router": str(router_path),
            "exact_native_route": True,
        }
        print(json.dumps(result, separators=(",", ":")))
        return 0
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
