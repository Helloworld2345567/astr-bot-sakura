"""Portable defaults and configuration validation."""
import json
import os
import unittest
from unittest.mock import patch

from support import ROOT, TEST_ADMIN_ID, permissions, plugin
from sakura_plugin_under_test.sakura.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_schema_defaults_construct_valid_settings(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        settings = Settings.from_config({key: value["default"] for key, value in schema.items()})
        self.assertEqual(settings, Settings())
        self.assertFalse(settings.admin_user_ids)

    def test_configurable_provider_persona_and_limits(self):
        settings = Settings.from_config({"image_base_url": "https://images.example.org/v1", "image_model": "deployment-model", "persona_id": "我的小樱", "merge_window_seconds": 3, "admin_user_ids": [TEST_ADMIN_ID]})
        self.assertEqual(settings.persona_id, "我的小樱")
        self.assertEqual(settings.permissions_config()["admin_user_ids"], [TEST_ADMIN_ID])
        self.assertEqual(settings.merge_window_seconds, 3)

    def test_unsafe_url_and_invalid_values_rejected(self):
        for config in (
            {"image_base_url": "http://images.example.org"},
            {"image_base_url": "https://" + "user:password" + "@images.example.org"},
            {"image_base_url": "https://images.example.org?api_key=example"},
            {"image_api_key_env": "a;command"},
            {"merge_window_seconds": 0},
            {"merge_window_seconds": float("nan")},
            {"reference_ttl_seconds": True},
            {"admin_user_ids": "test-owner"},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                Settings.from_config(config)

    def test_no_implicit_admin_or_borrowed_model_key(self):
        self.assertFalse(permissions.PermissionManager().admin_user_ids)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}, clear=True):
            with self.assertRaises(RuntimeError):
                plugin._image_api_key_env()

    def test_explicit_key_environment_override(self):
        with patch.dict(os.environ, {"SAKURA_IMAGE_API_KEY_ENV": "TEST_IMAGE_KEY", "TEST_IMAGE_KEY": "test-only"}, clear=True):
            self.assertEqual(plugin._image_api_key_env(), "TEST_IMAGE_KEY")
