"""Profile und Einstellungen von LumaCast."""
from __future__ import annotations

import copy
import json
import os
import sys
import uuid
from pathlib import Path

APP_NAME = "LumaCast"
APP_VERSION = "1.1.3"

TEST_PATTERN = "Testbild (ohne Capture-Karte)"
DEFAULT_AUDIO = "Standard"

DEFAULT_PROFILE: dict = {
    "id": "",
    "name": "Standard",
    "description": "",
    "video": {
        "device": "",          # Name des Capture-Geräts
        "width": 1920,
        "height": 1080,
        "fps": 60,
        "format": "Auto",      # Auto, YUY2, NV12, MJPG
    },
    "output": {
        "monitor": 0,           # Index des Monitors
        "scale_mode": "fit",    # fit, fill, stretch, integer
        "vsync": False,
        "overlay": False,
        "esc_closes": True,
        "skip_duplicates": True,
        "start_fullscreen": False,  # False = startet im Fenster (F11 / Doppelklick = Vollbild)  # doppelte Frames nicht erneut ausgeben
    },
    "upscale": {
        "method": "lanczos",    # nearest, bilinear, bicubic, lanczos
        "sharpness": 40,        # 0..100 (0 = aus)
        "anti_ringing": True,
    },
    "image": {
        "mode": "original",     # original, manual, auto, hybrid
        "input_range": "full",  # full, limited
        "brightness": 0,        # -100..100
        "contrast": 0,
        "saturation": 0,
        "vibrance": 0,
        "gamma": 0,
        "temperature": 0,
        "black_level": 0,
        "auto_strength": 60,    # 0..100
    },
    "audio": {
        "enabled": True,
        "input": "",
        "output": DEFAULT_AUDIO,
        "volume": 100,
    },
    "framegen": {
        "lossless": False,
        "ls_path": "",
        "send_hotkey": True,
        "hotkey": "ctrl+alt+s",
        "delay": 2.0,
    },
}

# Abschnitte, deren Änderung einen Neustart der Ausgabe braucht
RESTART_SECTIONS = ("video", "audio", "framegen")
RESTART_KEYS = (("output", "monitor"), ("output", "vsync"))


def data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        path = Path(base) / APP_NAME
    else:
        path = Path.home() / f".{APP_NAME.lower()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def profiles_path() -> Path:
    return data_dir() / "profiles.json"


def resource_path(name: str) -> str:
    """Pfad zu mitgelieferten Dateien (funktioniert auch in der .exe)."""
    base = getattr(sys, "_MEIPASS", None) or str(Path(__file__).resolve().parent.parent)
    return str(Path(base) / "assets" / name)


def _merge(defaults: dict, data: dict) -> dict:
    out = copy.deepcopy(defaults)
    for key, value in (data or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def new_profile(name: str = "Neues Profil", base: dict | None = None) -> dict:
    profile = _merge(DEFAULT_PROFILE, base or {})
    profile["id"] = uuid.uuid4().hex[:10]
    profile["name"] = name
    return profile


def normalize(profile: dict) -> dict:
    p = _merge(DEFAULT_PROFILE, profile)
    if not p.get("id"):
        p["id"] = uuid.uuid4().hex[:10]
    return p


def load_store() -> dict:
    path = profiles_path()
    store = {"active": "", "profiles": []}
    if path.exists():
        try:
            store.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    store["profiles"] = [normalize(p) for p in store.get("profiles", []) if isinstance(p, dict)]
    if not store["profiles"]:
        first = new_profile("Standard")
        first["description"] = "1080p60, Lanczos + Schärfen"
        store["profiles"] = [first]
    ids = {p["id"] for p in store["profiles"]}
    if store.get("active") not in ids:
        store["active"] = store["profiles"][0]["id"]
    return store


def save_store(store: dict) -> None:
    path = profiles_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def needs_restart(old: dict, new: dict) -> bool:
    for section in RESTART_SECTIONS:
        a = {k: v for k, v in old.get(section, {}).items() if k != "volume"}
        b = {k: v for k, v in new.get(section, {}).items() if k != "volume"}
        if a != b:
            return True
    for section, key in RESTART_KEYS:
        if old.get(section, {}).get(key) != new.get(section, {}).get(key):
            return True
    return False
