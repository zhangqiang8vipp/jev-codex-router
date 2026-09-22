#!/usr/bin/env python3
"""Install Jev's scoped native-route/quota hooks into Codex Router.

Jev forwards a concrete native GPT tier back through Codex Router's authenticated
caller edge. The managed hooks make the authenticated exact-route probe bypass
only native-redirect for that concrete request, and restore hard native Codex
usage-limit 429s after the local generic-provider hop.

The patch is intentionally narrow, idempotent, and fail-closed: if an upstream
source anchor is no longer recognized, this script refuses to rewrite it. A
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
HANDLE_RESPONSES_ANCHOR = "async function handleResponses(request, response, requestUrl) {"
QUOTA_FAILURE_ANCHOR = """      failedBodyText = await boundedResponseText(
        upstream,
        MAX_BUFFERED_RESPONSE_BYTES,
        controller.signal,
      );
"""
QUOTA_MARKER = "__JEV_NATIVE_QUOTA_V1__"
QUOTA_HELPER_NAME = "jevNativeQuotaEnvelope"
QUOTA_PASSTHROUGH_SENTINEL = "jev-native-quota-pass-through"
MARKER_NAME = "jev-exact-native-route.json"
MARKER_VERSION = 2

QUOTA_HELPER = r'''
function jevNativeQuotaEnvelope(bodyText) {
  const marker = "__JEV_NATIVE_QUOTA_V1__:";
  const at = String(bodyText || "").indexOf(marker);
  if (at < 0) return undefined;
  const match = /^[A-Za-z0-9_-]+/.exec(String(bodyText).slice(at + marker.length));
  const token = match?.[0];
  if (!token || token.length > 32 * 1024) return undefined;

  let decoded;
  try {
    decoded = JSON.parse(Buffer.from(token, "base64url").toString("utf8"));
  } catch {
    return undefined;
  }
  if (decoded?.version !== 1 || !decoded.error || typeof decoded.error !== "object") {
    return undefined;
  }

  const type = decoded.error.type;
  if (!["usage_limit_reached", "usage_not_included", "insufficient_quota"].includes(type)) {
    return undefined;
  }
  const message =
    typeof decoded.error.message === "string" && decoded.error.message.length <= 2048
      ? decoded.error.message
      : "You have reached your Codex usage limit.";
  const error = { type, message };
  if (
    typeof decoded.error.code === "string" &&
    decoded.error.code.length <= 128
  ) {
    error.code = decoded.error.code;
  }
  if (
    typeof decoded.error.plan_type === "string" &&
    decoded.error.plan_type.length <= 64
  ) {
    error.plan_type = decoded.error.plan_type;
  }
  if (
    Number.isSafeInteger(decoded.error.resets_at) &&
    decoded.error.resets_at > 0
  ) {
    error.resets_at = decoded.error.resets_at;
  }

  const allowedExact = new Set([
    "retry-after",
    "x-codex-active-limit",
    "x-codex-promo-message",
    "x-codex-rate-limit-reached-type",
    "x-codex-credits-has-credits",
    "x-codex-credits-unlimited",
    "x-codex-credits-balance",
  ]);
  const windowHeader =
    /^x-[a-z0-9][a-z0-9-]{0,63}-(?:primary|secondary)-(?:used-percent|window-minutes|reset-at)$/;
  const limitName = /^x-[a-z0-9][a-z0-9-]{0,63}-limit-name$/;
  const headers = {};
  for (const [rawName, rawValue] of Object.entries(decoded.headers || {})) {
    const name = String(rawName).toLowerCase();
    const value = String(rawValue);
    if (
      value.length <= 512 &&
      (allowedExact.has(name) || windowHeader.test(name) || limitName.test(name))
    ) {
      headers[name] = value;
    }
  }
  return { error, headers };
}
'''

QUOTA_PASSTHROUGH_BLOCK = r'''      // jev-native-quota-pass-through: Jev's inner exact native call may
      // return the real ChatGPT account/workspace hard limit. LiteLLM wraps a
      // routed provider 429, so unwrap only Jev's authenticated local marker
      // and restore the canonical HTTP 429 shape Codex itself understands.
      if (route.provider === "jev" && upstream.status === 429) {
        const nativeQuota = jevNativeQuotaEnvelope(failedBodyText);
        if (nativeQuota) {
          for (const [name, value] of Object.entries(nativeQuota.headers)) {
            response.setHeader(name, value);
          }
          writeJson(response, 429, { error: nativeQuota.error });
          recordObservedUsage({
            model: route.slug,
            provider: canonicalProviderId(route.provider),
            status: 429,
            durationMs: Date.now() - startedAt,
            responseStartMs: upstreamLatencyMs,
          }, diagnostics);
          observeSubagentOutcome(request, route, 429);
          finalStatus = 429;
          activityStatus = 429;
          usageRecorded = true;
          return;
        }
      }
'''


class PatchError(RuntimeError):
    pass


def source_supports_exact_native_route(text: str) -> bool:
    """Whether both managed caller-edge hooks are present."""
    if PATCHED_CONDITION not in text:
        return False
    condition_at = text.find(PATCHED_CONDITION)
    redirect_at = text.find(REDIRECT_ANCHOR, condition_at)
    exact_ok = redirect_at >= 0 and redirect_at - condition_at < 600
    quota_ok = (
        f"function {QUOTA_HELPER_NAME}" in text
        and QUOTA_PASSTHROUGH_SENTINEL in text
        and QUOTA_MARKER in text
    )
    return exact_ok and quota_ok


def patch_router_text(text: str) -> tuple[str, bool]:
    """Return (patched_text, changed), rejecting unfamiliar upstream shapes."""
    if EXACT_PROBE_DECLARATION not in text:
        raise PatchError("Codex Router exact-route probe declaration was not found.")

    newline = "\r\n" if "\r\n" in text else "\n"
    quota_helper = QUOTA_HELPER.replace("\n", newline)
    quota_block = QUOTA_PASSTHROUGH_BLOCK.replace("\n", newline)
    quota_failure_anchor = QUOTA_FAILURE_ANCHOR.replace("\n", newline)
    changed = False

    if PATCHED_CONDITION not in text:
        redirect_at = text.find(REDIRECT_ANCHOR)
        if redirect_at < 0:
            raise PatchError("Codex Router native redirect anchor was not found.")

        window_start = max(0, redirect_at - 500)
        before_redirect = text[window_start:redirect_at]
        relative = before_redirect.rfind(ORIGINAL_CONDITION)
        if relative < 0:
            raise PatchError("Codex Router native redirect condition is not a recognized shape.")

        condition_at = window_start + relative
        if text.find(
            ORIGINAL_CONDITION,
            condition_at + len(ORIGINAL_CONDITION),
            redirect_at,
        ) >= 0:
            raise PatchError("Codex Router native redirect condition is ambiguous.")

        text = (
            text[:condition_at]
            + PATCHED_CONDITION
            + text[condition_at + len(ORIGINAL_CONDITION):]
        )
        changed = True

    if f"function {QUOTA_HELPER_NAME}" not in text:
        handle_at = text.find(HANDLE_RESPONSES_ANCHOR)
        if handle_at < 0:
            raise PatchError("Codex Router Responses handler anchor was not found.")
        text = text[:handle_at] + quota_helper + newline + text[handle_at:]
        changed = True

    if QUOTA_PASSTHROUGH_SENTINEL not in text:
        anchor_at = text.find(quota_failure_anchor)
        if anchor_at < 0:
            raise PatchError("Codex Router routed-failure anchor was not found.")
        insert_at = anchor_at + len(quota_failure_anchor)
        text = text[:insert_at] + quota_block + text[insert_at:]
        changed = True

    if not source_supports_exact_native_route(text):
        raise PatchError("Patched Codex Router source did not pass verification.")
    return text, changed


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
        "mode": "exact-route-probe-plus-native-quota-pass-through",
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