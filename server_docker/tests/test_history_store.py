import json
import tempfile
import unittest
from pathlib import Path

from server_docker.history_store import HistoryStore


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "history.db"
        self.store = HistoryStore(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_and_persistence_and_newest_first(self):
        self.store.create_job(
            id="wxv_000001",
            input_url="https://weixin.qq.com/sph/one",
            prompt="first",
            preset_id=None,
            preset_name=None,
            output_format="txt",
            download_requested=True,
            max_attempts=3,
        )
        self.store.create_job(
            id="wxv_000002",
            input_url="https://weixin.qq.com/sph/two",
            prompt="second",
            preset_id="knowledge",
            preset_name="知识库标签与元数据",
            output_format="md",
            download_requested=False,
            max_attempts=3,
        )
        self.store.update_job(
            "wxv_000002",
            status="completed",
            attempts_used=2,
            retry_errors=[{"attempt": 1, "code": "TEMP", "message": "temporary"}],
            yuanbao_content="done",
        )

        rows = HistoryStore(self.db).list_jobs()
        self.assertEqual([r["id"] for r in rows], ["wxv_000002", "wxv_000001"])
        row = HistoryStore(self.db).get_job("wxv_000002")
        self.assertEqual(row["retry_errors"], [{"attempt": 1, "code": "TEMP", "message": "temporary"}])
        self.assertEqual(row["output_format"], "md")
        self.assertFalse(row["download_requested"])

    def test_stale_running_jobs_are_marked_failed_on_startup(self):
        self.store.create_job(
            id="wxv_000003",
            input_url="https://weixin.qq.com/sph/three",
            prompt=None,
            preset_id=None,
            preset_name=None,
            output_format="txt",
            download_requested=True,
            max_attempts=3,
        )
        self.store.update_job("wxv_000003", status="running", attempts_used=1)

        reopened = HistoryStore(self.db)
        row = reopened.get_job("wxv_000003")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "INTERRUPTED")
        self.assertIn("重启", row["error_message"])

    def test_update_rejects_unknown_columns(self):
        self.store.create_job(
            id="wxv_000004",
            input_url="https://weixin.qq.com/sph/four",
            prompt=None,
            preset_id=None,
            preset_name=None,
            output_format="txt",
            download_requested=True,
            max_attempts=3,
        )
        with self.assertRaises(ValueError):
            self.store.update_job("wxv_000004", arbitrary_path="/tmp/pwn")

    def test_delete_returns_record_and_removes_it(self):
        self.store.create_job(
            id="wxv_000005",
            input_url="https://weixin.qq.com/sph/five",
            prompt=None,
            preset_id=None,
            preset_name=None,
            output_format="txt",
            download_requested=True,
            max_attempts=3,
        )
        deleted = self.store.delete_job("wxv_000005")
        self.assertEqual(deleted["id"], "wxv_000005")
        self.assertIsNone(self.store.get_job("wxv_000005"))


if __name__ == "__main__":
    unittest.main()
