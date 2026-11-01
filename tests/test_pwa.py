import json
import struct
import unittest
import zlib
from unittest.mock import patch

import test_health as baseline


class PwaTests(unittest.TestCase):
    setUp = baseline.HealthTests.setUp
    get = baseline.HealthTests.get
    post = baseline.HealthTests.post
    login = baseline.HealthTests.login

    def test_manifest_and_icons_are_public_and_installable(self):
        login = self.get("/login")
        html = login.get_data(as_text=True)
        self.assertIn('rel="manifest" href="/manifest.webmanifest"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn("/static/pwa.js", html)
        for directive in ["manifest-src 'self'", "worker-src 'self'", "img-src 'self'"]:
            self.assertIn(directive, login.headers["Content-Security-Policy"])
        response = self.get("/manifest.webmanifest")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/manifest+json")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        manifest = json.loads(response.data)
        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["name"], "孩子健康记录")
        self.assertEqual({icon["sizes"] for icon in manifest["icons"]}, {"192x192", "512x512"})
        for icon in manifest["icons"]:
            self.assertIn("maskable", icon["purpose"].split())
            image = self.get(icon["src"])
            self.addCleanup(image.close)
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.mimetype, "image/png")
            self.assertEqual(image.headers["Cache-Control"], "no-store")
            self.assertEqual(image.data[:8], b"\x89PNG\r\n\x1a\n")
            width, height = struct.unpack(">II", image.data[16:24])
            self.assertEqual(f"{width}x{height}", icon["sizes"])
            offset, payload = 8, bytearray()
            while offset < len(image.data):
                length = struct.unpack(">I", image.data[offset : offset + 4])[0]
                kind = image.data[offset + 4 : offset + 8]
                data = image.data[offset + 8 : offset + 8 + length]
                checksum = struct.unpack(
                    ">I", image.data[offset + 8 + length : offset + 12 + length]
                )[0]
                self.assertEqual(checksum, zlib.crc32(kind + data))
                if kind == b"IDAT":
                    payload.extend(data)
                offset += 12 + length
            pixels = zlib.decompress(payload)
            self.assertEqual(len(pixels), height * (1 + width * 4))
            self.assertTrue(all(pixels[row * (1 + width * 4)] == 0 for row in range(height)))
            self.assertTrue(
                all(
                    pixels[row * (1 + width * 4) + 1 + col * 4 + 3] == 255
                    for row in range(height)
                    for col in range(width)
                )
            )
        apple = self.get("/static/icons/apple-touch-icon.png")
        self.addCleanup(apple.close)
        self.assertEqual(struct.unpack(">II", apple.data[16:24]), (180, 180))

    def test_worker_scope_and_metadata_bypass_session_database(self):
        self.login()
        with patch("auth.get_db", side_effect=AssertionError("Metadata accessed session database")):
            worker = self.get("/sw.js")
            manifest = self.get("/manifest.webmanifest")
            icon = self.get("/static/icons/icon-192.png")
        self.addCleanup(worker.close)
        self.addCleanup(manifest.close)
        self.addCleanup(icon.close)
        self.assertEqual(worker.status_code, 200)
        self.assertEqual(worker.mimetype, "application/javascript")
        self.assertEqual(worker.headers["Service-Worker-Allowed"], "/")
        self.assertEqual(worker.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/sw.js", base_url="https://evil.test").status_code, 400)
        self.assertEqual(
            self.client.get("/manifest.webmanifest", base_url="https://evil.test").status_code, 400
        )
