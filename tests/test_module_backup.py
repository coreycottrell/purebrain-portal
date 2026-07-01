"""
Module Backup/Restore/Rebuild API Tests (TDD)

Tests for:
  - GET /api/mods/health — module health check
  - POST /api/mods/backup — backup all modules
  - POST /api/mods/restore/{module_name} — restore single module
  - POST /api/mods/restore-all — full rebuild from backup

All endpoints require Bearer auth.
"""

import unittest
import requests
import os
import sys
import json
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import BASE_URL, PORTAL_DIR, load_token

TOKEN = load_token()
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}
BACKUP_DIR = os.path.join(PORTAL_DIR, ".module-backup")


class TestModuleHealthEndpoint(unittest.TestCase):
    """GET /api/mods/health"""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    def test_health_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/mods/health", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_health_returns_200(self):
        resp = requests.get(f"{BASE_URL}/api/mods/health", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)

    def test_health_returns_module_list(self):
        resp = requests.get(f"{BASE_URL}/api/mods/health", headers=AUTH_HEADERS, timeout=5)
        data = resp.json()
        self.assertIn("modules", data)
        self.assertIsInstance(data["modules"], list)
        self.assertGreater(len(data["modules"]), 0)

    def test_health_module_has_required_fields(self):
        resp = requests.get(f"{BASE_URL}/api/mods/health", headers=AUTH_HEADERS, timeout=5)
        data = resp.json()
        mod = data["modules"][0]
        for key in ("name", "file", "status", "size", "type"):
            self.assertIn(key, mod, f"Missing field: {key}")

    def test_health_statuses_are_valid(self):
        resp = requests.get(f"{BASE_URL}/api/mods/health", headers=AUTH_HEADERS, timeout=5)
        data = resp.json()
        valid = {"ok", "missing", "corrupted", "no_backup"}
        for mod in data["modules"]:
            self.assertIn(mod["status"], valid, f"Bad status for {mod['name']}: {mod['status']}")

    def test_health_has_summary(self):
        resp = requests.get(f"{BASE_URL}/api/mods/health", headers=AUTH_HEADERS, timeout=5)
        data = resp.json()
        self.assertIn("summary", data)
        self.assertIn("total", data["summary"])
        self.assertIn("ok", data["summary"])


class TestModuleBackupEndpoint(unittest.TestCase):
    """POST /api/mods/backup"""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    def test_backup_requires_auth(self):
        resp = requests.post(f"{BASE_URL}/api/mods/backup", timeout=10)
        self.assertEqual(resp.status_code, 401)

    def test_backup_returns_200(self):
        resp = requests.post(f"{BASE_URL}/api/mods/backup", headers=AUTH_HEADERS, timeout=10)
        self.assertEqual(resp.status_code, 200)

    def test_backup_returns_count(self):
        resp = requests.post(f"{BASE_URL}/api/mods/backup", headers=AUTH_HEADERS, timeout=10)
        data = resp.json()
        self.assertIn("backed_up", data)
        self.assertGreater(data["backed_up"], 0)

    def test_backup_creates_manifest(self):
        requests.post(f"{BASE_URL}/api/mods/backup", headers=AUTH_HEADERS, timeout=10)
        manifest_path = os.path.join(BACKUP_DIR, "manifest.json")
        self.assertTrue(os.path.exists(manifest_path), "manifest.json not created")
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.assertIn("modules", manifest)
        self.assertIn("created_at", manifest)

    def test_backup_files_exist_on_disk(self):
        requests.post(f"{BASE_URL}/api/mods/backup", headers=AUTH_HEADERS, timeout=10)
        # Check that at least auth.js backup exists
        backup_file = os.path.join(BACKUP_DIR, "static", "js", "core", "auth.js")
        self.assertTrue(os.path.exists(backup_file), f"Backup file not found: {backup_file}")


class TestModuleRestoreSingleEndpoint(unittest.TestCase):
    """POST /api/mods/restore/{module_name}"""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")
        # Ensure backup exists
        requests.post(f"{BASE_URL}/api/mods/backup", headers=AUTH_HEADERS, timeout=10)

    def test_restore_requires_auth(self):
        resp = requests.post(f"{BASE_URL}/api/mods/restore/auth.js", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_restore_known_module(self):
        resp = requests.post(
            f"{BASE_URL}/api/mods/restore/auth.js", headers=AUTH_HEADERS, timeout=10
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("status"), "restored")

    def test_restore_unknown_module_returns_404(self):
        resp = requests.post(
            f"{BASE_URL}/api/mods/restore/nonexistent.js", headers=AUTH_HEADERS, timeout=5
        )
        self.assertEqual(resp.status_code, 404)

    def test_restore_fixes_corrupted_file(self):
        """Full round-trip: backup -> corrupt -> health detects -> restore fixes."""
        # 1. Backup
        requests.post(f"{BASE_URL}/api/mods/backup", headers=AUTH_HEADERS, timeout=10)

        # 2. Get original checksum from health
        health_before = requests.get(
            f"{BASE_URL}/api/mods/health", headers=AUTH_HEADERS, timeout=5
        ).json()
        settings_mod = None
        for m in health_before["modules"]:
            if m["name"] == "settings.js":
                settings_mod = m
                break
        self.assertIsNotNone(settings_mod, "settings.js not in module list")
        original_checksum = settings_mod.get("checksum")

        # 3. Corrupt the file by appending junk
        settings_path = os.path.join(PORTAL_DIR, "static", "js", "features", "settings.js")
        with open(settings_path, "a") as f:
            f.write("\n// CORRUPTION TEST MARKER")

        try:
            # 4. Health should detect corruption
            health_after = requests.get(
                f"{BASE_URL}/api/mods/health", headers=AUTH_HEADERS, timeout=5
            ).json()
            corrupted_mod = None
            for m in health_after["modules"]:
                if m["name"] == "settings.js":
                    corrupted_mod = m
                    break
            self.assertEqual(corrupted_mod["status"], "corrupted")

            # 5. Restore it
            restore_resp = requests.post(
                f"{BASE_URL}/api/mods/restore/settings.js", headers=AUTH_HEADERS, timeout=10
            )
            self.assertEqual(restore_resp.status_code, 200)

            # 6. Health should show OK again
            health_fixed = requests.get(
                f"{BASE_URL}/api/mods/health", headers=AUTH_HEADERS, timeout=5
            ).json()
            fixed_mod = None
            for m in health_fixed["modules"]:
                if m["name"] == "settings.js":
                    fixed_mod = m
                    break
            self.assertEqual(fixed_mod["status"], "ok")
        finally:
            # Safety: always restore from backup in case test fails mid-way
            requests.post(
                f"{BASE_URL}/api/mods/restore/settings.js", headers=AUTH_HEADERS, timeout=10
            )


class TestModuleRestoreAllEndpoint(unittest.TestCase):
    """POST /api/mods/restore-all"""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")
        # Ensure backup exists
        requests.post(f"{BASE_URL}/api/mods/backup", headers=AUTH_HEADERS, timeout=10)

    def test_restore_all_requires_auth(self):
        resp = requests.post(f"{BASE_URL}/api/mods/restore-all", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_restore_all_returns_200(self):
        resp = requests.post(
            f"{BASE_URL}/api/mods/restore-all", headers=AUTH_HEADERS, timeout=15
        )
        self.assertEqual(resp.status_code, 200)

    def test_restore_all_returns_summary(self):
        resp = requests.post(
            f"{BASE_URL}/api/mods/restore-all", headers=AUTH_HEADERS, timeout=15
        )
        data = resp.json()
        self.assertIn("restored", data)
        self.assertIn("failed", data)
        self.assertGreater(data["restored"], 0)
        self.assertEqual(data["failed"], 0)


if __name__ == "__main__":
    unittest.main()
