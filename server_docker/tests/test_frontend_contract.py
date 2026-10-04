import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTests(unittest.TestCase):
    def test_frontend_has_three_theme_modes_and_persists_preference(self):
        base = (ROOT / "templates" / "base.html").read_text("utf-8")
        js = (ROOT / "static" / "app.js").read_text("utf-8")
        for theme in ["light", "dark", "system"]:
            self.assertIn(f'value="{theme}"', base)
        self.assertIn("localStorage", js)
        self.assertIn("wvd-theme", js)

    def test_fetch_helper_checks_content_type_before_json_parse(self):
        js = (ROOT / "static" / "app.js").read_text("utf-8")
        self.assertIn("async function safeFetch", js)
        self.assertIn("content-type", js.lower())
        self.assertIn("response.text()", js)
        self.assertIn("JSON.parse", js)
        self.assertIn("服务器返回了非 JSON 错误", js)
        self.assertNotIn("await response.json()", js)

    def test_preset_prompt_is_editable_and_markdown_is_web_default(self):
        index = (ROOT / "templates" / "index.html").read_text("utf-8")
        js = (ROOT / "static" / "app.js").read_text("utf-8")
        self.assertIn('id="preset-select"', index)
        self.assertIn('id="prompt-input"', index)
        self.assertIn('<option value="md" selected>', index)
        self.assertIn('routePath("api/presets")', js)
        self.assertIn("promptInput.value", js)

    def test_history_supports_download_detail_and_confirmed_delete(self):
        js = (ROOT / "static" / "app.js").read_text("utf-8")
        self.assertIn('routePath("api/history")', js)
        self.assertIn("window.confirm", js)
        self.assertIn("method: \"DELETE\"", js)
        self.assertIn("download_path", js)

    def test_frontend_routes_respect_reverse_proxy_prefix(self):
        base = (ROOT / "templates" / "base.html").read_text("utf-8")
        js = (ROOT / "static" / "app.js").read_text("utf-8")
        self.assertIn('data-base-path="{{ request.script_root }}"', base)
        self.assertIn("url_for('history_page')", base)
        self.assertIn("function routePath", js)
        self.assertIn('routePath("api/presets")', js)
        self.assertIn('routePath("api/history")', js)
        self.assertNotIn('safeFetch("/api/', js)
        self.assertNotIn('safeFetch(`/api/', js)

    def test_css_has_light_dark_and_responsive_layout(self):
        css = (ROOT / "static" / "app.css").read_text("utf-8")
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertIn(':root[data-theme="light"]', css)
        self.assertIn("@media", css)
        self.assertIn(".workspace-grid", css)


if __name__ == "__main__":
    unittest.main()
