import base64
import unittest

import server_docker.app as appmod
from server_docker.presets import PRESETS, get_preset


class PresetTests(unittest.TestCase):
    def test_exactly_five_curated_presets_with_stable_ids(self):
        self.assertEqual(len(PRESETS), 5)
        self.assertEqual(
            [p["id"] for p in PRESETS],
            ["script_copy", "engineering_framework", "knowledge_tags", "marketing_breakdown", "knowledge_notes"],
        )
        for preset in PRESETS:
            self.assertTrue(preset["name"].strip())
            self.assertGreater(len(preset["prompt"].strip()), 20)

    def test_get_preset_returns_copy_or_none(self):
        preset = get_preset("knowledge_tags")
        self.assertEqual(preset["id"], "knowledge_tags")
        preset["name"] = "changed"
        self.assertNotEqual(get_preset("knowledge_tags")["name"], "changed")
        self.assertIsNone(get_preset("does-not-exist"))
        self.assertIsNone(get_preset(None))

    def test_presets_api_requires_auth_and_returns_five(self):
        old_user, old_pass = appmod.WEBUI_USERNAME, appmod.WEBUI_PASSWORD
        appmod.WEBUI_USERNAME, appmod.WEBUI_PASSWORD = "u", "p"
        client = appmod.app.test_client()
        try:
            unauthorized = client.get("/api/presets")
            self.assertEqual(unauthorized.status_code, 401)
            token = base64.b64encode(b"u:p").decode()
            response = client.get("/api/presets", headers={"Authorization": f"Basic {token}"})
        finally:
            appmod.WEBUI_USERNAME, appmod.WEBUI_PASSWORD = old_user, old_pass
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["presets"]), 5)


if __name__ == "__main__":
    unittest.main()
