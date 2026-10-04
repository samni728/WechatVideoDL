import base64
import tempfile
import unittest
from pathlib import Path

import server_docker.app as appmod
from server_docker.history_store import HistoryStore


class ParseApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.downloads = root / "downloads"
        self.downloads.mkdir()
        self.old = {}
        for name, value in {
            "DATA_DIR": root,
            "DOWNLOAD_DIR": self.downloads,
            "SEQUENCE_FILE": root / "sequence.txt",
            "PUBLIC_BASE_URL": "",
            "TRUST_PROXY_HEADERS": False,
            "WEBUI_USERNAME": "tester",
            "WEBUI_PASSWORD": "secret",
            "MAX_PARSE_ATTEMPTS": 3,
        }.items():
            self.old[name] = getattr(appmod, name, None)
            setattr(appmod, name, value)
        self.old_store = getattr(appmod, "HISTORY_STORE", None)
        appmod.HISTORY_STORE = HistoryStore(root / "history.db")
        self.old_analyzer = appmod.analyze_with_yuanbao
        self.old_downloader = appmod.download_video
        appmod.analyze_with_yuanbao = lambda *_: {
            "content": "analysis body",
            "previewUrl": "https://channels.weixin.qq.com/finder-preview/demo",
            "directUrl": "https://finder.video.qq.com/demo.mp4",
            "card": {"title": "Demo"},
        }
        def downloader(job_id, direct_url, referer):
            path = self.downloads / f"{job_id}.mp4"
            path.write_bytes(b"video")
            return path
        appmod.download_video = downloader
        appmod.app.config.update(TESTING=True)
        self.client = appmod.app.test_client()

    def tearDown(self):
        appmod.analyze_with_yuanbao = self.old_analyzer
        appmod.download_video = self.old_downloader
        appmod.HISTORY_STORE = self.old_store
        for name, value in self.old.items():
            setattr(appmod, name, value)
        self.tmp.cleanup()

    def auth(self):
        token = base64.b64encode(b"tester:secret").decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def post(self, payload):
        return self.client.post("/api/parse", json=payload, headers=self.auth())

    def test_omitted_output_format_defaults_to_txt(self):
        r = self.post({"url": "https://weixin.qq.com/sph/demo", "download": False})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["text"]["filename"].endswith(".txt"))
        self.assertEqual(body["output_format"], "txt")

    def test_markdown_output_and_retry_metadata_and_proxy_safe_links(self):
        r = self.post({
            "url": "https://weixin.qq.com/sph/demo",
            "prompt": "总结",
            "output_format": "md",
            "download": True,
        })
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["attempts_used"], 1)
        self.assertEqual(body["max_attempts"], 3)
        self.assertEqual(body["retry_errors"], [])
        self.assertTrue(body["text"]["filename"].endswith(".md"))
        self.assertRegex(body["text"]["download_path"], r"^/files/wxv_\d{6}\.md$")
        self.assertTrue(body["text"]["download_url"].startswith("http://localhost/files/"))
        self.assertRegex(body["video"]["download_path"], r"^/files/wxv_\d{6}\.mp4$")

    def test_invalid_output_format_returns_json_400_without_analyzer(self):
        calls = []
        appmod.analyze_with_yuanbao = lambda *_: calls.append(1)
        r = self.post({
            "url": "https://weixin.qq.com/sph/demo",
            "output_format": "html",
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.content_type, "application/json")
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "INVALID_OUTPUT_FORMAT")
        self.assertEqual(calls, [])

    def test_malformed_json_returns_json_400(self):
        r = self.client.post("/api/parse", data="{bad", headers=self.auth())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.content_type, "application/json")
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "BAD_REQUEST")

    def test_next_id_recovers_from_text_only_artifacts(self):
        (self.downloads / "wxv_000042.md").write_text("old", encoding="utf-8")
        if appmod.SEQUENCE_FILE.exists():
            appmod.SEQUENCE_FILE.unlink()
        self.assertEqual(appmod.next_id(), "wxv_000043")

    def test_failed_job_returns_json_with_attempt_metadata(self):
        class UpstreamError(RuntimeError):
            code = "BROWSERLESS_UNREACHABLE"
            status = 503
            message = "temporarily unavailable"
        appmod.analyze_with_yuanbao = lambda *_: (_ for _ in ()).throw(UpstreamError())
        old_sleep = getattr(appmod, "RETRY_SLEEPER", None)
        appmod.RETRY_SLEEPER = lambda _: None
        try:
            r = self.post({"url": "https://weixin.qq.com/sph/demo", "download": False})
        finally:
            appmod.RETRY_SLEEPER = old_sleep
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.content_type, "application/json")
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["job"]["attempts_used"], 3)
        self.assertEqual(len(body["job"]["retry_errors"]), 3)
        self.assertEqual(body["error"]["code"], "BROWSERLESS_UNREACHABLE")


if __name__ == "__main__":
    unittest.main()
