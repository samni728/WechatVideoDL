import tempfile
import unittest
from pathlib import Path

from server_docker.history_store import HistoryStore
from server_docker.job_runner import is_retryable_error, run_parse_job


class FakeError(RuntimeError):
    def __init__(self, code, message="boom", status=502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class JobRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.downloads = root / "downloads"
        self.store = HistoryStore(root / "history.db")
        self.job_id = "wxv_000101"
        self.store.create_job(
            id=self.job_id,
            input_url="https://weixin.qq.com/sph/demo",
            prompt="summary",
            preset_id=None,
            preset_name=None,
            output_format="md",
            download_requested=True,
            max_attempts=3,
        )

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def success_analysis():
        return {
            "content": "result",
            "previewUrl": "https://channels.weixin.qq.com/finder-preview/demo",
            "directUrl": "https://finder.video.qq.com/demo.mp4",
        }

    def downloader(self, job_id, direct_url, referer):
        self.downloads.mkdir(parents=True, exist_ok=True)
        p = self.downloads / f"{job_id}.mp4"
        p.write_bytes(b"video")
        return p

    def _run_job(self, analyzer, downloader=None, sleeper=None):
        return run_parse_job(
            self.job_id,
            "https://weixin.qq.com/sph/demo",
            "summary",
            None,
            "md",
            True,
            store=self.store,
            download_dir=self.downloads,
            analyzer=analyzer,
            downloader=downloader or self.downloader,
            max_attempts=3,
            sleeper=sleeper or (lambda _: None),
        )

    def test_retryable_success_on_second_attempt(self):
        calls = []
        def analyzer(url, prompt):
            calls.append(1)
            if len(calls) == 1:
                raise FakeError("BROWSERLESS_UNREACHABLE")
            return self.success_analysis()

        row = self._run_job(analyzer)
        self.assertEqual(len(calls), 2)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["attempts_used"], 2)
        self.assertEqual(len(row["retry_errors"]), 1)
        self.assertEqual(row["retry_errors"][0]["code"], "BROWSERLESS_UNREACHABLE")
        self.assertEqual(row["text_filename"], f"{self.job_id}.md")
        self.assertEqual(row["video_filename"], f"{self.job_id}.mp4")

    def test_retryable_success_on_third_attempt_and_backoff(self):
        calls, sleeps = [], []
        def analyzer(url, prompt):
            calls.append(1)
            if len(calls) < 3:
                raise FakeError("VIDEO_URL_MISSING")
            return self.success_analysis()

        row = self._run_job(analyzer, sleeper=sleeps.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(row["attempts_used"], 3)
        self.assertEqual(row["status"], "completed")

    def test_non_retryable_error_stops_immediately(self):
        calls = []
        def analyzer(url, prompt):
            calls.append(1)
            raise FakeError("YUANBAO_LOGIN_REQUIRED", status=401)

        row = self._run_job(analyzer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts_used"], 1)
        self.assertEqual(row["error_code"], "YUANBAO_LOGIN_REQUIRED")

    def test_final_transient_failure_persists_one_row(self):
        def analyzer(url, prompt):
            raise FakeError("BROWSERLESS_ERROR")

        row = self._run_job(analyzer)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts_used"], 3)
        self.assertEqual(len(row["retry_errors"]), 3)
        self.assertEqual(len(self.store.list_jobs()), 1)

    def test_partial_artifacts_are_removed_before_retry(self):
        downloads = []
        def downloader(job_id, direct_url, referer):
            self.downloads.mkdir(parents=True, exist_ok=True)
            partial = self.downloads / f"{job_id}.mp4"
            partial.write_bytes(b"partial")
            downloads.append(partial)
            if len(downloads) == 1:
                raise FakeError("DOWNLOAD_FAILED")
            partial.write_bytes(b"final")
            return partial

        row = self._run_job(lambda *_: self.success_analysis(), downloader=downloader)
        self.assertEqual(row["status"], "completed")
        self.assertEqual((self.downloads / f"{self.job_id}.mp4").read_bytes(), b"final")
        self.assertTrue((self.downloads / f"{self.job_id}.md").exists())

    def test_retry_attempts_are_capped_at_three(self):
        calls = []
        def analyzer(url, prompt):
            calls.append(1)
            raise FakeError("BROWSERLESS_ERROR")

        row = run_parse_job(
            self.job_id,
            "https://weixin.qq.com/sph/demo",
            "summary",
            None,
            "md",
            True,
            store=self.store,
            download_dir=self.downloads,
            analyzer=analyzer,
            downloader=self.downloader,
            max_attempts=10,
            sleeper=lambda _: None,
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(row["attempts_used"], 3)
        self.assertEqual(row["max_attempts"], 3)

    def test_retry_classifier(self):
        self.assertTrue(is_retryable_error(FakeError("BROWSERLESS_UNREACHABLE")))
        self.assertTrue(is_retryable_error(FakeError("DOWNLOAD_TIMEOUT", status=504)))
        self.assertFalse(is_retryable_error(FakeError("INVALID_URL", status=400)))
        self.assertFalse(is_retryable_error(FakeError("YUANBAO_LOGIN_REQUIRED", status=401)))


if __name__ == "__main__":
    unittest.main()
