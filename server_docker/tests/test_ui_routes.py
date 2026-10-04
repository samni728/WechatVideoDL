import base64
import unittest
from pathlib import Path

import server_docker.app as appmod


class UiRouteTests(unittest.TestCase):
    def setUp(self):
        self.old_user, self.old_pass = appmod.WEBUI_USERNAME, appmod.WEBUI_PASSWORD
        appmod.WEBUI_USERNAME, appmod.WEBUI_PASSWORD = "u", "p"
        appmod.app.config.update(TESTING=True)
        self.client = appmod.app.test_client()
        token = base64.b64encode(b"u:p").decode()
        self.headers = {"Authorization": f"Basic {token}"}

    def tearDown(self):
        appmod.WEBUI_USERNAME, appmod.WEBUI_PASSWORD = self.old_user, self.old_pass

    def test_login_uses_product_template(self):
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("WechatVideoDL", html)
        self.assertIn("/static/logo.svg", html)
        self.assertIn('name="username"', html)
        self.assertIn('name="password"', html)

    def test_authenticated_workspace_has_new_controls(self):
        r = self.client.get("/", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        for token in [
            'id="video-url"',
            'id="preset-select"',
            'id="prompt-input"',
            'id="output-format"',
            'value="md"',
            'id="download-mp4"',
            'id="parse-button"',
            'id="result-panel"',
            'data-page="workspace"',
        ]:
            self.assertIn(token, html)
        self.assertIn('href="/history"', html)

    def test_history_page_exists_and_has_history_root(self):
        r = self.client.get("/history", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('data-page="history"', html)
        self.assertIn('id="history-list"', html)
        self.assertIn("历史记录", html)

    def test_static_assets_available(self):
        for path, marker in [
            ("/static/app.css", "--accent"),
            ("/static/app.js", "safeFetch"),
            ("/static/logo.svg", "<svg"),
        ]:
            r = self.client.get(path)
            try:
                self.assertEqual(r.status_code, 200, path)
                self.assertIn(marker, r.get_data(as_text=True))
            finally:
                r.close()

    def test_legacy_inline_html_constants_are_removed(self):
        self.assertFalse(hasattr(appmod, "INDEX_HTML"))
        self.assertFalse(hasattr(appmod, "LOGIN_HTML"))


if __name__ == "__main__":
    unittest.main()
