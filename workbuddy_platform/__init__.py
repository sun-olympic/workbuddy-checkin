from .common import SchedulerSettings, UnsupportedPlatformError


def get_platform(platform_name, scheduler_settings=None):
    if platform_name == "darwin":
        from .macos import MacOSPlatform
        return MacOSPlatform(scheduler_settings)
    if platform_name == "win32":
        from .windows import WindowsPlatform
        return WindowsPlatform(scheduler_settings)
    raise UnsupportedPlatformError(
        "unsupported platform: {}".format(platform_name)
    )


__all__ = [
    "SchedulerSettings",
    "UnsupportedPlatformError",
    "get_platform",
]
