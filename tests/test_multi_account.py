import importlib.util
import base64
import copy
import json
import pathlib
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
WORKER_SPEC = importlib.util.spec_from_file_location(
    "multi_account_worker", ROOT / "workbuddy_checkin.py",
)
worker = importlib.util.module_from_spec(WORKER_SPEC)
WORKER_SPEC.loader.exec_module(worker)

CLI_SPEC = importlib.util.spec_from_file_location(
    "multi_account_cli", ROOT / "checkin_cli.py",
)
cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(cli)


def jwt_for(uid, expires_at):
    def segment(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{segment({'alg': 'none'})}.{segment({'sub': uid, 'exp': expires_at})}.sig"


class AccountJobTest(unittest.TestCase):
    def test_enabled_accounts_get_independent_tokens_and_notification_overrides(self):
        cfg = {
            "desktop_notify": True,
            "notify_channel": "none",
            "accounts": [
                {
                    "id": "alice",
                    "name": "Alice",
                    "enabled": True,
                    "auth": {
                        "mode": "workbuddy",
                        "token": "alice-token",
                        "uid": "alice-uid",
                    },
                },
                {
                    "id": "bob",
                    "name": "Bob",
                    "enabled": True,
                    "auth": {
                        "mode": "codebuddy_cli",
                        "token": "bob-token",
                        "uid": "bob-uid",
                    },
                    "notify": {"desktop_notify": False},
                },
                {
                    "id": "disabled",
                    "name": "Disabled",
                    "enabled": False,
                    "auth": {"token": "ignored", "uid": "ignored"},
                },
            ],
        }

        account_jobs = getattr(worker, "account_jobs", lambda *_args, **_kwargs: [])
        jobs = account_jobs(cfg)

        self.assertEqual([job["id"] for job in jobs], ["alice", "bob"])
        self.assertEqual(jobs[0]["token"], ("alice-token", "alice-uid"))
        self.assertEqual(jobs[1]["token"], ("bob-token", "bob-uid"))
        self.assertTrue(jobs[0]["config"]["desktop_notify"])
        self.assertFalse(jobs[1]["config"]["desktop_notify"])
        self.assertEqual(jobs[0]["config"]["_auth_mode"], "workbuddy")
        self.assertEqual(jobs[1]["config"]["_account_id"], "bob")

    def test_selecting_unknown_account_is_rejected(self):
        cfg = {
            "accounts": [{
                "id": "alice", "name": "Alice", "enabled": True,
                "auth": {"token": "token", "uid": "uid"},
            }],
        }
        account_jobs = getattr(worker, "account_jobs", lambda *_args, **_kwargs: [])

        with self.assertRaisesRegex(ValueError, "missing"):
            account_jobs(cfg, selected="missing")

    def test_legacy_config_remains_a_single_implicit_job(self):
        account_jobs = getattr(worker, "account_jobs", lambda *_args, **_kwargs: [])

        jobs = account_jobs({"desktop_notify": False})

        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0]["id"])
        self.assertIsNone(jobs[0]["token"])
        self.assertFalse(jobs[0]["config"]["desktop_notify"])

    def test_token_extraction_can_select_requested_uid(self):
        now = int(time.time())
        alice = jwt_for("alice-uid", now + 600)
        bob = jwt_for("bob-uid", now + 3600)
        with tempfile.TemporaryDirectory() as directory:
            alice_path = pathlib.Path(directory) / "alice.info"
            bob_path = pathlib.Path(directory) / "bob.info"
            alice_path.write_text(json.dumps({
                "auth": {"accessToken": alice},
                "account": {"uid": "alice-uid"},
            }))
            bob_path.write_text(json.dumps({
                "auth": {"accessToken": bob},
                "account": {"uid": "bob-uid"},
            }))
            with mock.patch.object(
                        worker, "_codebuddy_auth_paths",
                        return_value=(str(alice_path), str(bob_path)),
                    ), mock.patch.object(
                        worker.os.path, "isdir", return_value=False,
                    ):
                selected = worker.extract_token(expected_uid="alice-uid")

        self.assertEqual(selected, (alice, "alice-uid"))

    def test_current_login_prefers_official_auth_over_longer_lived_history(self):
        now = int(time.time())
        first_user = jwt_for("first-uid", now + 7200)
        second_user = jwt_for("second-uid", now + 1800)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            auth_path = root / "current.info"
            auth_path.write_text(json.dumps({
                "auth": {"accessToken": second_user},
                "account": {"uid": "second-uid"},
            }))
            logs = root / "logs"
            logs.mkdir()
            (logs / "workbuddyMainThread-old.log").write_text(
                f"Authorization: Bearer {first_user}\n",
            )
            with mock.patch.object(
                        worker, "_codebuddy_auth_paths",
                        return_value=(str(auth_path),),
                    ), mock.patch.object(worker, "LOGS_DIR", str(logs)):
                current_extractor = getattr(
                    worker, "extract_current_token", worker.extract_token,
                )
                selected = current_extractor()

        self.assertEqual(selected, (second_user, "second-uid"))

    def test_current_login_uses_latest_workbuddy_log_entry_without_auth_file(self):
        now = int(time.time())
        first_user = jwt_for("first-uid", now + 7200)
        second_user = jwt_for("second-uid", now + 1800)
        with tempfile.TemporaryDirectory() as directory:
            log_path = pathlib.Path(directory) / "workbuddyMainThread.log"
            log_path.write_text(
                f"Authorization: Bearer {first_user}\n"
                f"Authorization: Bearer {second_user}\n",
            )
            with mock.patch.object(
                        worker, "_codebuddy_auth_paths", return_value=(),
                    ), mock.patch.object(worker, "LOGS_DIR", directory):
                selected = worker.extract_current_token()

        self.assertEqual(selected, (second_user, "second-uid"))

    def test_requested_user_never_falls_back_to_history_when_another_user_is_current(self):
        now = int(time.time())
        current_user = jwt_for("current-uid", now + 1800)
        historical_user = jwt_for("historical-uid", now + 7200)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            auth_path = root / "current.info"
            auth_path.write_text(json.dumps({
                "auth": {"accessToken": current_user},
                "account": {"uid": "current-uid"},
            }))
            logs = root / "logs"
            logs.mkdir()
            (logs / "workbuddyMainThread.log").write_text(
                f"Authorization: Bearer {historical_user}\n",
            )
            with mock.patch.object(
                        worker, "_codebuddy_auth_paths",
                        return_value=(str(auth_path),),
                    ), mock.patch.object(worker, "LOGS_DIR", str(logs)):
                selected = worker.extract_current_token(
                    expected_uid="historical-uid",
                )

        self.assertIsNone(selected)

    def test_current_login_rejects_token_already_rejected_by_server(self):
        now = int(time.time())
        rejected = jwt_for("alice-uid", now + 1800)
        with tempfile.TemporaryDirectory() as directory:
            auth_path = pathlib.Path(directory) / "current.info"
            auth_path.write_text(json.dumps({
                "auth": {"accessToken": rejected},
                "account": {"uid": "alice-uid"},
            }))
            with mock.patch.object(
                        worker, "_codebuddy_auth_paths",
                        return_value=(str(auth_path),),
                    ), mock.patch.object(
                        worker.os.path, "isdir", return_value=False,
                    ):
                extractor = getattr(
                    worker, "extract_current_token", worker.extract_token,
                )
                try:
                    selected = extractor(
                        expected_uid="alice-uid",
                        rejected_token=rejected,
                    )
                except TypeError:
                    selected = (rejected, "alice-uid")

        self.assertIsNone(selected)


class MultiAccountExecutionTest(unittest.TestCase):
    def test_account_without_saved_token_does_not_borrow_another_login(self):
        account_cfg = {
            "_account_id": "alice",
            "_account_name": "Alice",
            "_account_uid": "alice-uid",
            "_auth_mode": "workbuddy",
        }
        status = {"data": {"active": True, "today_checked_in": True}}
        with mock.patch.object(
                    worker, "extract_token",
                    return_value=("bob-token", "bob-uid"),
                ) as extract, \
                mock.patch.object(
                    worker, "_launch_reauthentication", return_value=None,
                ) as reauth, \
                mock.patch.object(
                    worker, "_post", return_value=(200, status),
                ) as post:
            result = worker.do_checkin(
                cfg=account_cfg, _token_override=None,
            )

        self.assertEqual(result, (False, False, "failed"))
        extract.assert_not_called()
        reauth.assert_called_once_with(account_cfg)
        post.assert_not_called()

    def test_worker_runs_every_account_with_its_own_token(self):
        cfg = {
            "max_retries": 0,
            "retry_base_delay": 1,
            "desktop_notify": False,
            "notify_channel": "none",
            "accounts": [
                {
                    "id": "alice", "name": "Alice", "enabled": True,
                    "auth": {"token": "alice-token", "uid": "alice-uid"},
                },
                {
                    "id": "bob", "name": "Bob", "enabled": True,
                    "auth": {"token": "bob-token", "uid": "bob-uid"},
                },
            ],
        }
        with mock.patch.object(worker, "load_config", return_value=cfg), \
                mock.patch.object(
                    worker, "do_checkin",
                    side_effect=[
                        (False, False, "failed"),
                        (True, False, "checked_in"),
                    ],
                ) as checkin, \
                mock.patch.object(worker, "notify") as notify, \
                mock.patch.object(worker.sys, "argv", ["workbuddy_checkin.py"]):
            with self.assertRaises(SystemExit) as exited:
                worker.main()

        self.assertEqual(exited.exception.code, 1)
        self.assertEqual(checkin.call_count, 2)
        self.assertEqual(
            checkin.call_args_list[0].kwargs["_token_override"],
            ("alice-token", "alice-uid"),
        )
        self.assertEqual(
            checkin.call_args_list[1].kwargs["_token_override"],
            ("bob-token", "bob-uid"),
        )
        sent_titles = [call.args[0] for call in notify.call_args_list]
        self.assertTrue(any("Alice" in title for title in sent_titles))
        self.assertTrue(any("Bob" in title for title in sent_titles))

    def test_worker_can_run_one_selected_account(self):
        cfg = {
            "max_retries": 0,
            "accounts": [
                {
                    "id": "alice", "name": "Alice", "enabled": True,
                    "auth": {"token": "alice-token", "uid": "alice-uid"},
                },
                {
                    "id": "bob", "name": "Bob", "enabled": True,
                    "auth": {"token": "bob-token", "uid": "bob-uid"},
                },
            ],
        }
        with mock.patch.object(worker, "load_config", return_value=cfg), \
                mock.patch.object(
                    worker, "do_checkin", return_value=(True, False, "checked_in"),
                ) as checkin, \
                mock.patch.object(worker, "notify"), \
                mock.patch.object(
                    worker.sys, "argv",
                    ["workbuddy_checkin.py", "--account", "bob"],
                ):
            with self.assertRaises(SystemExit) as exited:
                worker.main()

        self.assertEqual(exited.exception.code, 0)
        checkin.assert_called_once()
        self.assertEqual(
            checkin.call_args.kwargs.get("_token_override"),
            ("bob-token", "bob-uid"),
        )


class MultiAccountParserTest(unittest.TestCase):
    def test_run_accepts_account_selection_and_all_accounts(self):
        parser = cli.build_parser()

        try:
            selected = parser.parse_args(["run", "--account", "alice"])
            all_accounts = parser.parse_args(["run", "--all"])
        except SystemExit as exc:
            self.fail(f"run account selection is not supported: exit={exc.code}")

        self.assertEqual(selected.account, "alice")
        self.assertFalse(selected.all_accounts)
        self.assertTrue(all_accounts.all_accounts)
        self.assertIsNone(all_accounts.account)

    def test_account_commands_are_available(self):
        parser = cli.build_parser()

        try:
            add = parser.parse_args([
                "account", "add", "alice", "--name", "Alice",
                "--auth-mode", "workbuddy",
            ])
            listing = parser.parse_args(["account", "list"])
            login = parser.parse_args(["account", "login", "alice"])
            rename = parser.parse_args([
                "account", "rename", "alice", "--name", "New Alice",
            ])
            remove = parser.parse_args(["account", "remove", "alice"])
        except SystemExit as exc:
            self.fail(f"account commands are not supported: exit={exc.code}")

        self.assertEqual(add.account_id, "alice")
        self.assertEqual(add.name, "Alice")
        self.assertEqual(add.auth_mode, "workbuddy")
        self.assertIs(add.func, cli.cmd_account_add)
        self.assertIs(listing.func, cli.cmd_account_list)
        self.assertIs(login.func, cli.cmd_account_login)
        self.assertEqual(rename.account_id, "alice")
        self.assertEqual(rename.name, "New Alice")
        self.assertIs(rename.func, cli.cmd_account_rename)
        self.assertIs(remove.func, cli.cmd_account_remove)

    def test_account_add_allows_login_derived_id(self):
        parser = cli.build_parser()

        try:
            add = parser.parse_args([
                "account", "add", "--name", "Alice",
                "--auth-mode", "workbuddy",
            ])
        except SystemExit as exc:
            self.fail(f"account ID is still required: exit={exc.code}")

        self.assertIsNone(add.account_id)

    def test_status_lists_accounts_without_exposing_tokens(self):
        cfg = {
            "accounts": [
                {
                    "id": "alice", "name": "Alice", "enabled": True,
                    "auth": {
                        "mode": "workbuddy", "uid": "alice-uid",
                        "token": "super-secret-alice-token",
                    },
                },
                {
                    "id": "bob", "name": "Bob", "enabled": False,
                    "auth": {
                        "mode": "codebuddy_cli", "uid": "bob-uid",
                        "token": "super-secret-bob-token",
                    },
                },
            ],
        }
        platform = mock.Mock()
        platform.status_rows.return_value = []
        output = StringIO()
        with mock.patch.object(cli, "read_config", return_value=cfg), \
                mock.patch.object(cli.os.path, "isfile", return_value=True), \
                mock.patch.object(cli, "_platform_adapter", return_value=platform), \
                mock.patch.object(cli, "schedule_installed", return_value=(False, "")), \
                redirect_stdout(output):
            result = cli.cmd_status(None)

        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("签到账户", rendered)
        self.assertIn("Alice (alice)", rendered)
        self.assertIn("Bob (bob)", rendered)
        self.assertIn("已禁用", rendered)
        self.assertNotIn("super-secret", rendered)


class AccountCommandTest(unittest.TestCase):
    def test_wizard_saves_authenticated_user_as_first_account(self):
        cfg = {}
        with mock.patch.object(
                    cli, "_run_environment_preflight", return_value="workbuddy",
                ), mock.patch.object(
                    cli, "_find_workbuddy_app",
                    return_value="/Applications/WorkBuddy.app",
                ), mock.patch.object(
                    cli, "_capture_current_login",
                    return_value=("wizard-token", "wizard-uid"),
                ), mock.patch.object(
                    cli, "_prepare_schedule_definition", return_value=None,
                ), mock.patch.object(cli, "write_config") as write_config, \
                mock.patch("builtins.input", side_effect=["", "", "", "", "", ""]), \
                redirect_stdout(StringIO()):
            result = cli.interactive_config(cfg)

        self.assertEqual(result, 0)
        saved = write_config.call_args.args[0]
        self.assertEqual(len(saved["accounts"]), 1)
        account = saved["accounts"][0]
        self.assertEqual(account["name"], "默认账号")
        self.assertEqual(account["auth"]["uid"], "wizard-uid")
        self.assertEqual(account["auth"]["token"], "wizard-token")
        self.assertEqual(account["auth"]["mode"], "workbuddy")

    def test_first_account_add_migrates_legacy_wizard_user_before_switching(self):
        first_uid = "legacy-wizard-uid"
        second_uid = "second-user-uid"
        existing = {
            "_auth_mode": "workbuddy",
            "_auth_executable": "/Applications/WorkBuddy.app",
        }
        args = cli.argparse.Namespace(
            account_id=None,
            name="Second",
            auth_mode="auto",
            replace=False,
        )
        with mock.patch.object(cli, "read_config", return_value=existing), \
                mock.patch.object(
                    cli, "_capture_current_login",
                    side_effect=[
                        ("legacy-token", first_uid),
                        ("second-token", second_uid),
                    ],
                ), mock.patch.object(
                    cli, "_find_workbuddy_app",
                    return_value="/Applications/WorkBuddy.app",
                ), mock.patch.object(
                    cli, "_launch_workbuddy_app", return_value=True,
                ), mock.patch.object(
                    cli, "_wait_for_workbuddy_token", return_value=True,
                ), mock.patch.object(cli, "write_config") as write_config, \
                redirect_stdout(StringIO()):
            result = cli.cmd_account_add(args)

        self.assertEqual(result, 0)
        saved = write_config.call_args.args[0]
        self.assertEqual(len(saved["accounts"]), 2)
        self.assertEqual(saved["accounts"][0]["name"], "默认账号")
        self.assertEqual(saved["accounts"][0]["auth"]["uid"], first_uid)
        self.assertEqual(saved["accounts"][1]["name"], "Second")
        self.assertEqual(saved["accounts"][1]["auth"]["uid"], second_uid)

    def test_account_add_without_id_guides_switch_when_current_user_exists(self):
        first_uid = "first-user-uid"
        second_uid = "second-user-uid"
        first_id = cli._account_id_from_uid(first_uid)
        existing = {
            "_auth_mode": "workbuddy",
            "accounts": [{
                "id": first_id,
                "name": "First",
                "enabled": True,
                "auth": {
                    "mode": "workbuddy",
                    "executable": "/Applications/WorkBuddy.app",
                    "token": "first-token",
                    "uid": first_uid,
                },
            }],
        }
        args = cli.argparse.Namespace(
            account_id=None,
            name="Second",
            auth_mode="workbuddy",
            replace=False,
        )
        with mock.patch.object(cli, "read_config", return_value=existing), \
                mock.patch.object(
                    cli, "_capture_current_login",
                    side_effect=[
                        ("first-token", first_uid),
                        ("second-token", second_uid),
                    ],
                ), \
                mock.patch.object(
                    cli, "_find_workbuddy_app",
                    return_value="/Applications/WorkBuddy.app",
                ), \
                mock.patch.object(
                    cli, "_launch_workbuddy_app", return_value=True,
                ) as launch, \
                mock.patch.object(
                    cli, "_wait_for_workbuddy_token", return_value=True,
                ) as wait, \
                mock.patch.object(cli, "write_config") as write_config, \
                redirect_stdout(StringIO()):
            result = cli.cmd_account_add(args)

        self.assertEqual(result, 0)
        launch.assert_called_once_with("/Applications/WorkBuddy.app")
        wait.assert_called_once_with(rejected_uid=first_uid)
        saved = write_config.call_args.args[0]
        self.assertEqual(len(saved["accounts"]), 2)
        self.assertEqual(saved["accounts"][1]["name"], "Second")
        self.assertEqual(saved["accounts"][1]["auth"]["uid"], second_uid)
        self.assertEqual(
            saved["accounts"][1]["id"], cli._account_id_from_uid(second_uid),
        )

    def test_account_add_guides_login_when_saved_account_has_no_local_session(self):
        first_uid = "first-user-uid"
        second_uid = "second-user-uid"
        existing = {
            "accounts": [{
                "id": cli._account_id_from_uid(first_uid),
                "name": "First",
                "enabled": True,
                "auth": {
                    "mode": "codebuddy_cli",
                    "executable": "/usr/local/bin/codebuddy",
                    "token": "first-token",
                    "uid": first_uid,
                },
            }],
        }
        args = cli.argparse.Namespace(
            account_id=None,
            name="Second",
            auth_mode="codebuddy_cli",
            replace=False,
        )
        with mock.patch.object(cli, "read_config", return_value=existing), \
                mock.patch.object(
                    cli, "_capture_current_login", return_value=None,
                ), mock.patch.object(
                    cli, "_capture_different_account_login",
                    return_value=("second-token", second_uid),
                ) as capture_different, mock.patch.object(
                    cli, "write_config",
                ) as write_config, redirect_stdout(StringIO()):
            result = cli.cmd_account_add(args)

        self.assertEqual(result, 0)
        capture_different.assert_called_once_with(
            existing, "codebuddy_cli", first_uid,
        )
        saved = write_config.call_args.args[0]
        self.assertEqual(len(saved["accounts"]), 2)
        self.assertEqual(saved["accounts"][1]["auth"]["uid"], second_uid)

    def test_account_add_switch_timeout_does_not_change_configuration(self):
        first_uid = "first-user-uid"
        existing = {
            "accounts": [{
                "id": cli._account_id_from_uid(first_uid),
                "name": "First",
                "auth": {"uid": first_uid, "token": "first-token"},
            }],
        }
        args = cli.argparse.Namespace(
            account_id=None,
            name="Second",
            auth_mode="workbuddy",
            replace=False,
        )
        with mock.patch.object(cli, "read_config", return_value=existing), \
                mock.patch.object(
                    cli, "_capture_current_login",
                    return_value=("first-token", first_uid),
                ), \
                mock.patch.object(
                    cli, "_find_workbuddy_app",
                    return_value="/Applications/WorkBuddy.app",
                ), \
                mock.patch.object(
                    cli, "_launch_workbuddy_app", return_value=True,
                ) as launch, \
                mock.patch.object(
                    cli, "_wait_for_workbuddy_token", return_value=False,
                ) as wait, \
                mock.patch.object(cli, "write_config") as write_config, \
                redirect_stdout(StringIO()):
            result = cli.cmd_account_add(args)

        self.assertEqual(result, 1)
        launch.assert_called_once_with("/Applications/WorkBuddy.app")
        wait.assert_called_once_with(rejected_uid=first_uid)
        write_config.assert_not_called()

    def test_account_rename_only_changes_requested_display_name(self):
        existing = {
            "_schedule_hour": 9,
            "notify_channel": "none",
            "accounts": [
                {
                    "id": "alice", "name": "Old Alice", "enabled": False,
                    "auth": {
                        "mode": "workbuddy", "uid": "alice-uid",
                        "token": "alice-secret",
                    },
                    "notify": {"desktop_notify": False},
                },
                {
                    "id": "bob", "name": "Bob", "enabled": True,
                    "auth": {"uid": "bob-uid", "token": "bob-secret"},
                },
            ],
        }
        expected = copy.deepcopy(existing)
        expected["accounts"][0]["name"] = "New Alice"
        args = cli.argparse.Namespace(account_id="alice", name="  New Alice  ")

        with mock.patch.object(
                    cli, "read_config", return_value=copy.deepcopy(existing),
                ), mock.patch.object(
                    cli, "_capture_current_login",
                    side_effect=AssertionError("rename must not read login state"),
                ), mock.patch.object(cli, "write_config") as write_config:
            result = getattr(cli, "cmd_account_rename", lambda _args: 99)(args)

        self.assertEqual(result, 0)
        write_config.assert_called_once_with(expected)

    def test_account_rename_rejects_blank_name_without_writing(self):
        args = cli.argparse.Namespace(account_id="alice", name="   ")
        with mock.patch.object(cli, "read_config") as read_config, \
                mock.patch.object(cli, "write_config") as write_config:
            result = getattr(cli, "cmd_account_rename", lambda _args: 99)(args)

        self.assertEqual(result, 2)
        read_config.assert_not_called()
        write_config.assert_not_called()

    def test_account_rename_rejects_unknown_account_without_writing(self):
        args = cli.argparse.Namespace(account_id="missing", name="New Name")
        with mock.patch.object(
                    cli, "read_config", return_value={"accounts": []},
                ), mock.patch.object(cli, "write_config") as write_config:
            result = getattr(cli, "cmd_account_rename", lambda _args: 99)(args)

        self.assertEqual(result, 1)
        write_config.assert_not_called()

    def test_account_rename_rejects_alias_used_by_another_account(self):
        existing = {
            "accounts": [
                {"id": "alice", "name": "Alice", "auth": {"uid": "uid-a"}},
                {"id": "bob", "name": "Bob", "auth": {"uid": "uid-b"}},
            ],
        }
        args = cli.argparse.Namespace(account_id="bob", name="  Alice  ")
        output = StringIO()
        with mock.patch.object(
                    cli, "read_config", return_value=existing,
                ), mock.patch.object(
                    cli, "write_config",
                ) as write_config, redirect_stdout(output):
            result = cli.cmd_account_rename(args)

        self.assertEqual(result, 1)
        self.assertIn("别名 Alice 已被其他账户使用", output.getvalue())
        write_config.assert_not_called()

    def test_account_add_generates_stable_private_id_from_login_uid(self):
        args = cli.argparse.Namespace(
            account_id=None,
            name="Alice",
            auth_mode="workbuddy",
            replace=False,
        )
        with mock.patch.object(cli, "read_config", return_value={}), \
                mock.patch.object(
                    cli, "_capture_current_login",
                    return_value=("alice-token", "alice-sensitive-uid"),
                ), \
                mock.patch.object(
                    cli, "_find_workbuddy_app",
                    return_value="/Applications/WorkBuddy.app",
                ), \
                mock.patch.object(cli, "write_config") as write_config:
            result = cli.cmd_account_add(args)

        self.assertEqual(result, 0)
        account = write_config.call_args.args[0]["accounts"][0]
        self.assertRegex(account["id"], r"^user-[0-9a-f]{12}$")
        self.assertNotIn("alice-sensitive-uid", account["id"])

    def test_account_add_captures_current_login_without_overwriting_global_config(self):
        existing = {
            "_schedule_hour": 9,
            "_schedule_minute": 10,
            "notify_channel": "none",
        }
        args = cli.argparse.Namespace(
            account_id="alice",
            name="Alice",
            auth_mode="workbuddy",
            replace=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "checkin_config.json"
            with mock.patch.object(cli, "CONFIG_PATH", str(config_path)), \
                    mock.patch.object(cli, "read_config", return_value=existing), \
                    mock.patch.object(
                        cli, "_capture_current_login",
                        return_value=("alice-token", "alice-uid"),
                        create=True,
                    ), \
                    mock.patch.object(
                        cli, "_find_workbuddy_app",
                        return_value="/Applications/WorkBuddy.app",
                    ), \
                    mock.patch.object(cli, "write_config") as write_config:
                result = getattr(cli, "cmd_account_add", lambda _args: 99)(args)

        self.assertEqual(result, 0)
        saved = write_config.call_args.args[0]
        self.assertEqual(saved["_schedule_hour"], 9)
        self.assertEqual(saved["notify_channel"], "none")
        self.assertEqual(saved["accounts"][0]["id"], "alice")
        self.assertEqual(saved["accounts"][0]["name"], "Alice")
        self.assertEqual(saved["accounts"][0]["auth"]["uid"], "alice-uid")
        self.assertEqual(saved["accounts"][0]["auth"]["token"], "alice-token")
        self.assertEqual(
            saved["accounts"][0]["auth"]["executable"],
            "/Applications/WorkBuddy.app",
        )

    def test_account_add_rejects_duplicate_uid(self):
        existing = {
            "accounts": [{
                "id": "alice", "name": "Alice", "enabled": True,
                "auth": {"token": "old-token", "uid": "same-uid"},
            }],
        }
        args = cli.argparse.Namespace(
            account_id="bob", name="Bob", auth_mode="codebuddy_cli",
            replace=False,
        )
        with mock.patch.object(cli, "read_config", return_value=existing), \
                mock.patch.object(
                    cli, "_capture_current_login",
                    return_value=("new-token", "same-uid"),
                    create=True,
                ), \
                mock.patch.object(cli, "write_config") as write_config:
            result = getattr(cli, "cmd_account_add", lambda _args: 99)(args)

        self.assertEqual(result, 1)
        write_config.assert_not_called()

    def test_account_add_rejects_alias_used_by_another_account_before_login(self):
        existing = {
            "accounts": [{
                "id": "alice", "name": "Alice", "enabled": True,
                "auth": {"token": "old-token", "uid": "first-uid"},
            }],
        }
        args = cli.argparse.Namespace(
            account_id=None, name="  Alice  ", auth_mode="codebuddy_cli",
            replace=False,
        )
        output = StringIO()
        with mock.patch.object(cli, "read_config", return_value=existing), \
                mock.patch.object(
                    cli, "_capture_current_login",
                    return_value=("new-token", "second-uid"),
                ) as capture_login, mock.patch.object(
                    cli, "write_config",
                ) as write_config, redirect_stdout(output):
            result = cli.cmd_account_add(args)

        self.assertEqual(result, 1)
        self.assertIn("别名 Alice 已被其他账户使用", output.getvalue())
        capture_login.assert_not_called()
        write_config.assert_not_called()

    def test_account_remove_only_removes_requested_account(self):
        existing = {
            "accounts": [
                {"id": "alice", "auth": {"uid": "alice-uid"}},
                {"id": "bob", "auth": {"uid": "bob-uid"}},
            ],
        }
        args = cli.argparse.Namespace(account_id="alice")
        with mock.patch.object(cli, "read_config", return_value=existing), \
                mock.patch.object(cli, "write_config") as write_config:
            result = getattr(cli, "cmd_account_remove", lambda _args: 99)(args)

        self.assertEqual(result, 0)
        saved = write_config.call_args.args[0]
        self.assertEqual([item["id"] for item in saved["accounts"]], ["bob"])

    def test_account_login_waits_for_matching_user_and_updates_only_that_account(self):
        existing = {
            "accounts": [
                {
                    "id": "alice", "name": "Alice",
                    "auth": {
                        "mode": "workbuddy", "uid": "alice-uid",
                        "token": "old-alice",
                    },
                },
                {
                    "id": "bob", "name": "Bob",
                    "auth": {
                        "mode": "workbuddy", "uid": "bob-uid",
                        "token": "bob-token",
                    },
                },
            ],
        }
        args = cli.argparse.Namespace(mode="auto", account="alice")
        with mock.patch.object(cli, "read_config", return_value=existing), \
                mock.patch.object(
                    cli, "_find_workbuddy_app",
                    return_value="/Applications/WorkBuddy.app",
                ), \
                mock.patch.object(
                    cli, "_launch_workbuddy_app", return_value=True,
                ), \
                mock.patch.object(
                    cli, "_wait_for_workbuddy_token", return_value=True,
                ) as wait, \
                mock.patch.object(
                    cli, "_capture_current_login",
                    return_value=("new-alice", "alice-uid"),
                ), \
                mock.patch.object(cli, "write_config") as write_config, \
                redirect_stdout(StringIO()):
            result = cli.cmd_reauth(args)

        self.assertEqual(result, 0)
        wait.assert_called_once_with(
            expected_uid="alice-uid", rejected_token="old-alice",
        )
        saved = write_config.call_args.args[0]
        self.assertEqual(saved["accounts"][0]["auth"]["token"], "new-alice")
        self.assertEqual(saved["accounts"][1]["auth"]["token"], "bob-token")


class AccountReauthenticationTest(unittest.TestCase):
    def test_worker_reauth_loads_updated_credentials_for_requested_account(self):
        runtime_cfg = {
            "_account_id": "alice",
            "_auth_mode": "codebuddy_cli",
            "desktop_notify": False,
            "notify_channel": "none",
        }
        updated = {
            "accounts": [{
                "id": "alice",
                "auth": {"token": "new-token", "uid": "alice-uid"},
            }],
        }
        completed = mock.Mock(returncode=0)
        with mock.patch.object(
                    worker.subprocess, "run", return_value=completed,
                ) as run, \
                mock.patch.object(worker, "load_config", return_value=updated), \
                mock.patch.object(
                    worker, "extract_token", return_value=("wrong", "bob-uid"),
                ) as extract, \
                mock.patch.object(worker, "notify"):
            result = worker._launch_reauthentication(runtime_cfg)

        self.assertEqual(result, ("new-token", "alice-uid"))
        self.assertEqual(run.call_args.args[0][-2:], ["--account", "alice"])
        extract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
