#!/usr/bin/env python3
"""模型凭据解析器的单元测试（不启动服务，不使用桩）。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import credentials


LOCAL_KEY = "sk-local-0123456789abcdefghij"
LOCAL_BASE = "https://local.example.test/compatible-mode/v1"
ENV_KEY = "sk-env-0123456789abcdefghij"
ENV_BASE = "https://env.example.test/compatible-mode/v1"


class CredentialResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="homai-credentials-test-")
        self.root = Path(self.temp.name)
        self.local_path = self.root / "credentials.json"
        self.patch = mock.patch.object(
            credentials, "CREDENTIALS_PATH", self.local_path
        )
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temp.cleanup()

    def write_local(self, key: str = LOCAL_KEY, base_url: str = LOCAL_BASE) -> None:
        self.local_path.write_text(
            json.dumps(
                {"dashscope_api_key": key, "dashscope_base_url": base_url},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.chmod(self.local_path, 0o600)

    def test_env_wins_even_when_local_file_exists(self) -> None:
        self.write_local()
        with mock.patch.dict(
            os.environ,
            {
                "HOMAI_DASHSCOPE_KEY": ENV_KEY,
                "HOMAI_DASHSCOPE_BASE_URL": ENV_BASE,
            },
            clear=True,
        ):
            key, base_url, source = credentials.resolve_model_credentials()
        self.assertEqual((key, base_url, source), (ENV_KEY, ENV_BASE, "env"))

    def test_env_key_without_base_uses_local_base(self) -> None:
        self.write_local()
        with mock.patch.dict(
            os.environ, {"HOMAI_DASHSCOPE_KEY": ENV_KEY}, clear=True
        ):
            key, base_url, source = credentials.resolve_model_credentials()
        self.assertEqual((key, base_url, source), (ENV_KEY, LOCAL_BASE, "env"))

    def test_local_file_used_when_env_is_absent(self) -> None:
        self.write_local()
        with mock.patch.dict(os.environ, {}, clear=True):
            key, base_url, source = credentials.resolve_model_credentials()
        self.assertEqual((key, base_url, source), (LOCAL_KEY, LOCAL_BASE, "local"))

    def test_missing_everything_raises_with_settings_guidance(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                credentials.resolve_model_credentials()
        message = str(caught.exception)
        self.assertIn("HOMAI_DASHSCOPE_KEY", message)
        self.assertIn("设置", message)

    def test_snapshot_has_exactly_the_safe_fields(self) -> None:
        self.write_local()
        with mock.patch.dict(os.environ, {}, clear=True):
            state = credentials.credentials_snapshot()
        self.assertEqual(
            set(state),
            {"source", "key_masked", "base_url", "has_local", "env_present"},
        )
        self.assertEqual(state["source"], "local")
        self.assertTrue(state["has_local"])
        self.assertFalse(state["env_present"])
        self.assertNotIn(LOCAL_KEY, json.dumps(state, ensure_ascii=False))

    def test_mask_only_reveals_last_four_characters(self) -> None:
        masked = credentials.mask_api_key(ENV_KEY)
        self.assertEqual(masked, "sk-…ghij")
        self.assertNotIn(ENV_KEY, masked)
        self.assertTrue(masked.endswith(ENV_KEY[-4:]))
        self.assertEqual(credentials.mask_api_key(""), "")

    def test_save_local_file_is_0600_and_resolver_reads_it(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            state = credentials.save_local_credentials(
                LOCAL_KEY, LOCAL_BASE, credentials_path=self.local_path
            )
            key, base_url, source = credentials.resolve_model_credentials()
        mode = stat.S_IMODE(self.local_path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual((key, base_url, source), (LOCAL_KEY, LOCAL_BASE, "local"))
        self.assertEqual(state["source"], "local")
        self.assertEqual(state["key_masked"], "sk-…ghij")
        self.assertNotIn(LOCAL_KEY, json.dumps(state, ensure_ascii=False))
        self.assertTrue(state["has_local"])

    def test_save_rejects_key_that_is_not_longer_than_20_chars(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "20"):
                credentials.save_local_credentials(
                    "x" * 20, LOCAL_BASE, credentials_path=self.local_path
                )
        self.assertFalse(self.local_path.exists())

    def test_delete_local_file_falls_back_to_env(self) -> None:
        self.write_local()
        with mock.patch.dict(
            os.environ,
            {
                "HOMAI_DASHSCOPE_KEY": ENV_KEY,
                "HOMAI_DASHSCOPE_BASE_URL": ENV_BASE,
            },
            clear=True,
        ):
            state = credentials.delete_local_credentials(
                credentials_path=self.local_path
            )
            key, base_url, source = credentials.resolve_model_credentials()
        self.assertFalse(self.local_path.exists())
        self.assertEqual((key, base_url, source), (ENV_KEY, ENV_BASE, "env"))
        self.assertFalse(state["has_local"])
        self.assertTrue(state["env_present"])
        self.assertEqual(state["source"], "env")

    def test_delete_local_file_without_env_reports_none(self) -> None:
        self.write_local()
        with mock.patch.dict(os.environ, {}, clear=True):
            state = credentials.delete_local_credentials(
                credentials_path=self.local_path
            )
        self.assertFalse(self.local_path.exists())
        self.assertEqual(state["source"], "none")
        self.assertFalse(state["has_local"])
        self.assertFalse(state["env_present"])
        self.assertEqual(state["key_masked"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)