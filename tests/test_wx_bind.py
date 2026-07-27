import importlib.util
import base64
import json
import pathlib
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


MODULE_PATH = pathlib.Path(__file__).parents[1] / "checkin_cli.py"
SPEC = importlib.util.spec_from_file_location("checkin_cli", MODULE_PATH)
checkin_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checkin_cli)

WORKER_PATH = pathlib.Path(__file__).parents[1] / "workbuddy_checkin.py"
WORKER_SPEC = importlib.util.spec_from_file_location("workbuddy_checkin", WORKER_PATH)
worker = importlib.util.module_from_spec(WORKER_SPEC)
WORKER_SPEC.loader.exec_module(worker)


class WxBindHelpersTest(unittest.TestCase):
    def test_parser_exposes_one_click_wx_bind_and_keeps_wx_login_alias(self):
        parser = checkin_cli.build_parser()

        bind_args = parser.parse_args(["wx-bind"])
        login_args = parser.parse_args(["wx-login"])

        self.assertIs(bind_args.func, checkin_cli.cmd_wx_bind)
        self.assertIs(login_args.func, checkin_cli.cmd_wx_bind)

    def test_matching_template_reuses_an_existing_compatible_template(self):
        templates = [
            {
                "template_id": "old-layout",
                "title": "签到通知",
                "content": "{{first.DATA}}\n时间：{{keyword1.DATA}}\n{{remark.DATA}}",
            },
            {
                "template_id": "time-only-layout",
                "title": "签到通知",
                "content": "{{keyword1.DATA}}\n时间：{{keyword2.DATA}}",
            },
            {
                "template_id": "unlabelled-keywords",
                "title": "签到通知",
                "content": "{{keyword1.DATA}}\r\n{{keyword2.DATA}}\r\n时间： {{keyword3.DATA}}",
            },
            {
                "template_id": "wanted",
                "title": "签到通知",
                "content": "结果：{{keyword1.DATA}}\r\n说明：{{keyword2.DATA}}\r\n时间：{{keyword3.DATA}}",
            },
        ]

        self.assertEqual(checkin_cli._matching_template_id(templates), "wanted")

    def test_wechat_template_labels_every_dynamic_field_for_client_rendering(self):
        self.assertIn("结果：{{keyword1.DATA}}", checkin_cli.WX_TEMPLATE_CONTENT)
        self.assertIn("说明：{{keyword2.DATA}}", checkin_cli.WX_TEMPLATE_CONTENT)
        self.assertIn("时间：{{keyword3.DATA}}", checkin_cli.WX_TEMPLATE_CONTENT)

    def test_new_follower_wins_over_previously_configured_follower(self):
        selected = checkin_cli._choose_openid(
            before=["old-user"],
            current=["old-user", "just-scanned-user"],
            configured="old-user",
        )

        self.assertEqual(selected, "just-scanned-user")

    def test_configured_or_only_follower_can_be_reused_without_another_scan(self):
        self.assertEqual(
            checkin_cli._choose_openid(["only-user"], ["only-user"], "only-user"),
            "only-user",
        )
        self.assertEqual(
            checkin_cli._choose_openid([], ["only-user"], ""),
            "only-user",
        )

    def test_multiple_existing_followers_are_not_guessed(self):
        self.assertEqual(
            checkin_cli._choose_openid(
                before=["first", "second"],
                current=["first", "second"],
                configured="",
            ),
            "",
        )

    def test_binding_config_preserves_unrelated_settings(self):
        original = {"_schedule_hour": 8, "desktop_notify": False, "custom": "keep"}

        result = checkin_cli._with_wx_binding(
            original,
            appid="wx12345678",
            secret="secret-value",
            openid="openid-value",
            template_id="template-value",
        )

        self.assertEqual(result["_schedule_hour"], 8)
        self.assertFalse(result["desktop_notify"])
        self.assertEqual(result["custom"], "keep")
        self.assertEqual(result["wx_test_appid"], "wx12345678")
        self.assertEqual(result["wx_test_secret"], "secret-value")
        self.assertEqual(result["wx_test_touser"], "openid-value")
        self.assertEqual(result["wx_test_template_id"], "template-value")
        self.assertEqual(result["notify_channel"], "wx_test")
        self.assertNotEqual(result, original)

    def test_config_file_with_wechat_secret_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.json"
            with mock.patch.object(checkin_cli, "CONFIG_PATH", str(path)):
                checkin_cli.write_config({"wx_test_secret": "sensitive"})

            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_wizard_runs_browser_binding_when_user_chooses_wechat(self):
        answers = ["", "", "", "", "1", "1", ""]
        output = StringIO()
        with mock.patch("builtins.input", side_effect=answers), \
                mock.patch.object(
                    checkin_cli, "_run_environment_preflight",
                    return_value=True, create=True,
                ), \
                mock.patch.object(checkin_cli, "write_config") as write_config, \
                mock.patch.object(checkin_cli, "write_plist", return_value="test.plist"), \
                mock.patch.object(checkin_cli, "cmd_wx_bind", return_value=0) as bind, \
                redirect_stdout(output):
            result = checkin_cli.interactive_config({})

        self.assertEqual(result, 0)
        self.assertEqual(write_config.call_args.args[0]["notify_channel"], "wx_test")
        bind.assert_called_once()
        bind_args = bind.call_args.args[0]
        self.assertFalse(bind_args.no_bootstrap)
        self.assertEqual(getattr(bind_args, "mode", None), "auto")

    def test_wizard_can_choose_manual_wechat_binding(self):
        answers = ["", "", "", "", "1", "2", ""]
        with mock.patch("builtins.input", side_effect=answers), \
                mock.patch.object(
                    checkin_cli, "_run_environment_preflight",
                    return_value=True, create=True,
                ), \
                mock.patch.object(checkin_cli, "write_config"), \
                mock.patch.object(checkin_cli, "write_plist", return_value="test.plist"), \
                mock.patch.object(checkin_cli, "cmd_wx_bind", return_value=0) as bind, \
                redirect_stdout(StringIO()):
            result = checkin_cli.interactive_config({})

        self.assertEqual(result, 0)
        bind.assert_called_once()
        self.assertEqual(getattr(bind.call_args.args[0], "mode", None), "manual")

    def test_manual_binding_mode_skips_browser_automation(self):
        flow = getattr(checkin_cli, "_run_wx_bind_flow", None)
        self.assertIsNotNone(flow)
        args = checkin_cli.argparse.Namespace(mode="manual", no_bootstrap=False)
        with mock.patch.object(checkin_cli, "_cmd_wx_bind_auto") as automatic, \
                mock.patch.object(checkin_cli, "cmd_wx_setup", return_value=0) as manual:
            result = flow(args)

        self.assertEqual(result, 0)
        automatic.assert_not_called()
        manual.assert_called_once_with(args)

    def test_automatic_binding_failure_can_fall_back_to_manual_input(self):
        flow = getattr(checkin_cli, "_run_wx_bind_flow", None)
        self.assertIsNotNone(flow)
        args = checkin_cli.argparse.Namespace(mode="auto", no_bootstrap=False)
        with mock.patch.object(checkin_cli, "_cmd_wx_bind_auto", return_value=1), \
                mock.patch.object(checkin_cli, "cmd_wx_setup", return_value=0) as manual, \
                mock.patch("builtins.input", return_value=""):
            result = flow(args)

        self.assertEqual(result, 0)
        manual.assert_called_once_with(args)

    def test_bootstrapped_auto_binding_keeps_auto_mode(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(checkin_cli.os.path, "exists", return_value=True), \
                mock.patch.object(checkin_cli.subprocess, "run", return_value=completed):
            command = checkin_cli._bootstrap_wx_bind_runtime()

        self.assertEqual(command[-2:], ["--mode", "auto"])
        self.assertIn("--no-fallback", command)

    def test_nested_auto_process_can_disable_its_own_fallback_prompt(self):
        flow = checkin_cli._run_wx_bind_flow
        args = checkin_cli.argparse.Namespace(
            mode="auto",
            no_bootstrap=True,
            no_fallback=True,
        )
        with mock.patch.object(checkin_cli, "_cmd_wx_bind_auto", return_value=1), \
                mock.patch.object(checkin_cli, "cmd_wx_setup") as manual, \
                mock.patch("builtins.input") as prompt:
            result = flow(args)

        self.assertEqual(result, 1)
        prompt.assert_not_called()
        manual.assert_not_called()

    def test_wizard_system_notification_is_independent_from_remote_choice(self):
        answers = ["", "off", "", "", "4", ""]
        with mock.patch("builtins.input", side_effect=answers), \
                mock.patch.object(
                    checkin_cli, "_run_environment_preflight",
                    return_value=True, create=True,
                ), \
                mock.patch.object(checkin_cli, "write_config") as write_config, \
                mock.patch.object(checkin_cli, "write_plist", return_value="test.plist"), \
                mock.patch.object(checkin_cli, "cmd_wx_bind") as bind, \
                redirect_stdout(StringIO()):
            result = checkin_cli.interactive_config({})

        self.assertEqual(result, 0)
        saved = write_config.call_args.args[0]
        self.assertEqual(saved["notify_channel"], "none")
        self.assertFalse(saved["desktop_notify"])
        bind.assert_not_called()


class EnvironmentPreflightTest(unittest.TestCase):
    def test_codebuddy_login_settings_skip_folder_trust_and_register_hook(self):
        settings = json.loads(checkin_cli._codebuddy_login_settings(
            "/tmp/workbuddy login.done"
        ))

        self.assertTrue(settings["trustAll"])
        notification = settings["hooks"]["Notification"][0]
        self.assertEqual(notification["matcher"], "auth_success")
        self.assertIn(
            "/tmp/workbuddy login.done",
            notification["hooks"][0]["command"],
        )

    def test_codebuddy_auth_signal_without_bearer_token_is_not_success(self):
        process = mock.Mock()
        process.poll.return_value = 0
        with tempfile.TemporaryDirectory() as directory:
            signal_path = pathlib.Path(directory) / "auth-success"
            signal_path.touch()
            with mock.patch.object(
                        checkin_cli, "_workbuddy_token_ready", return_value=False,
                    ):
                result = checkin_cli._wait_for_codebuddy_login(
                    process, str(signal_path), timeout_seconds=0,
                )

        self.assertFalse(result)

    def test_macos_codebuddy_login_types_slash_command_in_cli_terminal(self):
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(checkin_cli.sys, "platform", "darwin"), \
                mock.patch.object(
                    checkin_cli.shutil, "which", return_value="/usr/bin/expect",
                ), \
                mock.patch.object(
                    checkin_cli.subprocess, "Popen", return_value=process,
                ) as popen, \
                mock.patch.object(
                    checkin_cli, "_wait_for_codebuddy_login", return_value=True,
                ), \
                redirect_stdout(StringIO()):
            result = checkin_cli._launch_codebuddy_login_and_wait(
                "/usr/local/bin/codebuddy"
            )

        self.assertTrue(result)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/expect")
        expect_script = command[2]
        self.assertIn('send -s -- "/login"', expect_script)
        self.assertIn('send -- "\\r"', expect_script)
        self.assertIn("Select login method", expect_script)
        self.assertEqual(expect_script.count('send -- "\\r"'), 2)
        self.assertNotIn('send -- "/login\\r"', expect_script)
        self.assertNotIn("spawn -noecho --", expect_script)
        self.assertIn("Tips for getting started", expect_script)
        self.assertLess(
            expect_script.index("Tips for getting started"),
            expect_script.index('send -s -- "/login"'),
        )
        self.assertLess(
            expect_script.index('send -s -- "/login"'),
            expect_script.index("Select login method"),
        )
        self.assertEqual(len(command), 3)
        self.assertNotIn("--serve", command)
        self.assertNotIn("--open", command)
        popen.assert_called_once()
        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(
            child_env["WORKBUDDY_CODEBUDDY_CLI"],
            "/usr/local/bin/codebuddy",
        )
        self.assertIn("trustAll", child_env["WORKBUDDY_CODEBUDDY_SETTINGS"])
        self.assertNotIn("stdin", popen.call_args.kwargs)
        process.terminate.assert_called_once_with()

    def test_windows_codebuddy_login_sends_slash_command_over_stdin(self):
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(checkin_cli.sys, "platform", "win32"), \
                mock.patch.object(
                    checkin_cli.subprocess, "Popen", return_value=process,
                ) as popen, \
                mock.patch.object(
                    checkin_cli, "_wait_for_codebuddy_login", return_value=True,
                ), \
                redirect_stdout(StringIO()):
            result = checkin_cli._launch_codebuddy_login_and_wait(
                "C:\\CodeBuddy\\codebuddy.exe"
            )

        self.assertTrue(result)
        command = popen.call_args.args[0]
        self.assertNotIn("/login", command)
        self.assertNotIn("--serve", command)
        self.assertIs(popen.call_args.kwargs["stdin"], checkin_cli.subprocess.PIPE)
        self.assertTrue(popen.call_args.kwargs["text"])
        process.stdin.write.assert_called_once_with("/login\n")
        process.stdin.flush.assert_called_once_with()

    def test_main_handles_ctrl_c_without_traceback(self):
        output = StringIO()
        with mock.patch.object(
                    checkin_cli, "build_parser",
                    return_value=mock.Mock(
                        parse_args=mock.Mock(return_value=mock.Mock(
                            cmd="wizard",
                            func=mock.Mock(side_effect=KeyboardInterrupt),
                        )),
                    ),
                ), redirect_stdout(output):
            result = checkin_cli.main()

        self.assertEqual(result, 130)
        self.assertIn("已取消", output.getvalue())

    def test_windows_ready_environment_passes_without_workbuddy(self):
        with mock.patch.object(checkin_cli.sys, "platform", "win32"), \
                mock.patch.object(checkin_cli, "_workbuddy_token_ready", return_value=True), \
                mock.patch.object(checkin_cli, "_find_workbuddy_app") as find_app, \
                redirect_stdout(StringIO()):
            result = checkin_cli._run_environment_preflight()

        self.assertTrue(result)
        find_app.assert_not_called()

    def test_ready_environment_passes_without_opening_workbuddy(self):
        preflight = getattr(checkin_cli, "_run_environment_preflight", None)
        self.assertIsNotNone(preflight)
        with mock.patch.object(checkin_cli.sys, "platform", "darwin"), \
                mock.patch.object(
                    checkin_cli, "_find_workbuddy_app",
                    return_value="/Applications/WorkBuddy.app",
                ), \
                mock.patch.object(checkin_cli, "_workbuddy_token_ready", return_value=True), \
                mock.patch.object(checkin_cli.subprocess, "run") as launch, \
                redirect_stdout(StringIO()):
            result = preflight()

        self.assertTrue(result)
        launch.assert_not_called()

    def test_missing_token_can_open_workbuddy_and_wait_until_ready(self):
        preflight = getattr(checkin_cli, "_run_environment_preflight", None)
        self.assertIsNotNone(preflight)
        with mock.patch.object(checkin_cli.sys, "platform", "darwin"), \
                mock.patch.object(
                    checkin_cli, "_find_workbuddy_app",
                    return_value="/Applications/WorkBuddy.app",
                ), \
                mock.patch.object(checkin_cli, "_workbuddy_token_ready", return_value=False), \
                mock.patch.object(
                    checkin_cli, "_wait_for_workbuddy_token", return_value=True,
                ) as wait, \
                mock.patch.object(
                    checkin_cli.subprocess, "run",
                    return_value=mock.Mock(returncode=0),
                ) as launch, \
                mock.patch("builtins.input", return_value=""), \
                redirect_stdout(StringIO()):
            result = preflight()

        self.assertTrue(result)
        launch.assert_called_once_with(
            ["open", "/Applications/WorkBuddy.app"],
            check=False,
            capture_output=True,
        )
        wait.assert_called_once()

    def test_missing_workbuddy_app_uses_standalone_codebuddy_login(self):
        preflight = getattr(checkin_cli, "_run_environment_preflight", None)
        self.assertIsNotNone(preflight)
        with mock.patch.object(checkin_cli.sys, "platform", "darwin"), \
                mock.patch.object(checkin_cli, "_workbuddy_token_ready", return_value=False), \
                mock.patch.object(checkin_cli, "_find_workbuddy_app", return_value=""), \
                mock.patch.object(
                    checkin_cli, "_ensure_codebuddy_cli",
                    return_value="/usr/local/bin/codebuddy", create=True,
                ), \
                mock.patch.object(
                    checkin_cli, "_launch_codebuddy_login_and_wait",
                    return_value=True, create=True,
                ) as login, \
                redirect_stdout(StringIO()):
            result = preflight()

        self.assertTrue(result)
        login.assert_called_once_with("/usr/local/bin/codebuddy")

    def test_user_can_choose_no_workbuddy_mode_when_app_is_installed(self):
        with mock.patch.object(checkin_cli.sys, "platform", "darwin"), \
                mock.patch.object(checkin_cli, "_workbuddy_token_ready", return_value=False), \
                mock.patch.object(
                    checkin_cli, "_find_workbuddy_app",
                    return_value="/Applications/WorkBuddy.app",
                ), \
                mock.patch.object(
                    checkin_cli, "_ensure_codebuddy_cli",
                    return_value="/opt/homebrew/bin/codebuddy", create=True,
                ), \
                mock.patch.object(
                    checkin_cli, "_launch_codebuddy_login_and_wait",
                    return_value=True, create=True,
                ) as login, \
                mock.patch("builtins.input", return_value="2"), \
                mock.patch.object(checkin_cli.subprocess, "run") as workbuddy_launch, \
                redirect_stdout(StringIO()):
            result = checkin_cli._run_environment_preflight()

        self.assertTrue(result)
        login.assert_called_once_with("/opt/homebrew/bin/codebuddy")
        workbuddy_launch.assert_not_called()

    def test_wizard_stops_before_writing_when_preflight_fails(self):
        with mock.patch.object(
                    checkin_cli, "_run_environment_preflight",
                    return_value=False, create=True,
                ), \
                mock.patch("builtins.input", side_effect=["", "", "", "", "4", ""]), \
                mock.patch.object(checkin_cli, "write_config") as write_config, \
                mock.patch.object(
                    checkin_cli, "write_plist", return_value="test.plist",
                ) as write_plist, \
                redirect_stdout(StringIO()):
            result = checkin_cli.interactive_config({})

        self.assertEqual(result, 1)
        write_config.assert_not_called()
        write_plist.assert_not_called()

    def test_install_stops_when_preflight_fails(self):
        with mock.patch.object(
                    checkin_cli, "_run_environment_preflight",
                    return_value=False, create=True,
                ), \
                mock.patch.object(
                    checkin_cli, "write_plist", return_value="test.plist",
                ) as write_plist, \
                mock.patch.object(
                    checkin_cli, "install", return_value=(True, "installed"),
                ) as install, \
                redirect_stdout(StringIO()):
            result = checkin_cli.cmd_install(None)

        self.assertEqual(result, 1)
        write_plist.assert_not_called()
        install.assert_not_called()


class PurgeUninstallTest(unittest.TestCase):
    def test_uninstall_parser_accepts_explicit_purge_flag(self):
        args = checkin_cli.build_parser().parse_args(["uninstall", "--purge"])

        self.assertTrue(args.purge)

    def test_uninstall_parser_accepts_codebuddy_and_noninteractive_flags(self):
        args = checkin_cli.build_parser().parse_args([
            "uninstall", "--purge", "--codebuddy", "--yes",
        ])

        self.assertTrue(args.purge)
        self.assertTrue(args.purge_codebuddy)
        self.assertTrue(args.yes)

    def test_codebuddy_deletion_requires_full_purge(self):
        args = checkin_cli.argparse.Namespace(
            purge=False, purge_codebuddy=True, yes=True,
        )
        output = StringIO()
        with mock.patch.object(checkin_cli, "uninstall") as uninstall, \
                mock.patch.object(checkin_cli, "_purge_codebuddy") as purge, \
                redirect_stdout(output):
            result = checkin_cli.cmd_uninstall(args)

        self.assertEqual(result, 2)
        self.assertIn("--purge", output.getvalue())
        uninstall.assert_not_called()
        purge.assert_not_called()

    def test_codebuddy_deletion_can_be_cancelled_before_any_removal(self):
        args = checkin_cli.argparse.Namespace(
            purge=True, purge_codebuddy=True, yes=False,
        )
        with mock.patch("builtins.input", return_value="n"), \
                mock.patch.object(checkin_cli, "uninstall") as uninstall, \
                mock.patch.object(checkin_cli, "_purge_paths") as purge_paths, \
                mock.patch.object(checkin_cli, "_purge_codebuddy") as purge_codebuddy, \
                redirect_stdout(StringIO()):
            result = checkin_cli.cmd_uninstall(args)

        self.assertEqual(result, 1)
        uninstall.assert_not_called()
        purge_paths.assert_not_called()
        purge_codebuddy.assert_not_called()

    def test_confirmed_codebuddy_deletion_removes_cli_and_login_state(self):
        args = checkin_cli.argparse.Namespace(
            purge=True, purge_codebuddy=True, yes=True,
        )
        with mock.patch.object(
                    checkin_cli, "uninstall", return_value=(True, "removed"),
                ), \
                mock.patch.object(
                    checkin_cli, "_purge_paths", return_value=([], []),
                ), \
                mock.patch.object(
                    checkin_cli, "_purge_codebuddy", return_value=(["cli"], []),
                ) as purge_codebuddy, \
                redirect_stdout(StringIO()):
            result = checkin_cli.cmd_uninstall(args)

        self.assertEqual(result, 0)
        purge_codebuddy.assert_called_once_with()

    def test_codebuddy_purge_targets_include_cli_config_and_shared_login(self):
        with mock.patch.object(checkin_cli.sys, "platform", "darwin"):
            targets = set(checkin_cli._codebuddy_purge_targets())

        self.assertIn(
            checkin_cli.os.path.expanduser("~/.codebuddy"), targets,
        )
        self.assertIn(
            checkin_cli.os.path.expanduser("~/.local/bin/codebuddy"), targets,
        )
        self.assertIn(
            checkin_cli.os.path.expanduser(
                "~/Library/Application Support/CodeBuddyExtension/Data/Public/"
                "auth/workbuddy-desktop.info"
            ),
            targets,
        )

    def test_codebuddy_purge_rejects_unsafe_custom_config_directory(self):
        with mock.patch.dict(
                    checkin_cli.os.environ,
                    {"CODEBUDDY_CONFIG_DIR": checkin_cli.os.path.sep},
                ):
            targets = set(checkin_cli._codebuddy_purge_targets())

        self.assertNotIn(checkin_cli.os.path.abspath(checkin_cli.os.path.sep), targets)

    def test_codebuddy_purge_logs_out_and_uninstalls_npm_package(self):
        with mock.patch.object(
                    checkin_cli, "_find_codebuddy_cli",
                    return_value="/usr/local/bin/codebuddy",
                ), \
                mock.patch.object(
                    checkin_cli, "_logout_codebuddy", return_value=True,
                ) as logout, \
                mock.patch.object(
                    checkin_cli.shutil, "which", return_value="/usr/local/bin/npm",
                ), \
                mock.patch.object(
                    checkin_cli, "_run", return_value=(0, "", ""),
                ) as run, \
                mock.patch.object(
                    checkin_cli, "_purge_paths", return_value=([], []),
                ):
            removed, failures = checkin_cli._purge_codebuddy()

        self.assertEqual(removed, [])
        self.assertEqual(failures, [])
        logout.assert_called_once_with("/usr/local/bin/codebuddy")
        self.assertIn(
            mock.call([
                "/usr/local/bin/npm", "uninstall", "-g",
                checkin_cli.CODEBUDDY_NPM_PACKAGE,
            ]),
            run.call_args_list,
        )

    def test_purge_removes_only_supplied_runtime_files_and_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config = root / "checkin_config.json"
            cache = root / ".wx_test_token.json"
            runtime = root / ".venv-wx-bind"
            source = root / "checkin_cli.py"
            config.write_text("secret")
            cache.write_text("token")
            runtime.mkdir()
            (runtime / "python").write_text("runtime")
            source.write_text("keep")

            removed, failures = checkin_cli._purge_paths([config, cache, runtime])

            self.assertEqual(failures, [])
            self.assertFalse(config.exists())
            self.assertFalse(cache.exists())
            self.assertFalse(runtime.exists())
            self.assertTrue(source.exists())
            self.assertEqual(set(removed), {str(config), str(cache), str(runtime)})

    def test_default_purge_targets_exclude_project_source_files(self):
        targets = set(checkin_cli._purge_targets())

        self.assertIn(checkin_cli.CONFIG_PATH, targets)
        self.assertIn(checkin_cli.PLIST_SRC, targets)
        self.assertIn(checkin_cli.WX_BIND_VENV, targets)
        self.assertIn(
            str(pathlib.Path(checkin_cli.BASE_DIR) / "tests" / "__pycache__"),
            targets,
        )
        self.assertIn(
            str(pathlib.Path(checkin_cli.BASE_DIR) / ".pytest_cache"),
            targets,
        )
        self.assertNotIn(checkin_cli.WORKER, targets)
        self.assertNotIn(str(MODULE_PATH), targets)

    def test_status_after_purge_reports_absent_configuration_not_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_config = pathlib.Path(directory) / "checkin_config.json"
            output = StringIO()
            with mock.patch.object(checkin_cli, "CONFIG_PATH", str(missing_config)), \
                    mock.patch.object(checkin_cli, "read_config", return_value={}), \
                    mock.patch.object(
                        checkin_cli, "launchctl_installed", return_value=(False, "")
                    ), \
                    redirect_stdout(output):
                result = checkin_cli.cmd_status(None)

        status = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("配置状态", status)
        self.assertIn("不存在（已清理）", status)
        self.assertIn("系统通知      : 未配置", status)
        self.assertIn("最大重试      : 未配置", status)
        self.assertIn("退避基数      : 未配置", status)
        self.assertNotIn("兼容模式", status)


class WindowsSupportTest(unittest.TestCase):
    def test_windows_install_creates_daily_and_logon_tasks(self):
        action = checkin_cli.subprocess.list2cmdline([
            checkin_cli.PY, checkin_cli.WORKER,
        ])
        with mock.patch.object(checkin_cli.sys, "platform", "win32"), \
                mock.patch.object(checkin_cli, "read_config", return_value={
                    "_schedule_hour": 7,
                    "_schedule_minute": 5,
                }), \
                mock.patch.object(
                    checkin_cli, "_run",
                    side_effect=[(0, "", ""), (0, "", "")],
                ) as run:
            ok, _ = checkin_cli.install()

        self.assertTrue(ok)
        self.assertEqual(run.call_args_list, [
            mock.call([
                "schtasks", "/Create", "/F",
                "/TN", checkin_cli.WINDOWS_DAILY_TASK,
                "/TR", action,
                "/SC", "DAILY", "/ST", "07:05",
                "/RL", "LIMITED",
            ]),
            mock.call([
                "schtasks", "/Create", "/F",
                "/TN", checkin_cli.WINDOWS_LOGON_TASK,
                "/TR", action,
                "/SC", "ONLOGON",
                "/RL", "LIMITED",
            ]),
        ])

    def test_windows_uninstall_removes_both_tasks(self):
        with mock.patch.object(checkin_cli.sys, "platform", "win32"), \
                mock.patch.object(checkin_cli, "_run", side_effect=[
                    (0, "exists", ""), (0, "deleted", ""),
                    (0, "exists", ""), (0, "deleted", ""),
                ]) as run:
            ok, _ = checkin_cli.uninstall()

        self.assertTrue(ok)
        self.assertEqual(run.call_args_list, [
            mock.call(["schtasks", "/Query", "/TN", checkin_cli.WINDOWS_DAILY_TASK]),
            mock.call(["schtasks", "/Delete", "/F", "/TN", checkin_cli.WINDOWS_DAILY_TASK]),
            mock.call(["schtasks", "/Query", "/TN", checkin_cli.WINDOWS_LOGON_TASK]),
            mock.call(["schtasks", "/Delete", "/F", "/TN", checkin_cli.WINDOWS_LOGON_TASK]),
        ])

    def test_windows_schedule_status_requires_both_tasks(self):
        with mock.patch.object(checkin_cli.sys, "platform", "win32"), \
                mock.patch.object(checkin_cli, "_run", side_effect=[
                    (0, "daily", ""), (0, "logon", ""),
                ]) as run:
            installed, error = checkin_cli.schedule_installed()

        self.assertTrue(installed)
        self.assertEqual(error, "")
        self.assertEqual(run.call_count, 2)

    def test_windows_configuration_does_not_write_macos_plist(self):
        with mock.patch.object(checkin_cli.sys, "platform", "win32"), \
                mock.patch.object(checkin_cli, "write_plist") as write_plist:
            result = checkin_cli._prepare_schedule_definition(9, 10)

        self.assertEqual(result, "")
        write_plist.assert_not_called()

    def test_windows_codebuddy_auth_path_uses_local_app_data(self):
        local_app_data = r"C:\Users\tester\AppData\Local"
        with mock.patch.object(worker.sys, "platform", "win32"), \
                mock.patch.dict(worker.os.environ, {"LOCALAPPDATA": local_app_data}):
            paths = worker._codebuddy_auth_paths()

        self.assertIn(
            worker.os.path.join(
                local_app_data, "CodeBuddyExtension", "Data", "Public", "auth",
                "workbuddy-desktop.info",
            ),
            paths,
        )

    def test_windows_desktop_notification_uses_powershell(self):
        with mock.patch.object(worker.sys, "platform", "win32"), \
                mock.patch.object(worker.subprocess, "run") as run:
            worker.notify("签到通知", "签到成功", {
                "desktop_notify": True,
                "notify_channel": "none",
            })

        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[0], "powershell")
        self.assertIn("-EncodedCommand", command)


class WxBindingVerificationTest(unittest.TestCase):
    @staticmethod
    def _jwt(exp, sub="codebuddy-user"):
        def segment(value):
            raw = json.dumps(value, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        return f"{segment({'alg': 'none'})}.{segment({'exp': exp, 'sub': sub})}.signature"

    def test_codebuddy_auth_file_provides_token_without_workbuddy_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            auth_path = pathlib.Path(directory) / "workbuddy-desktop.info"
            token = self._jwt(int(time.time()) + 3600)
            auth_path.write_text(json.dumps({
                "account": {"uid": "codebuddy-user"},
                "auth": {"accessToken": token},
            }))
            with mock.patch.object(worker, "LOGS_DIR", str(pathlib.Path(directory) / "missing")), \
                    mock.patch.object(worker, "CODEBUDDY_AUTH_PATHS", (str(auth_path),), create=True):
                result = worker.extract_token()

        self.assertEqual(result, (token, "codebuddy-user"))

    def test_expired_codebuddy_auth_token_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            auth_path = pathlib.Path(directory) / "workbuddy-desktop.info"
            auth_path.write_text(json.dumps({
                "account": {"uid": "codebuddy-user"},
                "auth": {"accessToken": self._jwt(int(time.time()) - 60)},
            }))
            with mock.patch.object(worker, "LOGS_DIR", str(pathlib.Path(directory) / "missing")), \
                    mock.patch.object(worker, "CODEBUDDY_AUTH_PATHS", (str(auth_path),), create=True):
                result = worker.extract_token()

        self.assertIsNone(result)

    def test_default_wechat_template_data_matches_time_first_card_style(self):
        data = worker._default_wx_test_data(
            "🎉 WorkBuddy 签到成功",
            "今日积分已领取（或本就已签）",
            "2026-07-26 23:34:56",
        )

        self.assertEqual(data["keyword1"]["value"], "🎉 WorkBuddy 签到成功")
        self.assertEqual(data["keyword2"]["value"], "今日积分已领取（或本就已签）")
        self.assertEqual(data["keyword3"]["value"], "2026-07-26 23:34:56")
        self.assertNotIn("first", data)
        self.assertNotIn("remark", data)

    def test_already_checked_in_message_says_no_automatic_checkin_was_performed(self):
        title, message = worker.classify_message(
            True, False, outcome="already_checked_in",
        )

        self.assertIn("已签到", title)
        self.assertIn("此前已经签到", message)
        self.assertIn("本次未执行自动签到", message)

    def test_status_already_checked_in_returns_distinct_outcome_without_claim(self):
        status_response = {
            "data": {
                "today_checked_in": True,
                "active": True,
                "streak_days": 3,
                "total_credits": 20,
            }
        }
        with mock.patch.object(worker, "extract_token", return_value=("token", "uid")), \
                mock.patch.object(worker, "_post", return_value=(200, status_response)) as post, \
                redirect_stdout(StringIO()):
            result = worker.do_checkin()

        self.assertEqual(result, (True, False, "already_checked_in"))
        post.assert_called_once_with(worker.CHECKIN_STATUS_URL, "token", "uid")

    def test_notify_sends_system_notification_plus_only_selected_remote_channel(self):
        cfg = {
            "notify_channel": "wx_test",
            "desktop_notify": True,
            "pushplus_token": "push-token",
            "wx_test_appid": "wx12345678",
            "wx_test_secret": "secret-value",
            "wx_test_touser": "openid-value",
            "wx_test_template_id": "template-value",
            "notify_webhook_url": "https://example.test/hook",
        }
        with mock.patch.object(worker.sys, "platform", "darwin"), \
                mock.patch.object(worker.subprocess, "run") as desktop, \
                mock.patch.object(worker.urllib.request, "urlopen") as http, \
                mock.patch.object(worker, "_wechat_test_send", return_value=True) as wechat:
            worker.notify("title", "message", cfg)

        desktop.assert_called_once()
        http.assert_not_called()
        wechat.assert_called_once_with("title", "message", cfg)

    def test_test_notification_uses_real_success_style_with_test_marker(self):
        real_title, real_message = worker.classify_message(True, False)

        test_title, test_message = worker.build_test_message()

        self.assertIn(real_title, test_title)
        self.assertIn(real_message, test_message)
        self.assertIn("测试", test_title + test_message)

    def test_transient_failure_notifies_before_waiting_for_retry(self):
        events = []

        def record_notify(title, message, _cfg):
            events.append(("notify", title, message))

        def record_sleep(delay):
            events.append(("sleep", delay))

        with mock.patch.object(worker, "load_config", return_value={
                    "max_retries": 1,
                    "retry_base_delay": 30,
                    "desktop_notify": False,
                }), \
                mock.patch.object(worker, "do_checkin", side_effect=[
                    (False, True, "failed"),
                    (True, False, "checked_in"),
                ]), \
                mock.patch.object(worker, "notify", side_effect=record_notify), \
                mock.patch.object(worker.time, "sleep", side_effect=record_sleep), \
                mock.patch.object(worker.sys, "argv", ["workbuddy_checkin.py"]):
            with self.assertRaises(SystemExit) as exited:
                worker.main()

        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(events[0][0], "notify")
        self.assertIn("正在重试", events[0][1] + events[0][2])
        self.assertEqual(events[1], ("sleep", 30))
        self.assertEqual(events[2][0], "notify")
        self.assertIn("成功", events[2][1] + events[2][2])

    def test_retry_exhaustion_sends_immediate_and_final_failure_notifications(self):
        with mock.patch.object(worker, "load_config", return_value={
                    "max_retries": 1,
                    "retry_base_delay": 30,
                    "desktop_notify": False,
                }), \
                mock.patch.object(worker, "do_checkin", side_effect=[
                    (False, True, "failed"),
                    (False, True, "failed"),
                ]), \
                mock.patch.object(worker, "notify") as send, \
                mock.patch.object(worker.time, "sleep"), \
                mock.patch.object(worker.sys, "argv", ["workbuddy_checkin.py"]):
            with self.assertRaises(SystemExit) as exited:
                worker.main()

        self.assertEqual(exited.exception.code, 1)
        self.assertEqual(send.call_count, 2)
        first_title, first_message, _ = send.call_args_list[0].args
        final_title, final_message, _ = send.call_args_list[1].args
        self.assertIn("正在重试", first_title + first_message)
        self.assertIn("失败", final_title + final_message)
        self.assertIn("超出重试次数", final_message)

    def test_wechat_test_send_reports_incomplete_configuration(self):
        self.assertFalse(worker._wechat_test_send("title", "message", {}))

    def test_wechat_test_send_reports_api_success(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"errcode": 0}'

        cfg = {
            "wx_test_appid": "wx12345678",
            "wx_test_secret": "secret-value",
            "wx_test_touser": "openid-value",
            "wx_test_template_id": "template-value",
        }
        with mock.patch.object(worker, "_get_wx_test_token", return_value="access-token"), \
                mock.patch.object(worker.urllib.request, "urlopen", return_value=Response()):
            self.assertTrue(worker._wechat_test_send("title", "message", cfg))

    def test_status_distinguishes_pushplus_from_bound_wechat_test_account(self):
        cfg = {
            "pushplus_token": "",
            "wx_test_appid": "wx12345678",
            "wx_test_secret": "secret-value",
            "wx_test_touser": "openid-value",
            "wx_test_template_id": "template-value",
        }
        output = StringIO()
        with mock.patch.object(checkin_cli, "read_config", return_value=cfg), \
                mock.patch.object(checkin_cli, "launchctl_installed", return_value=(False, "")), \
                redirect_stdout(output):
            checkin_cli.cmd_status(None)

        status = output.getvalue()
        self.assertIn("pushplus", status)
        self.assertIn("未配置", status)
        self.assertIn("微信测试号", status)
        self.assertIn("已绑定", status)
        self.assertNotIn("微信绑定      : 未绑定", status)


@unittest.skipUnless(sync_playwright, "Playwright is only installed in the wx-bind venv")
class WxTemplateDomTest(unittest.TestCase):
    def test_template_fields_and_submit_are_scoped_to_visible_dialog(self):
        html = """
        <main>
          <section id="background-form">
            <div><input id="api-url" type="text" value="background-value"></div>
            <button id="background-submit">提交</button>
          </section>
          <div class="mask">
            <div class="dialog">
              <h2>新增测试模板</h2>
              <p>模板标题</p>
              <div><input id="field-1" type="text"></div>
              <p>模板内容</p>
              <div><textarea id="field-2"></textarea></div>
              <button id="template-submit" onclick="window.templateSubmitted = true">提交</button>
              <button>取消</button>
            </div>
          </div>
        </main>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            page.set_content(html)

            self.assertTrue(checkin_cli._fill_and_submit_template(page))
            self.assertEqual(page.locator("#api-url").input_value(), "background-value")
            self.assertEqual(page.locator("#field-1").input_value(), "签到通知")
            self.assertEqual(
                page.locator("#field-2").input_value(),
                checkin_cli.WX_TEMPLATE_CONTENT,
            )
            self.assertTrue(page.evaluate("window.templateSubmitted === true"))
            browser.close()


if __name__ == "__main__":
    unittest.main()
