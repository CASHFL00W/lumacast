"""Updates direkt aus dem Programm: neue Version auf GitHub finden, laden und einspielen."""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from .config import APP_NAME, APP_VERSION

REPO = "CASHFL00W/lumacast"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "LumaCast.exe"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (Updater)"
_NO_WINDOW = 0x08000000
_DETACHED = 0x00000008


def parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3]) or (0,)


def is_newer(remote: str, local: str | None = None) -> bool:
    return parse_version(remote) > parse_version(local or APP_VERSION)


def can_self_update() -> bool:
    """Nur die fertige .exe kann sich selbst ersetzen."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def exe_path() -> Path:
    return Path(sys.executable).resolve()


def _open(url: str, timeout: float = 15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context())


def check() -> dict | None:
    """Gibt Infos zur neuesten Version zurück, wenn sie neuer ist – sonst None."""
    with _open(API_URL) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = data.get("tag_name", "")
    if not is_newer(tag):
        return None
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    digest = (asset or {}).get("digest") or ""
    return {
        "version": tag.lstrip("vV"),
        "page": data.get("html_url", f"https://github.com/{REPO}/releases/latest"),
        "url": (asset or {}).get("browser_download_url", ""),
        "size": int((asset or {}).get("size") or 0),
        "sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else "",
        "notes": (data.get("body") or "").strip(),
    }


def _download_target() -> Path:
    """Neben der .exe ablegen (gleiches Laufwerk), sonst im Temp-Ordner."""
    folder = exe_path().parent
    target = folder / f"{exe_path().stem}.update.exe"
    try:
        with open(target, "wb"):
            pass
        return target
    except OSError:
        return Path(tempfile.gettempdir()) / f"{APP_NAME}.update.exe"


def download(info: dict, progress=None, cancelled=None) -> Path:
    """Lädt die neue .exe herunter und prüft sie. progress(geladen, gesamt)."""
    if not info.get("url"):
        raise RuntimeError("Im Release wurde keine LumaCast.exe gefunden.")
    target = _download_target()
    sha = hashlib.sha256()
    done = 0
    try:
        with _open(info["url"], timeout=30) as resp, open(target, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or info.get("size") or 0)
            while True:
                if cancelled and cancelled():
                    raise RuntimeError("Abgebrochen.")
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if info.get("size") and done != info["size"]:
            raise RuntimeError("Download unvollständig – bitte erneut versuchen.")
        if info.get("sha256") and sha.hexdigest() != info["sha256"]:
            raise RuntimeError("Prüfsumme stimmt nicht – Download beschädigt.")
        with open(target, "rb") as fh:
            if fh.read(2) != b"MZ":
                raise RuntimeError("Heruntergeladene Datei ist keine gültige .exe.")
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return target


def install_and_restart(new_exe: Path) -> None:
    """Startet ein kleines Skript, das nach dem Beenden die .exe ersetzt und neu startet.
    Danach muss das Programm sich selbst beenden."""
    current = exe_path()
    script = Path(tempfile.gettempdir()) / f"{APP_NAME}_update.cmd"
    log = Path(tempfile.gettempdir()) / f"{APP_NAME}_update.log"
    # Skript bleibt rein ASCII; Pfade kommen als Argumente (Unicode-sicher, auch mit Umlauten)
    script.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "set \"NEW=%~1\"\r\n"
        "set \"EXE=%~2\"\r\n"
        "set \"LOG=%~3\"\r\n"
        "echo Update gestartet > \"%LOG%\"\r\n"
        "rem Warten, bis LumaCast beendet ist und die Datei frei wird (max. ~60 s)\r\n"
        "for /l %%i in (1,1,60) do (\r\n"
        "  move /y \"%NEW%\" \"%EXE%\" >nul 2>&1 && goto ok\r\n"
        "  ping -n 2 127.0.0.1 >nul\r\n"
        ")\r\n"
        "echo Ersetzen fehlgeschlagen >> \"%LOG%\"\r\n"
        "goto end\r\n"
        ":ok\r\n"
        "echo Ersetzt >> \"%LOG%\"\r\n"
        "start \"\" \"%EXE%\"\r\n"
        ":end\r\n"
        "endlocal\r\n"
        "(goto) 2>nul & del \"%~f0\"\r\n",
        encoding="ascii",
    )
    # Äußere Anführungszeichen, damit cmd /c auch Pfade mit Leerzeichen korrekt liest
    cmdline = f'cmd.exe /c ""{script}" "{new_exe}" "{current}" "{log}""'
    subprocess.Popen(cmdline, creationflags=_NO_WINDOW | _DETACHED, close_fds=True,
                     cwd=tempfile.gettempdir())


def cleanup_leftovers() -> None:
    """Reste eines abgebrochenen Updates entfernen."""
    if not can_self_update():
        return
    for path in (exe_path().parent / f"{exe_path().stem}.update.exe",
                 Path(tempfile.gettempdir()) / f"{APP_NAME}.update.exe"):
        try:
            if path.exists() and path != exe_path():
                path.unlink()
        except OSError:
            pass


def open_release_page(url: str = "") -> None:
    import webbrowser
    webbrowser.open(url or f"https://github.com/{REPO}/releases/latest")


if __name__ == "__main__":  # schneller Test: python -m lumacast.updater
    print(os.path.basename(sys.executable), APP_VERSION, check())
