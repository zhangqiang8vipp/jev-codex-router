import json
import os
import tempfile
import unittest
from unittest import mock

import auto_control as control


class AutoControl(unittest.TestCase):
    def write_redirect(self, state, model):
        path = control.native_redirect_path(state)
        if model is None:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "model": model}, fh)

    def fake_runner(self, state):
        def run(_script, action, model=None):
            if action == "set":
                self.write_redirect(state, model)
            elif action == "clear":
                self.write_redirect(state, None)
            return mock.Mock(returncode=0, stdout="{}", stderr="")
        return run

    def test_status_reads_jev_auto_redirect(self):
        with tempfile.TemporaryDirectory() as state:
            self.write_redirect(state, control.AUTO_ROUTE)
            with mock.patch.object(control, "resolve_control_script", return_value=__file__):
                result = control.status(state, "/router")
            self.assertTrue(result.enabled)
            self.assertEqual(result.redirect_model, control.AUTO_ROUTE)

    def test_enable_preserves_existing_redirect_and_disable_restores_it(self):
        with tempfile.TemporaryDirectory() as state:
            previous = "deepseek/deepseek-v4.1-flash"
            self.write_redirect(state, previous)
            with mock.patch.object(control, "resolve_control_script", return_value=__file__), \
                 mock.patch.object(control, "_run_control", side_effect=self.fake_runner(state)):
                enabled = control.set_enabled(state, "/router", True)
                self.assertTrue(enabled.enabled)
                self.assertEqual(control.read_redirect_model(state), control.AUTO_ROUTE)

                disabled = control.set_enabled(state, "/router", False)
                self.assertFalse(disabled.enabled)
                self.assertEqual(control.read_redirect_model(state), previous)
                self.assertFalse(os.path.exists(control.backup_path(state)))

    def test_disable_does_not_clobber_newer_operator_redirect(self):
        with tempfile.TemporaryDirectory() as state:
            with mock.patch.object(control, "resolve_control_script", return_value=__file__), \
                 mock.patch.object(control, "_run_control", side_effect=self.fake_runner(state)):
                control.set_enabled(state, "/router", True)
                self.write_redirect(state, "other/provider-model")
                result = control.set_enabled(state, "/router", False)
            self.assertFalse(result.enabled)
            self.assertEqual(control.read_redirect_model(state), "other/provider-model")
            self.assertFalse(os.path.exists(control.backup_path(state)))

    def test_missing_router_dir_fails_closed_without_mutating_redirect(self):
        with tempfile.TemporaryDirectory() as state:
            self.write_redirect(state, "native/other")
            result = control.set_enabled(state, None, True)
            self.assertFalse(result.available)
            self.assertEqual(control.read_redirect_model(state), "native/other")


if __name__ == "__main__":
    unittest.main()
