import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "jmapx"

# 无扩展名可执行脚本不能直接 import；编译到独立模块，main 保护不会触发。
jmapx = types.ModuleType("jmapx_test_module")
jmapx.__file__ = str(SCRIPT)
sys.modules[jmapx.__name__] = jmapx
exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), jmapx.__dict__)


def make_context(**overrides):
    values = {
        "username": "user@example.com",
        "password": "secret",
        "session": {"downloadUrl": "https://mail.test/d/{accountId}/{blobId}/{name}?accept={type}"},
        "account_id": "account",
        "api_url": "https://mail.test/jmap/",
        "max_objects": 500,
        "max_calls": 16,
        "debug": False,
    }
    values.update(overrides)
    return jmapx.JmapContext(**values)


class ParsingTests(unittest.TestCase):
    def test_csv_and_stable_deduplication(self):
        self.assertEqual(jmapx.split_csv(" a, b ,,a "), ["a", "b", "a"])
        self.assertEqual(jmapx.unique_in_order(["a", "b", "a"]), ["a", "b"])
        jobs = [("blob1", "a"), ("blob1", "b"), ("blob2", "c")]
        self.assertEqual(jmapx.unique_jobs_by_blob_id(jobs), [jobs[0], jobs[2]])

    def test_datetime_with_explicit_timezone(self):
        self.assertEqual(
            jmapx.parse_datetime_arg("2026-08-21T13:00:00Z", False, "--start"),
            "2026-08-21T13:00:00Z",
        )
        self.assertEqual(
            jmapx.parse_datetime_arg("2026-08-21T21:00:00+08:00", False, "--start"),
            "2026-08-21T13:00:00Z",
        )
        with self.assertRaises(jmapx.JmapxError):
            jmapx.parse_datetime_arg("not-a-date", False, "--start")

    def test_address_parsing_and_matching(self):
        query = jmapx.parse_query_addr("Alice <ALICE@example.com>")
        self.assertTrue(jmapx.address_matches({"name": "Other", "email": "alice@example.com"}, query))
        self.assertFalse(jmapx.address_matches({"name": "Alice", "email": "bob@example.com"}, query))
        self.assertTrue(
            jmapx.email_matches(
                {"from": [{"name": "Alice", "email": "alice@example.com"}]},
                "from",
                query,
            )
        )

    def test_attachment_filter_syntax_and_partition(self):
        email = {
            "id": "e1",
            "subject": "发票",
            "from": [],
            "to": [],
            "attachments": [
                {"partId": "1", "name": "发票.PDF", "type": "application/pdf", "size": 1, "blobId": "b1"},
                {"partId": "2", "name": "发票.xml", "type": "text/xml", "size": 2, "blobId": "b2"},
            ],
        }
        pattern, invert = jmapx.parse_attachment_filter("*.pdf")
        result = jmapx.partition_attachments(email, pattern, invert)
        self.assertEqual([a["blobId"] for a in result["matched"]], ["b1"])
        self.assertEqual([a["blobId"] for a in result["unmatched"]], ["b2"])
        pattern, invert = jmapx.parse_attachment_filter("!*.pdf")
        result = jmapx.partition_attachments(email, pattern, invert)
        self.assertEqual([a["blobId"] for a in result["matched"]], ["b2"])
        with self.assertRaises(jmapx.JmapxError):
            jmapx.parse_attachment_filter("!")


class CredentialTests(unittest.TestCase):
    def write_credentials(self, directory, mode=0o600):
        path = pathlib.Path(directory) / "creds.json"
        path.write_text(
            json.dumps({"server": "mail.test", "username": "u", "password": "p"}),
            encoding="utf-8",
        )
        os.chmod(path, mode)
        return path

    def test_environment_has_priority(self):
        env = {"JMAP_SERVER": "mail.env", "JMAP_USERNAME": "u", "JMAP_PASSWORD": "p"}
        with mock.patch.dict(os.environ, env, clear=True):
            credentials, source = jmapx.resolve_credentials("missing.json")
        self.assertEqual(credentials["server"], "mail.env")
        self.assertIn("JMAP_SERVER", source)

    def test_file_requires_0600(self):
        if os.name == "nt":
            self.skipTest("Windows 无 POSIX 权限语义")
        with tempfile.TemporaryDirectory() as directory:
            valid = self.write_credentials(directory, 0o600)
            credentials, _ = jmapx.credentials_from_file(str(valid))
            self.assertEqual(credentials["username"], "u")
            os.chmod(valid, 0o644)
            with self.assertRaises(jmapx.JmapxError):
                jmapx.credentials_from_file(str(valid))


class JmapBatchTests(unittest.TestCase):
    def test_query_paginates_and_uses_server_limit(self):
        context = make_context()
        responses = [
            [["Email/query", {"ids": ["a", "b"], "total": 3, "limit": 2}, "q"]],
            [["Email/query", {"ids": ["c"], "limit": 2}, "q"]],
        ]
        with mock.patch.object(jmapx, "QUERY_PAGE_SIZE", 5), \
                mock.patch.object(jmapx, "jmap_post", side_effect=responses) as post:
            ids, total = jmapx.query_all_email_ids(context)
        self.assertEqual(ids, ["a", "b", "c"])
        self.assertEqual(total, 3)
        first_args = post.call_args_list[0].args[1][0][1]
        second_args = post.call_args_list[1].args[1][0][1]
        self.assertEqual(first_args["position"], 0)
        self.assertTrue(first_args["calculateTotal"])
        self.assertEqual(second_args["position"], 2)
        self.assertFalse(second_args["calculateTotal"])

    def test_8001_ids_use_17_calls_and_2_http_requests(self):
        context = make_context()
        request_sizes = []

        def fake_post(_context, calls, using=jmapx.USING):
            request_sizes.append(len(calls))
            return [
                ["Email/get", {"list": [], "notFound": arguments["ids"]}, call_id]
                for _, arguments, call_id in calls
            ]

        ids = [f"id-{index}" for index in range(8001)]
        with mock.patch.object(jmapx, "jmap_post", side_effect=fake_post):
            emails, not_found = jmapx.fetch_emails_batched(context, ids)
        self.assertEqual(emails, [])
        self.assertEqual(len(not_found), 8001)
        self.assertEqual(request_sizes, [16, 1])

    def test_request_too_large_is_split_and_retried(self):
        context = make_context(max_objects=4)
        calls_seen = []

        def fake_post(_context, calls, using=jmapx.USING):
            calls_seen.append(calls)
            if len(calls_seen) == 1:
                return [["error", {"type": "requestTooLarge"}, "g0"]]
            return [
                ["Email/get", {"list": [], "notFound": arguments["ids"]}, call_id]
                for _, arguments, call_id in calls
            ]

        with mock.patch.object(jmapx, "jmap_post", side_effect=fake_post):
            _, not_found = jmapx.fetch_emails_batched(context, ["a", "b", "c", "d"])
        self.assertEqual(not_found, ["a", "b", "c", "d"])
        self.assertEqual([len(call[1]["ids"]) for call in calls_seen[1]], [2, 2])

    def test_mailbox_get_falls_back_to_query_and_batches(self):
        context = make_context(max_objects=2)
        responses = [
            [["error", {"type": "requestTooLarge"}, "m"]],
            [["Mailbox/query", {"ids": ["m1", "m2", "m3"]}, "mq"]],
            [["Mailbox/get", {"list": [{"id": "m1"}, {"id": "m2"}]}, "m"]],
            [["Mailbox/get", {"list": [{"id": "m3"}]}, "m"]],
        ]
        with mock.patch.object(jmapx, "jmap_post", side_effect=responses):
            mailboxes = jmapx.fetch_mailboxes(context)
        self.assertEqual([m["id"] for m in mailboxes], ["m1", "m2", "m3"])


class DownloadTests(unittest.TestCase):
    def test_conflict_name_contains_full_blob_and_truncates_safely(self):
        blob_id = "b" * 70
        with tempfile.TemporaryDirectory() as directory:
            name = "发票.pdf"
            pathlib.Path(directory, name).touch()
            path, renamed = jmapx.unique_dest_path(directory, name, blob_id)
            self.assertTrue(renamed)
            self.assertTrue(path.endswith(f"发票-1-{blob_id}.pdf"))

            long_name = "x" * 200 + ".pdf"
            pathlib.Path(directory, long_name).touch()
            long_path, _ = jmapx.unique_dest_path(directory, long_name, blob_id)
            self.assertLessEqual(len(os.path.basename(long_path).encode("utf-8")), 255)
            self.assertTrue(long_path.endswith(".pdf"))

    def test_download_many_preserves_success_order_and_isolates_failures(self):
        context = make_context()
        jobs = [("ok1", "1"), ("bad", "2"), ("ok2", "3")]

        def fake_download(_context, blob_id, name, dest_dir, chunks):
            if blob_id == "bad":
                raise jmapx.JmapxError("boom")
            return {"blobId": blob_id, "file": f"/{name}", "size": 1, "renamed": False}

        with mock.patch.object(jmapx, "download_one_blob", side_effect=fake_download):
            downloaded, failed = jmapx.download_many(context, jobs, "/tmp", 3, 1)
        self.assertEqual([item["blobId"] for item in downloaded], ["ok1", "ok2"])
        self.assertEqual(failed, [{"blobId": "bad", "error": "boom"}])


class CliContractTests(unittest.TestCase):
    def test_command_and_parameter_names_are_unchanged(self):
        parser = jmapx.build_parser()
        expected = {"total", "emails", "detail", "download", "attachments"}
        subparsers = next(
            action for action in parser._actions if isinstance(action, jmapx.argparse._SubParsersAction)
        )
        self.assertEqual(set(subparsers.choices), expected)

        download = parser.parse_args([
            "download", "--blob-ids", "b", "--dir", "/tmp",
        ])
        self.assertEqual(download.concurrency, 16)
        self.assertEqual(download.chunks, 4)

        attachments = parser.parse_args([
            "attachments", "--ids", "e", "--filter", "*.pdf", "--download-dir", "/tmp",
        ])
        self.assertEqual(attachments.filter, "*.pdf")
        self.assertEqual(attachments.concurrency, 16)

    def test_no_subcommand_prints_help_and_returns_zero(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = jmapx.main([])
        self.assertEqual(result, 0)
        for command in ("total", "emails", "detail", "download", "attachments"):
            self.assertIn(command, stdout.getvalue())


RUN_INTEGRATION = (
    os.environ.get("JMAPX_INTEGRATION") == "1"
    and (ROOT / "bin" / "jmapx_creds.json").exists()
)


@unittest.skipUnless(RUN_INTEGRATION, "设置 JMAPX_INTEGRATION=1 且提供本地凭据后运行")
class LiveIntegrationTests(unittest.TestCase):
    @classmethod
    def run_cli(cls, *arguments):
        env = os.environ.copy()
        for name in jmapx.ENV_FIELDS.values():
            env.pop(name, None)
        process = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(process.stdout)

    @classmethod
    def setUpClass(cls):
        cls.total = cls.run_cli("total")
        cls.listing = cls.run_cli("emails")
        cls.email = cls.listing["emails"][0] if cls.listing["emails"] else None
        cls.attachment_email = next(
            (email for email in cls.listing["emails"] if email.get("hasAttachment")),
            None,
        )

    def test_total_and_emails(self):
        self.assertIsInstance(self.total["total"], int)
        self.assertEqual(self.listing["returned"], len(self.listing["emails"]))
        self.assertIn("mailboxes", self.listing)

    def test_detail_and_attachments(self):
        if not self.email:
            self.skipTest("账户无邮件")
        detail = self.run_cli("detail", "--ids", self.email["id"], "--body-max-bytes", "200")
        self.assertEqual(detail["requested"], 1)
        self.assertEqual(detail["emails"][0]["id"], self.email["id"])

        attachment_list = self.run_cli("attachments", "--ids", self.email["id"], "--filter", "*.pdf")
        item = attachment_list["emails"][0]
        self.assertIn("from", item)
        self.assertIn("to", item)
        self.assertIn("matched", item)
        self.assertIn("unmatched", item)

    def test_attachment_download_and_conflict_rename(self):
        if not self.attachment_email:
            self.skipTest("账户无带附件邮件")
        details = self.run_cli("attachments", "--ids", self.attachment_email["id"])
        attachment = details["emails"][0]["matched"][0]
        with tempfile.TemporaryDirectory() as directory:
            first = self.run_cli(
                "download",
                "--blob-ids", attachment["blobId"],
                "--names", attachment["name"],
                "--dir", directory,
            )
            second = self.run_cli(
                "download",
                "--blob-ids", attachment["blobId"],
                "--names", attachment["name"],
                "--dir", directory,
            )
            first_item = first["downloaded"][0]
            second_item = second["downloaded"][0]
            self.assertTrue(pathlib.Path(first_item["file"]).is_file())
            self.assertFalse(first_item["renamed"])
            self.assertTrue(second_item["renamed"])
            self.assertIn(attachment["blobId"], pathlib.Path(second_item["file"]).name)


if __name__ == "__main__":
    unittest.main()
