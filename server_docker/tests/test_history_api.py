import base64
import tempfile
import unittest
from pathlib import Path

import server_docker.app as appmod
from server_docker.history_store import HistoryStore


class HistoryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.downloads = root / "downloads"
        self.downloads.mkdir()
        self.old = {}
        for name, value in {
            "DATA_DIR": root,
            "DOWNLOAD_DIR": self.downloads,
            "PUBLIC_BASE_URL": "",
            "TRUST_PROXY_HEADERS": False,
            "WEBUI_USERNAME": "u",
            "WEBUI_PASSWORD": "p",
        }.items():
            self.old[name] = getattr(appmod, name, None)
            setattr(appmod, name, value)
        self.old_store = getattr(appmod, "HISTORY_STORE", None)
        self.store = HistoryStore(root / "history.db")
        appmod.HISTORY_STORE = self.store
        appmod.app.config.update(TESTING=True)
        self.client = appmod.app.test_client()
        token = base64.b64encode(b"u:p").decode()
        self.headers = {"Authorization": f"Basic {token}"}

    def tearDown(self):
        appmod.HISTORY_STORE = self.old_store
        for name, value in self.old.items():
            setattr(appmod, name, value)
        self.tmp.cleanup()

    def create(self, job_id, *, fmt="md", status="completed"):
        self.store.create_job(
            id=job_id,
            input_url=f"https://weixin.qq.com/sph/{job_id}",
            prompt="prompt",
            preset_id=None,
            preset_name=None,
            output_format=fmt,
            download_requested=True,
            max_attempts=3,
        )
        self.store.update_job(job_id, status=status, yuanbao_content=f"content {job_id}")

    def test_history_requires_auth(self):
        self.assertEqual(self.client.get("/api/history").status_code, 401)

    def test_list_is_newest_first_and_detail_has_content(self):
        self.create("wxv_000201")
        self.create("wxv_000202")
        r = self.client.get("/api/history", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual([j["id"] for j in body["jobs"]], ["wxv_000202", "wxv_000201"])
        detail = self.client.get("/api/history/wxv_000202", headers=self.headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()["job"]["content"], "content wxv_000202")

    def test_detail_missing_returns_404(self):
        r = self.client.get("/api/history/wxv_999999", headers=self.headers)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "NOT_FOUND")

    def test_delete_removes_owned_files_and_record_and_tolerates_missing_optional_file(self):
        self.create("wxv_000203")
        self.create("wxv_000204")
        text = self.downloads / "wxv_000203.md"
        text.write_text("hello", "utf-8")
        # video filename is recorded but file is intentionally missing
        self.store.update_job(
            "wxv_000203",
            text_filename=text.name,
            text_bytes=text.stat().st_size,
            video_filename="wxv_000203.mp4",
            video_bytes=123,
        )
        neighbor = self.downloads / "wxv_000204.md"
        neighbor.write_text("neighbor", "utf-8")
        self.store.update_job("wxv_000204", text_filename=neighbor.name, text_bytes=neighbor.stat().st_size)

        r = self.client.delete("/api/history/wxv_000203", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertFalse(text.exists())
        self.assertIsNone(self.store.get_job("wxv_000203"))
        self.assertIsNotNone(self.store.get_job("wxv_000204"))
        self.assertTrue(neighbor.exists())

    def test_delete_rejects_malformed_id_and_cannot_delete_arbitrary_path(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("safe", "utf-8")
        self.create("wxv_000205")
        self.store.update_job("wxv_000205", text_filename="../../outside.txt")

        bad = self.client.delete("/api/history/not-valid", headers=self.headers)
        self.assertEqual(bad.status_code, 400)
        ok = self.client.delete("/api/history/wxv_000205", headers=self.headers)
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
