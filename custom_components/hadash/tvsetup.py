"""The adb steps of "Add a Fire TV", with no Home Assistant parts, so they can also be tried from a computer.

firetv.py gives them the app file, the dashboard address and Home Assistant's adb key.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

PACKAGE = "com.lukaps.wall"
ACTIVITY = f"{PACKAGE}/.MainActivity"
ADB_PORT = 5555
# How long the TV's "Allow USB debugging?" question may wait for the person.
ALLOW_WAIT_S = 90.0
# The screensaver start times the studio offers, in minutes. 0 leaves the TV's own setting.
SAVER_MINUTES = (0, 5, 10, 15, 30, 60)
STEPS = ("connect", "allow", "install", "permission", "saver", "open")
TMP_APK = "/data/local/tmp/hadash-wall.apk"


class SetupError(Exception):
    """A setup step failed. code is what the studio explains: unreachable, declined, old_app, no_app, failed."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def installed_code(dumpsys: str) -> int | None:
    """The installed wall app's version code from "dumpsys package", or None when it is not installed."""
    m = re.search(r"versionCode=(\d+)", dumpsys)
    return int(m.group(1)) if m else None


def install_problem(output: str) -> str | None:
    """None when "pm install" worked; otherwise the error code for the studio."""
    if "Success" in output:
        return None
    if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in output or "signatures do not match" in output:
        return "old_app"
    return "failed"



async def set_up(
    host: str,
    signer: Any,
    app: dict[str, Any],
    url: str,
    saver_minutes: int,
    replace: bool,
    report: Callable[[dict[str, Any]], None],
    device_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Connects (the TV asks "Allow USB debugging?" the first time), then installs, allows and opens the app.
    app is {version, code, path}; report gets {step, state, ...} for each step."""
    from adb_shell.adb_device_async import AdbDeviceTcpAsync
    from adb_shell.exceptions import AdbTimeoutError, DeviceAuthError, TcpTimeoutException

    def step(key: str, state: str = "active", **extra: Any) -> None:
        report({"step": key, "state": state, **extra})

    step("connect")
    device = device_factory(host) if device_factory else AdbDeviceTcpAsync(host, ADB_PORT, default_transport_timeout_s=8.0)
    asked = False

    def asking(_: Any) -> None:
        nonlocal asked
        asked = True
        step("connect", "done")
        step("allow")

    try:
        await device.connect(rsa_keys=[signer], auth_timeout_s=ALLOW_WAIT_S, auth_callback=asking)
    except (OSError, TcpTimeoutException) as err:
        if asked:
            raise SetupError("declined", str(err)) from err
        raise SetupError("unreachable", str(err)) from err
    except (AdbTimeoutError, DeviceAuthError) as err:
        raise SetupError("declined", str(err)) from err
    if not asked:
        step("connect", "done")
    step("allow", "done")

    try:
        return await _after_connect(device, host, url, saver_minutes, replace, app, step)
    finally:
        try:
            await device.close()
        except Exception:  # noqa: BLE001 - the setup result matters, not a tidy close
            pass


async def _after_connect(
    device: Any,
    host: str,
    url: str,
    saver_minutes: int,
    replace: bool,
    app: dict[str, Any],
    step: Callable[..., None],
) -> dict[str, Any]:
    async def sh(cmd: str, timeout: float = 30.0) -> str:
        out = await device.shell(cmd, read_timeout_s=timeout, timeout_s=timeout)
        return out if isinstance(out, str) else str(out or "")

    name = (await sh("settings get global device_name")).strip()
    model = (await sh("getprop ro.product.model")).strip()
    tv = "leanback" in await sh("pm list features")
    if not name or name == "null":
        name = model or host
    step("connect", "done", name=name, model=model, tv=tv)

    # The app: install it when it is missing or older than the one Home Assistant serves.
    step("install")
    before = installed_code(await sh(f"dumpsys package {PACKAGE} | grep versionCode"))
    if before is not None and replace:
        await sh(f"pm uninstall {PACKAGE}")
        before = None
    if before is None or before < app["code"]:
        await device.push(str(app["path"]), TMP_APK, read_timeout_s=60.0)
        result = await sh(f"pm install -r {TMP_APK}", timeout=120.0)
        await sh(f"rm -f {TMP_APK}")
        problem = install_problem(result)
        if problem:
            raise SetupError(problem, result.strip()[-300:])
        step("install", "done", version=app["version"])
    else:
        step("install", "done", version=app["version"], kept=True)

    # Over other apps, and out of battery saving, so the helper is still there when the TV rests.
    step("permission")
    await sh(f"appops set {PACKAGE} SYSTEM_ALERT_WINDOW allow")
    await sh(f"dumpsys deviceidle whitelist +{PACKAGE}")
    step("permission", "done")

    # Amazon's screensaver start: the moment the dashboard comes up. Only on a TV, and only when asked.
    if tv and saver_minutes in SAVER_MINUTES and saver_minutes > 0:
        step("saver")
        ms = saver_minutes * 60_000
        await sh(f"settings put system screen_off_timeout {ms}")
        await sh(f"settings put secure contextual_screen_off_timeout {ms}")
        step("saver", "done", minutes=saver_minutes)
    else:
        step("saver", "skip")

    # Open the dashboard. A new app takes this address; an app that is set up already keeps its own screen.
    step("open")
    await sh(f"am start -n {ACTIVITY} --es url '{url}'")
    step("open", "done", kept=before is not None)

    return {"name": name, "model": model, "tv": tv, "url": url, "kept": before is not None}


