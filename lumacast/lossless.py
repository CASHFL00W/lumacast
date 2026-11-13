"""Anbindung an Lossless Scaling (separat auf Steam erhältlich) für Frame Generation."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

EXE_NAME = "LosslessScaling.exe"
_NO_WINDOW = 0x08000000


def _steam_libraries() -> list[Path]:
    if sys.platform != "win32":
        return []
    roots = []
    try:
        import winreg
        for hive, key, value in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    roots.append(Path(winreg.QueryValueEx(k, value)[0]))
            except OSError:
                pass
    except ImportError:
        pass
    roots.append(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam")
    libs: list[Path] = []
    for root in roots:
        if root not in libs:
            libs.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in re.findall(r'"path"\s+"([^"]+)"', text):
            p = Path(match.replace("\\\\", "\\"))
            if p not in libs:
                libs.append(p)
    return libs


def find_lossless(custom: str = "") -> str:
    if custom and Path(custom).is_file():
        return custom
    for lib in _steam_libraries():
        exe = lib / "steamapps" / "common" / "Lossless Scaling" / EXE_NAME
        if exe.is_file():
            return str(exe)
    return ""


def is_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}", "/NH"],
                             capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=5).stdout
        return EXE_NAME.lower() in out.lower()
    except Exception:
        return False


def _pid() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=5).stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) > 1 and parts[0].lower() == EXE_NAME.lower():
                return int(parts[1])
    except Exception:
        pass
    return None


def self_is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_elevated() -> bool:
    """True, wenn Lossless Scaling mit Administratorrechten läuft."""
    pid = _pid()
    if pid is None:
        return False
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    adv = ctypes.windll.advapi32
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return True  # kein Zugriff -> läuft mit höheren Rechten
    try:
        token = wintypes.HANDLE()
        if not adv.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return True
        try:
            elevation = wintypes.DWORD()
            size = wintypes.DWORD()
            ok = adv.GetTokenInformation(token, 20, ctypes.byref(elevation), ctypes.sizeof(elevation),
                                         ctypes.byref(size))  # TokenElevation
            return bool(ok and elevation.value)
        finally:
            k32.CloseHandle(token)
    finally:
        k32.CloseHandle(handle)


def hotkey_blocked() -> bool:
    """Windows blockiert Tastendrücke an Programme mit höheren Rechten."""
    try:
        return is_elevated() and not self_is_admin()
    except Exception:
        return False


def restart_as_admin() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    exe = sys.executable
    base = [] if getattr(sys, "frozen", False) else sys.argv[:1]
    args = " ".join(f'"{a}"' for a in base + ["--restarted"])
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1) > 32


def launch(path: str) -> str:
    """Startet Lossless Scaling, falls nötig. Gibt eine Fehlermeldung oder '' zurück."""
    if is_running():
        return ""
    if not path:
        return "Lossless Scaling wurde nicht gefunden. Pfad unter „Frame Gen“ angeben."
    try:
        os.startfile(path)  # type: ignore[attr-defined]  # startet ggf. mit Admin-Abfrage
        time.sleep(2.5)
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"Lossless Scaling konnte nicht gestartet werden: {exc}"


# ------------------------------------------------------------------ Hotkey senden
_VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "home": 0x24, "end": 0x23,
    "insert": 0x2D, "delete": 0x2E, "pageup": 0x21, "pagedown": 0x22,
}
for _i in range(1, 13):
    _VK[f"f{_i}"] = 0x6F + _i


def _vk(token: str) -> int | None:
    token = token.strip().lower()
    if token in _VK:
        return _VK[token]
    if len(token) == 1 and token.isalnum():
        return ord(token.upper())
    return None


def send_hotkey(combo: str) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    keys = [_vk(t) for t in combo.split("+")]
    if not keys or any(k is None for k in keys):
        return False
    user32 = ctypes.windll.user32
    for k in keys:
        scan = user32.MapVirtualKeyW(k, 0)
        user32.keybd_event(k, scan, 0, 0)
        time.sleep(0.03)
    time.sleep(0.05)
    for k in reversed(keys):
        scan = user32.MapVirtualKeyW(k, 0)
        user32.keybd_event(k, scan, 2, 0)  # KEYEVENTF_KEYUP
        time.sleep(0.03)
    return True
