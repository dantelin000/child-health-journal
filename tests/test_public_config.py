import os
import unittest
from unittest.mock import patch

from app import create_app


class PublicConfigTests(unittest.TestCase):
    def test_default_hosts_are_limited_to_localhost(self):
        with patch.dict(os.environ, {}, clear=True):
            app = create_app(
                {"TESTING": True, "SECRET_KEY": b"test-key-with-32-bytes-exactly-1234"}
            )
        self.assertEqual(app.config["TRUSTED_HOSTS"], ["localhost", "127.0.0.1"])

    def test_allowed_hosts_are_configurable(self):
        with patch.dict(
            os.environ, {"HEALTH_TRUSTED_HOSTS": "health.example.org, other.example.org"}
        ):
            app = create_app(
                {"TESTING": True, "SECRET_KEY": b"test-key-with-32-bytes-exactly-1234"}
            )
        self.assertEqual(app.config["TRUSTED_HOSTS"], ["health.example.org", "other.example.org"])

    def test_empty_host_configuration_fails_closed(self):
        with (
            patch.dict(os.environ, {"HEALTH_TRUSTED_HOSTS": " , "}),
            self.assertRaises(RuntimeError),
        ):
            create_app({"TESTING": True, "SECRET_KEY": b"test-key-with-32-bytes-exactly-1234"})
