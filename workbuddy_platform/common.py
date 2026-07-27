from dataclasses import dataclass


class UnsupportedPlatformError(RuntimeError):
    pass


@dataclass(frozen=True)
class SchedulerSettings:
    python_executable: str
    worker_path: str
    label: str
    plist_path: str
    daily_task: str
    logon_task: str
