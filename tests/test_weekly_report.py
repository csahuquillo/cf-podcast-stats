import importlib
from io import BytesIO
import os
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from unittest.mock import patch


# Prevent tests from reading any developer .env file during module import.
os.environ["CFPS_ENV"] = "/path/that/does/not/exist/cfps-test.env"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
report = importlib.import_module("weekly_report")


class CloudflareTokenTests(unittest.TestCase):
    def test_prefers_configured_in_memory_token(self):
        report.CF_API_TOKEN = "in-memory-test-token"
        report.CF_API_TOKEN_SSM_PARAMETER = "/qpg/automations/test/token"
        with patch.object(report.subprocess, "run") as run:
            self.assertEqual(report._load_cloudflare_api_token(), "in-memory-test-token")
        run.assert_not_called()

    def test_reads_ssm_secure_string_without_logging_value(self):
        report.CF_API_TOKEN = ""
        report.CF_API_TOKEN_SSM_PARAMETER = "/qpg/automations/test/token"
        report.CFPS_AWS_REGION = "eu-west-3"
        with patch.object(report.subprocess, "run", return_value=CompletedProcess([], 0, "fixture-token\n", "")) as run:
            self.assertEqual(report._load_cloudflare_api_token(), "fixture-token")
        args = run.call_args.args[0]
        self.assertIn("--with-decryption", args)
        self.assertEqual(args[args.index("--name") + 1], "/qpg/automations/test/token")
        self.assertNotIn("fixture-token", args)

    def test_redacts_ssm_cli_error_output(self):
        report.CF_API_TOKEN = ""
        report.CF_API_TOKEN_SSM_PARAMETER = "/qpg/automations/test/token"
        failure = CalledProcessError(1, ["aws"], output="fixture-secret", stderr="fixture-secret")
        with patch.object(report.subprocess, "run", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "could not retrieve") as raised:
                report._load_cloudflare_api_token()
        self.assertNotIn("fixture-secret", str(raised.exception))


class R2ReportTests(unittest.TestCase):
    def test_episode_titles_uses_a_descriptive_user_agent(self):
        report.FEED_URL = "https://example.test/feed.xml"
        xml = '<rss><channel><item><title>Episode 117</title><enclosure url="https://podcast.test/episodes/117-test.mp3" /></item></channel></rss>'
        with patch.object(report.urllib.request, "urlopen", return_value=BytesIO(xml.encode())) as urlopen:
            titles = report.episode_titles()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), "cf-podcast-stats/1.0 (+https://github.com/csahuquillo/cf-podcast-stats)")
        self.assertEqual(titles, {"117-test": "Episode 117"})

    def test_aggregates_request_counts_by_episode(self):
        report.CF_API_TOKEN = "test-token"
        report.CF_API_TOKEN_SSM_PARAMETER = ""
        report.CF_ACCOUNT = "a" * 32
        report.R2_BUCKET = "sahuquillo-media"
        report.R2_DATASET = "r2"
        report.WINDOW_DAYS = 7
        response = {"success": True, "result": {"calculations": [{"aggregates": [{
            "groups": [{"key": "request.uri", "value": "/episodes/117-test.mp3"}],
            "value": 4,
        }]}]}}
        with patch.object(report, "_cf_request", return_value=response) as request:
            counts, total = report.fetch_r2_aggregates(datetime.now(timezone.utc) - timedelta(days=7))
        self.assertEqual(counts, Counter({"117-test": 4}))
        self.assertEqual(total, 4)
        self.assertEqual(request.call_args.kwargs["token"], "test-token")

    def test_r2_dry_run_neither_sends_email_nor_creates_state_dir(self):
        with tempfile.TemporaryDirectory() as parent:
            state_dir = Path(parent) / "not-created"
            report.SOURCE = "r2"
            report.STATE_DIR = state_dir
            with patch.object(report, "fetch_r2_aggregates", return_value=(Counter(), 0)), \
                 patch.object(report, "episode_titles", return_value={}), \
                 patch.object(report, "send_email") as send_email, \
                 patch.object(sys, "argv", ["weekly_report.py", "--dry-run"]):
                report.main()
            send_email.assert_not_called()
            self.assertFalse(state_dir.exists())


if __name__ == "__main__":
    unittest.main()
