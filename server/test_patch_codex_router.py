import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import patch_codex_router as patcher
import jev_server as jev


def upstream_fixture(condition, include_quota_anchor=True):
    failure = (
        "      failedBodyText = await boundedResponseText(\n"
        "        upstream,\n"
        "        MAX_BUFFERED_RESPONSE_BYTES,\n"
        "        controller.signal,\n"
        "      );\n"
        if include_quota_anchor
        else ""
    )
    return (
        "async function handleResponses(request, response, requestUrl) {\n"
        "  const exactRouteProbe = exactRouteProbeRequested(request.headers);\n"
        "  let registeredRoute;\n"
        f"    {condition}\n"
        "      const redirect = MODEL_BY_SLUG.get(readNativeRedirect());\n"
        "      if (redirect) registeredRoute = redirect;\n"
        "    }\n"
        "  let failedBodyText;\n"
        "  if (route && !upstream.ok) {\n"
        + failure
        + "    let verdict = classifyRoutedFailure({});\n"
        "  }\n"
        "}\n"
    )


class CodexRouterExactRoutePatch(unittest.TestCase):
    def test_patches_native_redirect_condition_once(self):
        original = upstream_fixture(patcher.ORIGINAL_CONDITION)
        patched, changed = patcher.patch_router_text(original)
        self.assertTrue(changed)
        self.assertTrue(patcher.source_supports_exact_native_route(patched))
        self.assertNotIn(
            "\n    if (!registeredRoute && requestedModel) {\n",
            patched,
        )

    def test_patch_is_idempotent(self):
        original = upstream_fixture(patcher.ORIGINAL_CONDITION)
        patched, _changed = patcher.patch_router_text(original)
        second, changed = patcher.patch_router_text(patched)
        self.assertFalse(changed)
        self.assertEqual(second, patched)


    def test_patch_adds_native_quota_passthrough_hook(self):
        original = upstream_fixture(patcher.ORIGINAL_CONDITION)
        patched, changed = patcher.patch_router_text(original)
        self.assertTrue(changed)
        self.assertIn(patcher.QUOTA_MARKER, patched)
        self.assertIn(patcher.QUOTA_PASSTHROUGH_SENTINEL, patched)
        self.assertIn("route.provider === \"jev\"", patched)
        self.assertIn("writeJson(response, 429", patched)

    def test_exact_only_old_patch_is_upgraded_with_quota_hook(self):
        original = upstream_fixture(patcher.PATCHED_CONDITION)
        patched, changed = patcher.patch_router_text(original)
        self.assertTrue(changed)
        self.assertTrue(patcher.source_supports_exact_native_route(patched))

    def test_missing_quota_failure_anchor_fails_closed(self):
        original = upstream_fixture(
            patcher.ORIGINAL_CONDITION,
            include_quota_anchor=False,
        )
        with self.assertRaises(patcher.PatchError):
            patcher.patch_router_text(original)

    def test_unrecognized_upstream_shape_fails_closed(self):
        source = upstream_fixture("if (somethingElse) {")
        with self.assertRaises(patcher.PatchError):
            patcher.patch_router_text(source)

    def test_file_patch_preserves_crlf_newlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "src"
            source_dir.mkdir()
            router = source_dir / "router.mjs"
            original = upstream_fixture(patcher.ORIGINAL_CONDITION).replace("\n", "\r\n")
            router.write_bytes(original.encode("utf-8"))
            _path, changed = patcher.patch_router_file(root)
            self.assertTrue(changed)
            raw = router.read_bytes()
            self.assertIn(b"\r\n", raw)
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
            self.assertIn(patcher.PATCHED_CONDITION.encode("utf-8"), raw)


    def test_unarmed_patch_restarts_once_then_marker_prevents_restart_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "src"
            source_dir.mkdir()
            router = source_dir / "router.mjs"
            router.write_text(
                upstream_fixture(patcher.ORIGINAL_CONDITION),
                encoding="utf-8",
            )
            state = root / "state"
            with mock.patch.object(patcher, "restart_router") as restart:
                first = patcher.ensure_patch(root, state, restart=True)
                self.assertTrue(first["changed"])
                self.assertTrue(first["restarted"])
                self.assertTrue(first["armed"])
                restart.assert_called_once_with(root)

                restart.reset_mock()
                second = patcher.ensure_patch(root, state, restart=True)
                self.assertFalse(second["changed"])
                self.assertFalse(second["restarted"])
                self.assertTrue(second["armed"])
                restart.assert_not_called()

    def test_prepatched_but_unarmed_source_is_restarted_before_trust(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "src"
            source_dir.mkdir()
            router = source_dir / "router.mjs"
            router.write_text(
                patcher.patch_router_text(
                    upstream_fixture(patcher.ORIGINAL_CONDITION)
                )[0],
                encoding="utf-8",
            )
            state = root / "state"
            with mock.patch.object(patcher, "restart_router") as restart:
                result = patcher.ensure_patch(root, state, restart=True)
            self.assertFalse(result["changed"])
            self.assertTrue(result["restarted"])
            self.assertTrue(result["armed"])
            restart.assert_called_once_with(root)


class JevExactRouteCapability(unittest.TestCase):
    def setUp(self):
        self.old_cache = jev._exact_native_route_cache

    def tearDown(self):
        jev._exact_native_route_cache = self.old_cache

    def _armed_fixture(self, root):
        source_dir = root / "src"
        source_dir.mkdir()
        router = source_dir / "router.mjs"
        router.write_text(
            patcher.patch_router_text(
                upstream_fixture(patcher.ORIGINAL_CONDITION)
            )[0],
            encoding="utf-8",
        )
        state = root / "state"
        patcher.write_marker(state, router, patcher.source_sha256(router))
        return router, state

    def test_capability_requires_supervisor_opt_in_and_matching_arm_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            router, state = self._armed_fixture(root)
            jev._exact_native_route_cache = None
            with mock.patch.object(jev, "CODEX_ROUTER_DIR", str(root)), \
                 mock.patch.object(jev, "STATE", str(state)):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(jev._EXACT_NATIVE_ROUTE_ENV, None)
                    self.assertFalse(jev.exact_native_route_supported())
                jev._exact_native_route_cache = None
                with mock.patch.dict(
                    os.environ,
                    {jev._EXACT_NATIVE_ROUTE_ENV: "1"},
                    clear=False,
                ):
                    self.assertTrue(jev.exact_native_route_supported())

    def test_capability_rejects_unarmed_patched_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "src"
            source_dir.mkdir()
            (source_dir / "router.mjs").write_text(
                patcher.patch_router_text(
                    upstream_fixture(patcher.ORIGINAL_CONDITION)
                )[0],
                encoding="utf-8",
            )
            state = root / "state"
            state.mkdir()
            with mock.patch.object(jev, "CODEX_ROUTER_DIR", str(root)), \
                 mock.patch.object(jev, "STATE", str(state)), \
                 mock.patch.dict(
                     os.environ,
                     {jev._EXACT_NATIVE_ROUTE_ENV: "1"},
                     clear=False,
                 ):
                jev._exact_native_route_cache = None
                self.assertFalse(jev.exact_native_route_supported())

    def test_capability_falls_back_after_source_loses_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            router, state = self._armed_fixture(root)
            with mock.patch.object(jev, "CODEX_ROUTER_DIR", str(root)), \
                 mock.patch.object(jev, "STATE", str(state)), \
                 mock.patch.dict(
                     os.environ,
                     {jev._EXACT_NATIVE_ROUTE_ENV: "1"},
                     clear=False,
                 ):
                jev._exact_native_route_cache = None
                self.assertTrue(jev.exact_native_route_supported())
                router.write_text(
                    upstream_fixture(patcher.ORIGINAL_CONDITION) + " ",
                    encoding="utf-8",
                )
                self.assertFalse(jev.exact_native_route_supported())


    def test_exact_forward_keeps_global_redirect_visible(self):
        with tempfile.TemporaryDirectory() as state:
            with mock.patch.object(jev, "STATE", state), \
                 mock.patch.object(jev, "exact_native_route_supported", return_value=True):
                path, held = jev._native_redirect_paths()
                Path(path).write_text(
                    '{"version":1,"model":"jev/auto"}',
                    encoding="utf-8",
                )
                with jev.concrete_native_forward():
                    self.assertTrue(os.path.exists(path))
                    self.assertFalse(os.path.exists(held))
                self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()