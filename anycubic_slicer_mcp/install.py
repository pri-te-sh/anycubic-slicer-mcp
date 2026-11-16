"""Where Anycubic Slicer Next and this server keep their files, per operating system."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP = "AnycubicSlicerNext"


def system_name() -> str:
    """'macos' or 'windows' (anything else is treated like macOS-style paths but unsupported)."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "other"


@dataclass(frozen=True)
class Install:
    """Resolved locations of one Anycubic Slicer Next installation."""

    executable: Path
    data_dir: Path
    os_name: str

    @property
    def app_bundle(self) -> Path | None:
        # .../AnycubicSlicerNext.app/Contents/MacOS/AnycubicSlicerNext -> .../AnycubicSlicerNext.app
        if self.os_name == "macos" and self.executable.parent.name == "MacOS":
            return self.executable.parent.parent.parent
        return None

    @property
    def bundled_profiles(self) -> Path:
        """Vendor profiles shipped inside the app; used until the slicer has copied them to data_dir/system."""
        if self.os_name == "macos":
            return self.executable.parent.parent / "Resources" / "profiles"
        return self.executable.parent / "resources" / "profiles"

    @property
    def settings_names_file(self) -> Path:
        """Compiled file that contains every setting key the slicer understands."""
        if self.os_name == "macos":
            return self.executable
        return self.executable.parent / "AnycubicSlicer.dll"

    @property
    def config_file(self) -> Path:
        return self.data_dir / f"{APP}.conf"


def default_executable(os_name: str | None = None) -> Path:
    os_name = os_name or system_name()
    if os_name == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        return Path(program_files) / APP / f"{APP}.exe"
    return Path("/Applications") / f"{APP}.app" / "Contents" / "MacOS" / APP


def default_data_dir(os_name: str | None = None) -> Path:
    os_name = os_name or system_name()
    if os_name == "windows":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP
    return Path.home() / "Library" / "Application Support" / APP


def find_install() -> Install:
    """Environment variables win; otherwise the standard install locations for this OS."""
    os_name = system_name()
    exe = os.environ.get("ANYCUBIC_SLICER_EXE") or default_executable(os_name)
    data = os.environ.get("ANYCUBIC_SLICER_DATA") or default_data_dir(os_name)
    return Install(executable=Path(exe), data_dir=Path(data), os_name=os_name)


def state_dir() -> Path:
    """This server's own folder (spool inventory and similar), separate from the slicer's files."""
    override = os.environ.get("ANYCUBIC_MCP_STATE_DIR")
    if override:
        return Path(override)
    if system_name() == "windows":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path.home() / "Library" / "Application Support"
    return base / "anycubic-slicer-mcp"
