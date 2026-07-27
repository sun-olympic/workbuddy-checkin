import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workbuddy_platform import (
    SchedulerSettings,
    UnsupportedPlatformError,
    get_platform,
)


class PlatformSelectionTest(unittest.TestCase):
    def test_platform_selection_returns_distinct_adapters(self):
        settings = SchedulerSettings(
            python_executable="python",
            worker_path="worker.py",
            label="com.example.checkin",
            plist_path="agent.plist",
            daily_task="DailyTask",
            logon_task="LogonTask",
        )

        macos = get_platform("darwin", settings)
        windows = get_platform("win32", settings)

        self.assertEqual(macos.display_name, "macOS")
        self.assertEqual(windows.display_name, "Windows")
        self.assertNotEqual(type(macos), type(windows))

    def test_unsupported_platform_is_rejected_at_the_seam(self):
        with self.assertRaises(UnsupportedPlatformError):
            get_platform("linux")


class WindowsPlatformTest(unittest.TestCase):
    def setUp(self):
        self.settings = SchedulerSettings(
            python_executable=r"C:\Python\python.exe",
            worker_path=r"C:\workbuddy\worker.py",
            label="com.example.checkin",
            plist_path="",
            daily_task="DailyTask",
            logon_task="LogonTask",
        )
        self.platform = get_platform("win32", self.settings)

    def test_scheduler_install_is_owned_by_windows_adapter(self):
        run = mock.Mock(side_effect=[(0, "", ""), (0, "", "")])

        ok, _ = self.platform.install_schedule(
            {"_schedule_hour": 7, "_schedule_minute": 5}, run,
        )

        self.assertTrue(ok)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][:4], [
            "schtasks", "/Create", "/F", "/TN",
        ])
        self.assertIn("07:05", run.call_args_list[0].args[0])

    def test_runtime_paths_use_windows_layout(self):
        with mock.patch.dict(os.environ, {
            "LOCALAPPDATA": r"C:\Users\tester\AppData\Local",
        }):
            paths = self.platform.codebuddy_auth_paths(("auth.info",))

        self.assertIn(
            os.path.join(
                r"C:\Users\tester\AppData\Local",
                "CodeBuddyExtension", "Data", "Public", "auth", "auth.info",
            ),
            paths,
        )
        self.assertTrue(
            self.platform.wx_bind_python("venv").endswith(
                os.path.join("Scripts", "python.exe")
            )
        )


class MacOSPlatformTest(unittest.TestCase):
    def test_scheduler_prepare_is_owned_by_macos_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            plist_path = str(Path(directory) / "agent.plist")
            settings = SchedulerSettings(
                python_executable="/usr/bin/python3",
                worker_path="/workbuddy/worker.py",
                label="com.example.checkin",
                plist_path=plist_path,
                daily_task="DailyTask",
                logon_task="LogonTask",
            )
            platform = get_platform("darwin", settings)
            write_plist = mock.Mock(return_value=plist_path)

            result = platform.prepare_schedule(9, 10, write_plist)

        self.assertEqual(result, plist_path)
        write_plist.assert_called_once_with(9, 10)
        self.assertTrue(
            platform.wx_bind_python("venv").endswith(
                os.path.join("bin", "python3")
            )
        )


if __name__ == "__main__":
    unittest.main()
