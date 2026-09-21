import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import patch_codex_router as patcher
import jev_server as jev


def upstream_fixture(condition):
    return (
        "async function handleResponses(request) {\n"
        "  const exactRouteProbe = exactRouteProbeRequested(request.headers);\n"
        "  let registeredRoute;\n"
        f"    {condition}\n"
        "      const redirect = MODEL_BY_SLUG.get(readNativeRedirect());\n"
        "      if (redirect) registeredRoute = redirect;\n"
        "    }\n"
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


class JevExactRouteCapability(unittest.TestCase):
    def setUp(self):
        self.old_cache = jev._exact_native_route_cache

    def tearDown(self):
        jev._exact_native_route_cache = self.old_cache

    def test_capability_requires_supervisor_opt_in_and_patched_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "src"
            source_dir.mkdir()
            router = source_dir / "router.mjs"
            router.write_text(
                upstream_fixture(patcher.PATCHED_CONDITION),
                encoding="utf-8",
            )
            jev._exact_native_route_cache = None
            with mock.patch.object(jev, "CODEX_ROUTER_DIR", str(root)):
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

    def test_capability_falls_back_after_source_loses_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "src"
            source_dir.mkdir()
            router = source_dir / "router.mjs"
            router.write_text(
                upstream_fixture(patcher.PATCHED_CONDITION),
                encoding="utf-8",
            )
            with mock.patch.object(jev, "CODEX_ROUTER_DIR", str(root)), \
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


if __name__ == "__main__":
    unittest.main()
