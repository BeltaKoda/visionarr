#!/usr/bin/env python3
"""
dovi_convert - Dolby Vision Profile 7 -> 8.1 Converter

DESCRIPTION:
  Automates conversion of Dolby Vision Profile 7 MKV files (UHD Blu-ray)
  into Profile 8.1. This ensures compatibility with devices that do not support
  the Enhancement Layer.

"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# =============================================================================
# CONSTANTS
# =============================================================================

VERSION = "7.3.2"
REPO_URL = "https://api.github.com/repos/cryptochrome/dovi_convert/releases/latest"

# ANSI Colors
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DEFAULT = "\033[39m"
RESET = "\033[0m"

# Cache directory
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "dovi_convert"
UPDATE_FILE = CACHE_DIR / "latest_version"

# Dependency map: command -> (brew, apt, dnf, pacman)
DEP_MAP = {
    "mkvmerge": ("mkvtoolnix", "mkvtoolnix", "mkvtoolnix", "mkvtoolnix"),
    "mkvextract": ("mkvtoolnix", "mkvtoolnix", "mkvtoolnix", "mkvtoolnix"),
    "dovi_tool": ("dovi_tool", "dovi_tool", "dovi_tool", "dovi_tool"),
    "mediainfo": ("mediainfo", "mediainfo", "mediainfo", "mediainfo"),
    "ffmpeg": ("ffmpeg", "ffmpeg", "ffmpeg", "ffmpeg"),
}

# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class VideoInfo:
    """Video track metadata from mkvmerge/mediainfo."""
    track_id: Optional[int] = None
    delay: int = 0
    language: str = "und"
    name: str = ""
    mi_info_string: str = ""
    fps: str = ""
    frame_count: int = 0
    mkvmerge_error: str = ""


@dataclass
class ScanResult:
    """Result of FEL complexity analysis."""
    verdict: str = "UNKNOWN"  # SAFE, COMPLEX, UNKNOWN
    reason: str = "Analysis failed"


@dataclass
class Config:
    """Runtime configuration flags."""
    debug_mode: bool = False
    safe_mode: bool = False
    force_mode: bool = False
    auto_yes: bool = False
    include_simple: bool = False
    delete_backup: bool = False
    candidates_only: bool = False
    hdr10_mode: bool = False
    temp_dir: Optional[Path] = None
    output_dir: Optional[Path] = None


@dataclass
class ConversionMetrics:
    """Metrics for a conversion operation."""
    start_time: float = 0.0
    orig_size: int = 0
    frame_count: int = 0
    fps: str = ""


@dataclass
class BatchStats:
    """Statistics for batch processing."""
    success_list: List[str] = field(default_factory=list)
    fail_list: List[str] = field(default_factory=list)
    ignored_count: int = 0
    skipped_count: int = 0
    complex_count: int = 0
    simple_count: int = 0
    forced_count: int = 0


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def version_gt(v1: str, v2: str) -> bool:
    """
    Returns True if v1 > v2 (semantic version comparison).
    Logic:
    1. Compare numeric base versions (e.g. 7.0.0 > 6.6.4).
    2. If base versions equal: Stable (no suffix) > Pre-release (with suffix).
       e.g. 7.0.0 > 7.0.0-beta1
    """
    def parse(v):
        v = v.lstrip("v")
        if "-" in v:
            base, suffix = v.split("-", 1)
            return base, suffix
        return v, ""

    base1, suff1 = parse(v1)
    base2, suff2 = parse(v2)

    try:
        p1 = [int(x) for x in base1.split(".")]
        p2 = [int(x) for x in base2.split(".")]
    except ValueError:
        return False

    # 1. Numeric Comparison
    if p1 > p2:
        return True
    if p1 < p2:
        return False

    # 2. Suffix Comparison (Base versions are equal)
    # If v1 has NO suffix (stable) and v2 HAS suffix (beta), v1 is newer.
    if not suff1 and suff2:
        return True

    return False





def is_wsl() -> bool:
    """Check if running in Windows Subsystem for Linux."""
    try:
        if Path("/proc/version").exists():
            with open("/proc/version", "r") as f:
                return "microsoft" in f.read().lower()
    except Exception:
        pass
    return False


def check_wsl_path_limit(filepath: Path) -> bool:
    """
    Check if a file in WSL is on a Windows drive and exceeds the 255 char limit.
    Uses /proc/mounts to robustly identify Windows filesystems (drvfs/9p).
    Fails HARD if system mounts cannot be read.
    """
    try:
        abs_path = str(filepath.resolve())
        best_match = None

        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    mount_point = parts[1]
                    fs_type = parts[2]

                    if abs_path.startswith(mount_point):
                        # Keep the most specific mount point
                        if best_match is None or len(mount_point) > len(best_match[0]):
                             best_match = (mount_point, fs_type)

        if best_match:
            fs_type = best_match[1]
            if fs_type in ("drvfs", "9p") and len(abs_path) > 255:
                # Confirmed: Windows Drive + Long Path
                return True

    except Exception as e:
        # Fail Hard on Broken System
        print(f"\n{RED}Critical Error: Can't read /proc/mounts.{RESET}")
        print(f"{RED}Your WSL environment appears broken or is not supported.{RESET}")
        print(f"Debug: {e}")
        sys.exit(1)

    return False


def human_size_gb(size_bytes: int) -> str:
    """Convert bytes to human readable GB string."""
    gb = size_bytes / 1024 / 1024 / 1024
    return f"{gb:.2f} GB"


def get_file_size(filepath: Path) -> int:
    """Get file size in bytes."""
    try:
        return filepath.stat().st_size
    except OSError:
        return 0


def pq_to_nits(code_val: int) -> int:
    """Convert PQ code value (0-4095) to nits using ST.2084 EOTF."""
    if code_val <= 0:
        return 0

    # ST.2084 Constants
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 32.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 128.0
    c3 = 2392.0 / 128.0

    # Normalize 12-bit code value (0-4095) to 0-1
    V = code_val / 4095.0

    if V <= 0:
        return 0

    try:
        # Calculate V^(1/m2)
        vp = math.pow(V, 1.0 / m2)

        # Calculate max(vp - c1, 0)
        num = max(vp - c1, 0)

        # Calculate c2 - c3*vp
        den = c2 - c3 * vp
        if den == 0:
            den = 0.000001

        # Calculate R = (num / den)^(1/m1)
        base_val = max(num / den, 0)

        nits = 10000.0 * math.pow(base_val, 1.0 / m1)
        return int(round(nits))
    except (ValueError, OverflowError):
        return 0


def send_notification(title: str, message: str) -> None:
    """Send macOS notification if available."""
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{message}" with title "{title}" sound name "Glass"'],
                capture_output=True,
                timeout=5
            )
        except Exception:
            pass


# =============================================================================
# SPINNER CLASS
# =============================================================================

class Spinner:
    """Animated spinner with elapsed time display."""

    def __init__(self, label: str):
        self.label = label
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.start_time = 0.0

        # Check for UTF-8 support
        lang = os.environ.get("LANG", "") + os.environ.get("LC_ALL", "")
        self.use_braille = "UTF-8" in lang.upper()

        if self.use_braille:
            self.spinstr = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        else:
            self.spinstr = "|/-\\"

    def _spin(self) -> None:
        idx = 0
        while self.running:
            elapsed = int(time.time() - self.start_time)
            minutes = elapsed // 60
            seconds = elapsed % 60
            char = self.spinstr[idx % len(self.spinstr)]

            # Clear line and print
            sys.stdout.write(f"\r\033[K{self.label} {char} ({minutes}m {seconds:02d}s)")
            sys.stdout.flush()

            idx += 1
            time.sleep(0.1)

    def start(self) -> None:
        """Start the spinner."""
        self.running = True
        self.start_time = time.time()
        # Hide cursor
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop the spinner."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
        # Show cursor
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


# =============================================================================
# DEPENDENCY MANAGER
# =============================================================================

class DependencyManager:
    """Handles dependency checking and auto-installation."""

    PM_INDEX = {"brew": 0, "apt": 1, "dnf": 2, "pacman": 3}

    @staticmethod
    def get_pkg_name(cmd: str, pm: str) -> str:
        """Get package name for command and package manager."""
        if cmd in DEP_MAP:
            idx = DependencyManager.PM_INDEX.get(pm, 0)
            return DEP_MAP[cmd][idx]
        return cmd

    @staticmethod
    def check_pkg_available(pkg: str, pm: str) -> bool:
        """Check if package is available in repos."""
        try:
            if pm == "brew":
                result = subprocess.run(["brew", "info", pkg], capture_output=True)
            elif pm == "apt":
                result = subprocess.run(["apt-cache", "show", pkg], capture_output=True)
            elif pm == "dnf":
                result = subprocess.run(["dnf", "info", pkg], capture_output=True)
            elif pm == "pacman":
                result = subprocess.run(["pacman", "-Si", pkg], capture_output=True)
            else:
                return False
            return result.returncode == 0
        except Exception:
            return False

    @staticmethod
    def detect_package_manager() -> Tuple[str, str, bool, bool]:
        """Detect package manager. Returns (pm, install_cmd, needs_sudo, is_arch)."""
        if sys.platform == "darwin":
            if shutil.which("brew"):
                return ("brew", "brew install", False, False)
            return ("", "", False, False)

        has_brew = shutil.which("brew") is not None

        if shutil.which("apt"):
            return ("apt", "sudo apt install -y", True, False)
        if shutil.which("dnf"):
            return ("dnf", "sudo dnf install -y", True, False)
        if shutil.which("pacman"):
            return ("pacman", "sudo pacman -S --noconfirm", True, True)
        if has_brew:
            return ("brew", "brew install", False, False)

        return ("", "", False, False)

    @staticmethod
    def find_missing() -> List[str]:
        """Find missing dependencies."""
        missing = []
        for cmd in DEP_MAP.keys():
            if not shutil.which(cmd):
                if cmd not in missing:
                    missing.append(cmd)
        return missing

    @staticmethod
    def install_dependencies(missing: List[str]) -> None:
        """Install missing dependencies."""
        pm, pm_install, needs_sudo, is_arch = DependencyManager.detect_package_manager()

        if not pm:
            print(f"{RED}Unsupported system.{RESET} Please install dependencies manually:")
            for dep in missing:
                print(f"  - {dep}")
            sys.exit(1)

        if needs_sudo:
            print()
            print(f"{YELLOW}Note: Installation requires administrator privileges.{RESET}")
            print("You may be prompted for your password.")
            print()

        installed = []
        failed = []
        manual = []
        already_installed = set()

        total = len(missing)
        for idx, cmd in enumerate(missing, 1):
            pkg = DependencyManager.get_pkg_name(cmd, pm)

            if pkg in already_installed:
                continue

            print(f"[{idx}/{total}] Installing {cmd} ({pkg})... ", end="", flush=True)

            if not DependencyManager.check_pkg_available(pkg, pm):
                # Try brew fallback for dovi_tool on Linux
                if cmd == "dovi_tool" and pm != "brew" and shutil.which("brew"):
                    brew_pkg = DependencyManager.get_pkg_name(cmd, "brew")
                    if DependencyManager.check_pkg_available(brew_pkg, "brew"):
                        print("(via Homebrew) ", end="", flush=True)
                        pm_install = "brew install"
                        pkg = brew_pkg
                    else:
                        print(f"{YELLOW}Not in repos.{RESET}")
                        manual.append(cmd)
                        continue
                else:
                    print(f"{YELLOW}Not in repos.{RESET}")
                    manual.append(cmd)
                    continue

            try:
                result = subprocess.run(
                    pm_install.split() + [pkg],
                    capture_output=True,
                    timeout=300
                )
                if result.returncode == 0:
                    print(f"{GREEN}Done.{RESET}")
                    installed.append(cmd)
                    already_installed.add(pkg)
                else:
                    print(f"{RED}Failed.{RESET}")
                    failed.append(cmd)
            except Exception:
                print(f"{RED}Failed.{RESET}")
                failed.append(cmd)

        # Summary
        print()
        print("---------------------------------------------------")
        print(f"{BOLD}INSTALLATION SUMMARY{RESET}")
        print("---------------------------------------------------")

        if installed:
            print(f"Installed:    {GREEN}{' '.join(installed)}{RESET}")
        if failed:
            print(f"Failed:       {RED}{' '.join(failed)}{RESET}")
        if manual:
            print(f"Manual Setup: {YELLOW}{' '.join(manual)}{RESET}")

        if manual or failed:
            print()
            needs_manual = manual + failed
            for dep in needs_manual:
                if dep == "dovi_tool":
                    print("dovi_tool must be installed manually:")
                    if is_arch:
                        print("  AUR:    https://aur.archlinux.org/packages/dovi_tool-bin")
                    print("  GitHub: https://github.com/quietvoid/dovi_tool")
                    print()
                    print("Tip: Install Homebrew (https://brew.sh) - a universal package manager.")
                    print("     Once installed, dovi_convert will use it to install dovi_tool automatically.")
                else:
                    print(f"{dep} must be installed manually using your package manager.")

            print()
            print("Please install, then run dovi_convert again.")
            print("---------------------------------------------------")
            sys.exit(1)

        print("---------------------------------------------------")
        print(f"{GREEN}All dependencies installed successfully!{RESET}")
        print()


# =============================================================================
# UPDATE CHECKER
# =============================================================================

class UpdateChecker:
    """Handles version checking and update notifications."""

    @staticmethod
    def check_background() -> None:
        """Fetch latest version in background thread."""
        def _fetch():
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                req = urllib.request.Request(
                    REPO_URL,
                    headers={"User-Agent": "dovi_convert"}
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read().decode())
                    tag = data.get("tag_name", "")
                    if tag:
                        UPDATE_FILE.write_text(tag)
            except Exception:
                pass

        thread = threading.Thread(target=_fetch, daemon=True)
        thread.start()

    @staticmethod
    def check_foreground() -> None:
        """Check for updates and display if available (from cache)."""
        if UPDATE_FILE.exists():
            try:
                latest = UPDATE_FILE.read_text().strip()
                if version_gt(latest, VERSION):
                    print(f"{CYAN}---------------------------------------------------{RESET}")
                    print(f"{BOLD}Update Available:{RESET} {GREEN}{latest}{RESET} (Current: v{VERSION})")
                    print("Get it at: https://github.com/cryptochrome/dovi_convert")
                    print(f"{CYAN}---------------------------------------------------{RESET}")
                    print()
            except Exception:
                pass

    @staticmethod
    def check_manual() -> None:
        """Manual update check with live fetch."""
        print(f"{BOLD}Checking for updates...{RESET}")
        try:
            req = urllib.request.Request(
                REPO_URL,
                headers={"User-Agent": "dovi_convert"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                latest_tag = data.get("tag_name", "")
        except Exception:
            print(f"{RED}Error: Could not fetch update info from GitHub.{RESET}")
            return

        if not latest_tag:
            print(f"{RED}Error: Could not fetch update info from GitHub.{RESET}")
            return

        print(f"Latest version on GitHub: {latest_tag}")
        print(f"Installed version:        v{VERSION}")

        if version_gt(latest_tag, VERSION):
            print(f"\n{GREEN}Update Available!{RESET}")
            print("Download at: https://github.com/cryptochrome/dovi_convert")
        else:
            print(f"\n{GREEN}You are up to date.{RESET}")


# =============================================================================
# MEDIA TOOL WRAPPER
# =============================================================================

class MediaToolWrapper:
    """Wraps external media tools (ffmpeg, mediainfo, mkvmerge, dovi_tool)."""

    def __init__(self, debug_mode: bool = False):
        self.debug_mode = debug_mode
        self.debug_log = Path("dovi_convert_debug.log")

    def log(self, msg: str) -> None:
        """Write to debug log if enabled."""
        if self.debug_mode:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self.debug_log, "a") as f:
                f.write(f"[{timestamp}] {msg}\n")

    def run_logged(self, cmd: List[str], capture: bool = False) -> Tuple[int, str, str]:
        """Run command with logging. Returns (returncode, stdout, stderr)."""
        if self.debug_mode:
            self.log(f"--- Command: {' '.join(cmd)} ---")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True
            )

            if self.debug_mode and result.stdout:
                self.log(result.stdout)
            if self.debug_mode and result.stderr:
                self.log(result.stderr)

            return result.returncode, result.stdout, result.stderr
        except Exception as e:
            return 1, "", str(e)

    def get_video_info(self, filepath: Path) -> VideoInfo:
        """Extract video track information from MKV file."""
        info = VideoInfo()

        if not filepath.exists():
            info.mi_info_string = "FILE_NOT_FOUND"
            return info

        # 1. Get Track ID & Properties from mkvmerge
        try:
            result = subprocess.run(
                ["mkvmerge", "-J", str(filepath)],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                # mkvmerge returns errors in stdout JSON, not stderr
                error_msg = ""
                try:
                    err_json = json.loads(result.stdout)
                    errors = err_json.get("errors", [])
                    if errors:
                        error_msg = errors[0].strip()
                except Exception:
                    error_msg = result.stderr.strip() if result.stderr else ""

                info.mi_info_string = "MKVMERGE_FAIL"
                info.mkvmerge_error = error_msg
                if self.debug_mode:
                    self.log(f"mkvmerge error: {filepath}")
                    self.log(f"Command: mkvmerge -J {filepath}")
                    self.log(f"Exit code: {result.returncode}")
                    self.log(f"Error: {error_msg or '(empty)'}")
                return info

            mkv_json = json.loads(result.stdout)

            # Find first video track
            for track in mkv_json.get("tracks", []):
                if track.get("type") == "video":
                    info.track_id = track.get("id")
                    props = track.get("properties", {})
                    info.delay = props.get("minimum_timestamp", 0)
                    info.language = props.get("language", "und")
                    info.name = props.get("track_name", "")
                    break

            if info.track_id is None:
                info.mi_info_string = "NO_TRACK"
                return info

        except Exception as e:
            info.mi_info_string = "MKVMERGE_FAIL"
            info.mkvmerge_error = str(e)
            if self.debug_mode:
                self.log(f"mkvmerge exception: {filepath}")
                self.log(f"Error: {e}")
            return info

        # 2. Get Dolby Vision profile from MediaInfo
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=JSON", str(filepath)],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                info.mi_info_string = "MEDIAINFO_FAIL"
                if self.debug_mode:
                    self.log(f"mediainfo error (code {result.returncode}): {result.stderr}")

                # Check for WSL Path Limit (Issue #14)
                if is_wsl() and check_wsl_path_limit(filepath):
                    info.mi_info_string = "MEDIAINFO_WSL_LIMIT"
                else:
                    info.mi_info_string = "MEDIAINFO_FAIL"
                return info

            # Check for empty output
            if not result.stdout.strip():
                info.mi_info_string = "MEDIAINFO_FAIL"
                if self.debug_mode:
                    self.log("MediaInfo Error: Empty stdout result")
                return info

            mi_json = json.loads(result.stdout)

            for track in mi_json.get("media", {}).get("track", []):
                if track.get("@type") == "Video":
                    hdr_format = track.get("HDR_Format", "") or ""
                    hdr_profile = track.get("HDR_Format_Profile", "") or ""
                    codec_id = track.get("CodecID", "") or ""
                    info.mi_info_string = f"{hdr_format} {hdr_profile} {codec_id}"
                    info.fps = track.get("FrameRate", "")
                    try:
                        info.frame_count = int(track.get("FrameCount", 0))
                    except (ValueError, TypeError):
                        info.frame_count = 0
                    break

        except Exception as e:
            info.mi_info_string = "MEDIAINFO_FAIL"
            if self.debug_mode:
                self.log(f"MediaInfo Exception: {e}")
            return info

        return info


    def get_bl_peak(self, filepath: Path) -> tuple[int, bool]:
        """Get base layer peak brightness (MaxCLL) in nits. Returns (value, is_default)."""
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%MaxCLL%", str(filepath)],
                capture_output=True,
                text=True
            )
            maxcll_str = result.stdout.strip()
            if maxcll_str and maxcll_str.isdigit():
                maxcll = int(maxcll_str)
                if maxcll >= 100:  # Sanity check
                    return (maxcll, False)
        except Exception as e:
            if self.debug_mode:
                self.log(f"get_bl_peak exception: {e}")

        return (1000, True)  # Default when MaxCLL unavailable


    def get_duration_ms(self, filepath: Path) -> int:
        """Get video duration in milliseconds."""
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%Duration%", str(filepath)],
                capture_output=True,
                text=True
            )
            dur_str = result.stdout.strip().split(".")[0]
            return int(dur_str) if dur_str else 0
        except Exception:
            return 0

    def get_frame_count(self, filepath: Path) -> int:
        """Get video frame count."""
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%FrameCount%", str(filepath)],
                capture_output=True,
                text=True
            )
            return int(result.stdout.strip())
        except Exception:
            return 0

    def get_fps(self, filepath: Path) -> str:
        """Get video frame rate."""
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%FrameRate%", str(filepath)],
                capture_output=True,
                text=True
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def get_frame_count_ffprobe(self, filepath: Path) -> int:
        """Get highly accurate frame count using ffprobe stream analysis (Slow)."""
        try:
            # -count_packets reads the actual stream
            cmd = [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-count_packets", "-show_entries", "stream=nb_read_packets",
                "-of", "default=noprint_wrappers=1:nokey=1", str(filepath)
            ]

            if self.debug_mode:
                self.log(f"Running strict frame count: {' '.join(cmd)}")

            result = subprocess.run(cmd, capture_output=True, text=True)
            return int(result.stdout.strip())
        except Exception as e:
            if self.debug_mode:
                self.log(f"ffprobe frame count failed: {e}")
            return 0


# =============================================================================
# MAIN APPLICATION CLASS
# =============================================================================

class DoviConvertApp:
    """Main application controller."""

    def __init__(self, config: Config):
        self.config = config

        # Check for Native Windows (Not Supported)
        if os.name == 'nt':
            print(f"{RED}Error: Native Windows is not officially supported.{RESET}")
            print(f"       Please use {BOLD}WSL2 (Windows Subsystem for Linux){RESET} or {BOLD}Docker{RESET}.")
            sys.exit(1)

        self.media = MediaToolWrapper(debug_mode=config.debug_mode)
        self.batch_running = False
        self.abort_requested = False

        # Current file state
        self.video_info: Optional[VideoInfo] = None
        self.scan_result: Optional[ScanResult] = None
        self.dovi_status = ""
        self.action = ""

        # Temp files to cleanup
        self.temp_files: List[Path] = []

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum: int, frame) -> None:
        """Handle interrupt signals."""
        self.abort_requested = True
        self._cleanup()
        sys.stdout.write("\033[?25h")  # Ensure cursor visible
        print(f"\n{YELLOW}[!] Process Interrupted by User.{RESET}")
        print(f"{YELLOW}[!] Cleaning up temporary files... Done.{RESET}")
        if self.batch_running:
            print(f"{GREEN}[✓] Original Source file is safe and untouched.{RESET}")
            return
        sys.exit(130)

    def _cleanup(self) -> None:
        """Clean up temporary files."""
        for tf in self.temp_files:
            try:
                if tf.exists():
                    tf.unlink()
            except Exception:
                pass
        self.temp_files.clear()

        # Clean up probe/inspect temp files
        cwd = Path.cwd()
        for pattern in ["probe_*.hevc", "probe_*.rpu", "probe_*.json",
                        "inspect_*.hevc", "inspect_*.rpu", "inspect_*.json"]:
            for f in cwd.glob(pattern):
                try:
                    f.unlink()
                except Exception:
                    pass

    def print_usage(self) -> None:
        """Print quick usage help."""
        print()
        print(f"{CYAN}{BOLD}dovi_convert v{VERSION}{RESET}")
        print()
        print(f"{BOLD}Usage:{RESET}")
        print(f"  {BOLD}dovi_convert -help{RESET}               Show detailed manual & examples.")
        print("  dovi_convert -scan [path]        Scan file(s) or directory.")
        print("  dovi_convert -inspect [file]     Full RPU structure inspection.")
        print("  dovi_convert -convert [file] ... Convert file(s) to Profile 8.1.")
        print("  dovi_convert -batch [dir]        Batch convert directory.")
        print("  dovi_convert -cleanup            Delete tool backups.")
        print("  dovi_convert -update-check       Check for software updates.")
        print()
        print(f"{BOLD}Common Options:{RESET}")
        print("  -r N          Recursive depth (for -scan, -batch, -cleanup)")
        print("  -y            Auto-confirm prompts")
        print("  -force        Force convert Complex FEL files")
        print("  -safe         Force disk extraction mode")
        print("  -delete       Auto-delete backups on success")
        print("  -temp [path]  Use temp directory for temporary files")
        print("  -o [path]     Output directory for converted files")
        print("  -hdr10        Strip DV, keep HDR10 (single file only)")
        print()

    def print_help(self) -> None:
        """Print detailed manual page."""
        help_text = f"""{BOLD}dovi_convert - Dolby Vision Profile 7 -> 8.1 Converter{RESET}

{BOLD}DESCRIPTION{RESET}
  This tool automates the conversion of Dolby Vision Profile 7 MKV files (UHD Blu-ray)
  into Profile 8.1. This ensures compatibility with devices that do not support the
  Enhancement Layer (Apple TV 4K, Shield, etc.), preventing fallback to HDR10.

{BOLD}THE CONVERSION{RESET}
  The conversion process strips the Enhancement Layer (EL) from the video while
  injecting the RPU (dynamic metadata) into the base layer. This creates a
  Profile 8.1 compatible file. All audio and subtitle tracks are preserved.

  {BOLD}Single File{RESET}: Convert individual files with full control.
  {BOLD}Batch Mode{RESET}: Recursively scans directories and batch-converts files.

{BOLD}MODES OF OPERATION{RESET}
  {BOLD}1. Standard Mode (Default){RESET}
     Pipes the video stream directly into the conversion tool.
     Fast, efficient, and requires zero temporary disk space.

  {BOLD}2. Safe Mode (-safe){RESET}
     Extracts the video track to a temporary file on disk, then converts.
     Slower, but robust against files with irregular timestamps or
     'Seamless Branching' structures (common on Disney/Marvel discs).
     The tool will automatically offer this mode if Standard conversion fails.

{BOLD}FILE SCANNING & ANALYSIS{RESET}
  {BOLD}Default Scan{RESET}
    When scanning MKV files, the tool first identifies the video format (HDR10,
    Dolby Vision Profile, etc.). For Profile 7 files with FEL, it analyzes the
    RPU metadata to distinguish between:
    1. {GREEN}Simple FEL / MEL{RESET}: No active brightness expansion detected. Likely safe to convert.
    2. {RED}Complex FEL{RESET}: Expands luminance beyond base layer. Conversion skipped.

  {BOLD}Inspection (Manual){RESET}
    For a definitive analysis, a dedicated inspection mode is available.
    It reads the entire file frame-by-frame to verify whether brightness
    expansion is present in the FEL. Use this to verify Simple FEL verdicts,
    or if you want absolute certainty.

{BOLD}AUTOMATIC BACKUPS{RESET}
  The tool automatically preserves your original file before any modification.
  It uses a specific naming convention to distinguish its backups from your own files.
  Backup File: {CYAN}[filename].mkv.bak.dovi_convert{RESET}

{BOLD}KNOWN LIMITATION: Single Video Track{RESET}
  The {BOLD}converted{RESET} file will contain exactly one video track (the main movie).
  Any secondary video streams (e.g., Picture-in-Picture commentary or Multi-Angle views)
  will be dropped because the conversion process isolates the main video track.

  {BOLD}No Risk of Data Loss:{RESET} Your original source file (containing all tracks)
  is automatically preserved as a backup. You can restore it if needed.

{BOLD}COMMANDS{RESET}

  {BOLD}-scan [path] [path2] ...{RESET}  (alias: -check)
       Scan files or directories to identify format and conversion candidates.
       Detects HDR formats and analyzes Profile 7 files for FEL complexity.

       If no path is given, scans all MKV files in the current directory.

       Options:
          {BOLD}-r [depth]{RESET}      Scan subdirectories recursively. Default depth: 5.
          {BOLD}-candidates{RESET}    Show only conversion candidates (Profile 7).

  {BOLD}-convert [file] [file2] ...{RESET}
       Converts file(s) to Profile 8.1. Multiple files supported.
       Skips 'Complex FEL' files to prevent data loss.
       The original file is NOT deleted; it is renamed to *.mkv.bak.dovi_convert.

       Options:
          {BOLD}-hdr10{RESET}         Convert to HDR10 instead of DoVi Profile 8.1.
          {BOLD}-force{RESET}         Override 'Complex FEL' detection.
          {BOLD}-temp [path]{RESET}   Write temp files to a faster drive.

  {BOLD}-inspect [file]{RESET}
       Full frame-by-frame inspection of brightness metadata.
       Verifies whether the FEL contains active brightness expansion.
       Use this to verify Simple FEL verdicts, or if you want absolute certainty.

       Note: Reads entire file. Slower than the default scan.

       Options:
          {BOLD}-safe{RESET}   Force Safe Mode (Disk Extraction fallback).

  {BOLD}-batch [dir] [dir2] ...{RESET}
       Batch convert safe Profile 7 files from directories.
       If no directory given, uses current directory.

       Options:
          {BOLD}-r [depth]{RESET}       Scan subdirectories recursively. Default depth: 5.
          {BOLD}-y{RESET}               Skip confirmation prompts (Auto-Yes).
          {BOLD}-include-simple{RESET}  Allow auto-conversion of Simple FEL files in Auto-Yes mode.
          {BOLD}-force{RESET}           Force convert 'Complex FEL' files (Apply to all).
          {BOLD}-delete{RESET}          Auto-delete backups after successful conversion.
          {BOLD}-temp [path]{RESET}     Write temp files to a faster drive.

  {BOLD}-cleanup{RESET}
       Scans for and deletes {CYAN}*.mkv.bak.dovi_convert{RESET} files in the current directory.
       {BOLD}Safety Check:{RESET} Checks if 'Parent' MKV exists before deleting orphan backups.

       Options:
          {BOLD}-r{RESET}   Recursive scan.
          {BOLD}-y{RESET}   Skip confirmation prompts.

  {BOLD}-update-check{RESET}
       Checks if a newer version of dovi_convert is available.

{BOLD}OPTION DETAILS{RESET}

  {BOLD}-force{RESET} [Convert, Batch]
       {RED}Force Conversion.{RESET}
       Overrides the 'Complex FEL' detection. Use this if you want to convert
       a Complex FEL file despite the potential loss of brightness data.

  {BOLD}-include-simple{RESET} [Batch]
       {YELLOW}Auto-Include Simple FEL.{RESET}
       When using -y (Auto-Yes), Simple FEL files are normally skipped to allow
       manual review. This flag includes them in batch conversions.

  {BOLD}-safe{RESET}  [Convert, Batch]
       {YELLOW}Force Safe Mode (Extraction).{RESET}
       Forces extraction of the video track to disk before converting.
       This is the robust fallback method usually triggered automatically on error,
       but you can force it manually here for known problematic files.

  {BOLD}-delete{RESET} [Convert, Batch]
       {YELLOW}Auto-Delete Mode.{RESET}
       Automatically deletes the backup (Original Source) file immediately
       after a successful conversion and verification.
       Use this for large batches where you don't have disk space to store backups.

  {BOLD}-debug{RESET} [Global]
       {YELLOW}Debug Mode.{RESET}
       Generates a 'dovi_convert_debug.log' file in the current directory
       containing full ffmpeg/dovi_tool output. Essential for troubleshooting.

  {BOLD}-y{RESET}     [Batch, Cleanup]
       {YELLOW}Auto-Yes Mode.{RESET}
       Automatically answers 'Yes' to confirmation prompts (Batch Start / Cleanup).
       Does NOT override safety decisions (like Safe Mode fallback).

  {BOLD}-temp [path]{RESET} [Convert, Batch]
       {YELLOW}Temp Directory.{RESET}
       Write temporary files to a separate directory.
       Use this when source files are on slow storage (HDD, NAS).
       The temp directory should be on a fast drive (SSD/NVMe).

  {BOLD}-o [path]{RESET} [Convert, Batch]
       {YELLOW}Output Directory.{RESET}
       Place converted files in specified directory.

       Convert: Files placed directly in output directory.
       Batch:   Basename of source directory preserved, subdirectories mirrored.

  {BOLD}-hdr10{RESET} [Convert only]
       {YELLOW}HDR10 Mode.{RESET}
       Converts to HDR10 (with HDR10+ metadata, if available in the source)
       instead of DoVi Profile 8.1. Read docs for use cases.

       Not available in batch mode.
"""
        # Use pager if available and stdout is a tty
        if shutil.which("less") and sys.stdout.isatty():
            try:
                proc = subprocess.Popen(
                    ["less", "-R"],
                    stdin=subprocess.PIPE,
                    text=True
                )
                proc.communicate(input=help_text)
                return
            except Exception:
                pass
        print(help_text)

    def check_fel_complexity(self, filepath: Path) -> ScanResult:
        """Analyze RPU to detect Complex FEL."""
        result = ScanResult()

        # 1. Determine probe points
        duration_ms = self.media.get_duration_ms(filepath)

        if duration_ms < 10000:
            timestamps = [0]
        else:
            dur_sec = duration_ms // 1000
            # Probe at 10 points (5% to 95%)
            timestamps = [
                int(dur_sec * 0.05), int(dur_sec * 0.15), int(dur_sec * 0.25),
                int(dur_sec * 0.35), int(dur_sec * 0.45), int(dur_sec * 0.55),
                int(dur_sec * 0.65), int(dur_sec * 0.75), int(dur_sec * 0.85),
                int(dur_sec * 0.95)
            ]

        # 2. Get base layer peak
        bl_peak, _ = self.media.get_bl_peak(filepath)
        threshold = bl_peak + 50

        if self.config.debug_mode:
            self.media.log(f"[Scan Debug] Base Layer Peak: {bl_peak} nits (Threshold: {threshold})")

        complex_signal = False
        probe_count = 0


        for t in timestamps:
            if self.abort_requested:
                break

            # Create temp files
            temp_hevc = filepath.parent / f"probe_{t}_{int(time.time())}_{os.getpid()}.hevc"
            temp_rpu = temp_hevc.with_suffix(".rpu")
            temp_json = temp_hevc.with_suffix(".json")

            self.temp_files.extend([temp_hevc, temp_rpu, temp_json])

            # Extract 1 second of HEVC
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-v", "error" if not self.config.debug_mode else "info",
                "-analyzeduration", "100M", "-probesize", "100M",
                "-ss", str(t), "-i", str(filepath),
                "-map", "0:v:0", "-c:v", "copy", "-an", "-sn", "-dn",
                "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-t", "1",
                str(temp_hevc)
            ]

            try:
                result_proc = subprocess.run(
                    ffmpeg_cmd,
                    capture_output=True,
                    stdin=subprocess.DEVNULL
                )

            except Exception as e:
                if self.config.debug_mode:
                    self.media.log(f"probe ffmpeg exception @ {t}s: {e}")
                continue


            if not temp_hevc.exists() or temp_hevc.stat().st_size == 0:
                self._cleanup_probe_files([temp_hevc, temp_rpu, temp_json])
                continue

            # Extract RPU
            try:
                subprocess.run(
                    ["dovi_tool", "extract-rpu", str(temp_hevc), "-o", str(temp_rpu)],
                    capture_output=True
                )
            except Exception as e:
                if self.config.debug_mode:
                    self.media.log(f"probe dovi_tool extract-rpu exception: {e}")

            temp_hevc.unlink(missing_ok=True)

            if not temp_rpu.exists() or temp_rpu.stat().st_size == 0:
                self._cleanup_probe_files([temp_rpu, temp_json])
                continue

            # Export to JSON
            try:
                subprocess.run(
                    ["dovi_tool", "export", "-i", str(temp_rpu), "-d", f"all={temp_json}"],
                    capture_output=True
                )
            except Exception as e:
                if self.config.debug_mode:
                    self.media.log(f"probe dovi_tool export exception: {e}")

            temp_rpu.unlink(missing_ok=True)

            if not temp_json.exists() or temp_json.stat().st_size == 0:
                temp_json.unlink(missing_ok=True)
                continue

            probe_count += 1

            # Check for MEL
            try:
                json_content = temp_json.read_text()
                if '"el_type":"MEL"' in json_content:
                    result.verdict = "SAFE"
                    result.reason = "Minimal Enhancement Layer (MEL) Detected"
                    temp_json.unlink(missing_ok=True)
                    return result

                # Extract L1 max using simple parsing (no jq needed in Python)
                l1_max = self._extract_l1_max(json_content)

                if self.config.debug_mode:
                    self.media.log(f"[Scan Debug] Probe @ {t}s : L1 Raw={l1_max}")

                if l1_max is not None:
                    l1_nits = pq_to_nits(l1_max)

                    if self.config.debug_mode:
                        self.media.log(f"[Scan Debug] Probe @ {t}s : L1={l1_max} -> {l1_nits} nits vs Threshold={threshold}")

                    if l1_nits > threshold:
                        complex_signal = True
                        result.reason = f"Active Reconstruction (L1: {l1_nits} nits > BL: {bl_peak} nits @ {t}s)"
                        temp_json.unlink(missing_ok=True)
                        break

            except Exception as e:
                if self.config.debug_mode:
                    self.media.log(f"[Scan Debug] Probe @ {t}s : Error parsing JSON: {e}")

            temp_json.unlink(missing_ok=True)

        if probe_count == 0:
            result.reason = "Extraction failed (No probes succeeded)"
            result.verdict = "COMPLEX"  # Default to Complex if we can't read it
            return result

        # Require at least 50% of probes to succeed for reliable verdict
        # (Only applies when we haven't already detected Complex - early break is fine)
        min_required = max(1, len(timestamps) // 2)
        if not complex_signal and probe_count < min_required:
            if self.config.debug_mode:
                self.media.log(f"Probe threshold not met: {probe_count}/{len(timestamps)} succeeded, {min_required} required")
            result.reason = f"Insufficient data ({probe_count}/{len(timestamps)} probes succeeded)"
            result.verdict = "COMPLEX"  # Default to Complex if data is unreliable
            return result

        if complex_signal:
            result.verdict = "COMPLEX"
        else:
            result.verdict = "SAFE"
            result.reason = "Static / Simple FEL (Safe to Convert)"

        return result

    def _extract_l1_max(self, json_content: str) -> Optional[int]:
        """Extract max L1 value from RPU JSON."""
        try:
            data = json.loads(json_content)
            max_vals = []

            def find_l1(obj):
                if isinstance(obj, dict):
                    # Look for Level1 or l1 keys
                    for key in ["Level1", "l1", "L1"]:
                        if key in obj:
                            l1_data = obj[key]
                            if isinstance(l1_data, dict):
                                for mkey in ["max_pq", "max", "Max"]:
                                    if mkey in l1_data:
                                        val = l1_data[mkey]
                                        if isinstance(val, (int, float)):
                                            max_vals.append(int(val))
                    for v in obj.values():
                        find_l1(v)
                elif isinstance(obj, list):
                    for item in obj:
                        find_l1(item)

            find_l1(data)
            return max(max_vals) if max_vals else None
        except Exception:
            return None

    def _extract_l1_stream(self, json_path: Path) -> List[int]:
        """
        Stream parser for RPU JSON.
        Extracts 'max_pq' values line-by-line using low memory.
        """
        max_vals = []
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if "max_pq" in line:
                        # Expected format: "max_pq": 1234, or similar
                        # We use simple splitting to be robust against whitespace
                        try:
                            parts = line.split(":")
                            if len(parts) >= 2:
                                # Get the part after the colon, strip comma and whitespace
                                val_str = parts[1].strip().rstrip(",")
                                max_vals.append(int(val_str))
                        except (ValueError, IndexError):
                            continue
        except Exception:
            return []

        return max_vals


    def _cleanup_probe_files(self, files: List[Path]) -> None:
        """Clean up specific probe files."""
        for f in files:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

    def determine_action(self, filepath: Path) -> Tuple[str, str]:
        """Determine action based on video info and FEL analysis."""
        info = self.video_info
        if info is None:
            return (f"{RED}Error{RESET}", "ERROR")

        mi = info.mi_info_string

        if mi == "FILE_NOT_FOUND":
            return (f"{RED}File not found{RESET}", "ERROR")

        if mi == "NO_TRACK":
            return (f"{RED}No Video Track{RESET}", "SKIP")

        if mi == "MKVMERGE_FAIL":
            if info.mkvmerge_error:
                err_detail = info.mkvmerge_error.split('\n')[0][:60]
                return (f"{RED}mkvmerge: {err_detail}{RESET}", "ERROR")
            else:
                return (f"{RED}mkvmerge: failed (no details, use -debug){RESET}", "ERROR")

        if mi == "MEDIAINFO_FAIL":
            return (f"{RED}Error: MediaInfo failed (Check file inputs or installation){RESET}", "ERROR")

        if mi == "MEDIAINFO_WSL_LIMIT":
            return (f"{RED}Error: WSL limit reached. File path > 255 chars on Windows drive.{RESET}", "ERROR")

        # Decision matrix
        if "dvhe.07" in mi or "Profile 7" in mi:
            # Profile 7 detected - run FEL analysis
            self.scan_result = self.check_fel_complexity(filepath)

            if self.scan_result.verdict == "COMPLEX":
                if "Insufficient data" in self.scan_result.reason or "Extraction failed" in self.scan_result.reason:
                    status = f"{RED}DV Profile 7 (Scan Failed){RESET}"
                    action = f"{RED}SKIP (error){RESET}"
                elif self.config.force_mode:
                    status = f"{RED}DV Profile 7 FEL (Complex){RESET}"
                    action = f"{RED}CONVERT (FORCED){RESET}"
                else:
                    status = f"{RED}DV Profile 7 FEL (Complex){RESET}"
                    action = f"{RED}SKIP (Complex FEL){RESET}"
            elif self.scan_result.verdict == "SAFE":
                if "MEL" in self.scan_result.reason:
                    status = f"{GREEN}DV Profile 7 MEL (Safe){RESET}"
                    action = f"{GREEN}CONVERT{RESET}"
                else:
                    status = f"{CYAN}DV Profile 7 FEL (Simple){RESET}"
                    action = f"{CYAN}CONVERT*{RESET}"
            else:
                status = f"{YELLOW}DV Profile 7 (Check Failed){RESET}"
                action = f"{YELLOW}MANUAL CHECK{RESET}"

            return (status, action)

        elif "dvhe.08" in mi or "Profile 8" in mi:
            return (f"{DEFAULT}DV Profile 8.1{RESET}", "IGNORE")
        elif "dvhe.05" in mi or "Profile 5" in mi:
            return (f"{YELLOW}DV Profile 5 (Stream){RESET}", "IGNORE")
        elif "Dolby Vision" in mi:
            return (f"{YELLOW}DV Unknown Profile{RESET}", "IGNORE")
        else:
            # Granular HDR detection
            if "2094" in mi:
                return (f"{DEFAULT}HDR10+{RESET}", "IGNORE")
            elif "HLG" in mi or "Hybrid Log Gamma" in mi:
                return (f"{DEFAULT}HLG{RESET}", "IGNORE")
            elif "2086" in mi or "HDR10" in mi:
                return (f"{DEFAULT}HDR10{RESET}", "IGNORE")
            else:
                return (f"{DEFAULT}SDR{RESET}", "IGNORE")

    def analyze_file(self, filepath: Path) -> None:
        """Full analysis: get video info and determine action."""
        self.video_info = self.media.get_video_info(filepath)
        self.dovi_status, self.action = self.determine_action(filepath)


    def print_metrics(self, final_file: Path, frame_count: int, start_time: float, orig_size: int) -> None:
        """Print conversion metrics."""
        duration = int(time.time() - start_time)
        minutes = duration // 60
        seconds = duration % 60

        final_size = get_file_size(final_file)
        size_diff = orig_size - final_size
        if size_diff < 0:
            size_diff = 0

        orig_gb = human_size_gb(orig_size)
        final_gb = human_size_gb(final_size)

        # Dynamic unit for diff
        if size_diff >= 1073741824:
            diff_disp = f"{size_diff / 1024 / 1024 / 1024:.2f} GB"
        else:
            diff_disp = f"{size_diff / 1024 / 1024:.2f} MB"

        fps = frame_count // duration if duration > 0 else 0

        print("---------------------------------------------------")
        print(f"             {BOLD}CONVERSION METRICS{RESET}")
        print("---------------------------------------------------")
        print(f"Time Taken:    {minutes}m {seconds:02d}s")
        print(f"Orig Size:     {orig_gb}")
        print(f"Final Size:    {final_gb}")
        print(f"EL Discarded:  {diff_disp} (Space Saved)")
        print(f"Avg Speed:     {fps} fps")
        print("---------------------------------------------------")

    def convert_turbo(self, input_file: Path, output_file: Path) -> Tuple[int, str]:
        """Standard conversion using pipe mode. Returns (status, error_type)."""
        spinner = Spinner("[1/3] Converting... ")
        spinner.start()

        ffmpeg_stderr = b""
        dovi_stderr = b""

        try:
            # Create the pipe command
            ffmpeg_proc = subprocess.Popen(
                ["ffmpeg", "-y", "-v", "error", "-i", str(input_file),
                 "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # Choose dovi_tool command based on mode
            if self.config.hdr10_mode:
                dovi_cmd = ["dovi_tool", "remove", "-", "-o", str(output_file)]
            else:
                dovi_cmd = ["dovi_tool", "-m", "2", "convert", "--discard", "-", "-o", str(output_file)]

            dovi_proc = subprocess.Popen(
                dovi_cmd,
                stdin=ffmpeg_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # Allow ffmpeg_proc to receive SIGPIPE
            ffmpeg_proc.stdout.close()

            _, dovi_stderr = dovi_proc.communicate()
            _, ffmpeg_stderr = ffmpeg_proc.communicate()

            ffmpeg_status = ffmpeg_proc.returncode
            dovi_status = dovi_proc.returncode

        except Exception as e:
            spinner.stop()
            return (1, "UNKNOWN")

        spinner.stop()

        # Check BOTH return codes - critical for catching hidden pipe errors
        if ffmpeg_status != 0:
            # ffmpeg failed - the data sent to dovi_tool may be incomplete/corrupt
            output_file.unlink(missing_ok=True)
            if self.config.debug_mode:
                self.media.log(f"ffmpeg failed with code {ffmpeg_status}: {ffmpeg_stderr.decode()}")

            stderr_text = ffmpeg_stderr.decode() if ffmpeg_stderr else ""
            if any(err in stderr_text for err in ["No space left on device", "Permission denied", "Read-only file system"]):
                print(stderr_text)
                return (1, "CRITICAL")

            # Treat any ffmpeg failure as a stream error
            return (1, "STREAM_ERROR")

        if dovi_status == 0:
            print(f"\r\033[K[1/3] Converting... Done.")
            return (0, "")

        # Check for user abort
        if dovi_status == 130 or self.abort_requested:
            output_file.unlink(missing_ok=True)
            return (130, "")

        output_file.unlink(missing_ok=True)

        # Error classification from dovi_tool
        stderr_text = dovi_stderr.decode() if dovi_stderr else ""

        if self.config.debug_mode:
            self.media.log(f"dovi_tool error (code {dovi_status}): {stderr_text or '(empty)'}")

        if any(err in stderr_text for err in ["No space left on device", "Permission denied", "Read-only file system"]):
            print(stderr_text)
            return (1, "CRITICAL")

        if any(err in stderr_text for err in ["Invalid data", "Invalid NAL unit", "conversion failed", "Error splitting"]):
            return (1, "STREAM_ERROR")

        print(stderr_text)
        return (1, "UNKNOWN")

    def convert_legacy(self, input_file: Path, output_file: Path) -> int:
        """Safe mode conversion using disk extraction."""
        raw_temp = input_file.with_suffix(".raw.hevc")
        self.temp_files.append(raw_temp)

        if self.video_info is None or self.video_info.track_id is None:
            return 1

        # Extraction step
        spinner = Spinner("[1/3] Extracting... ")
        spinner.start()

        ret, _, _ = self.media.run_logged(
            ["mkvextract", str(input_file), "tracks", f"{self.video_info.track_id}:{raw_temp}"]
        )
        spinner.stop()

        if ret == 130 or self.abort_requested:
            return 130
        if ret != 0:
            return 1

        print(f"\r\033[K[1/3] Extracting... Done.")

        # Conversion step
        spinner = Spinner("[1/3] Converting... ")
        spinner.start()

        # Choose dovi_tool command based on mode
        if self.config.hdr10_mode:
            dovi_cmd = ["dovi_tool", "remove", str(raw_temp), "-o", str(output_file)]
        else:
            dovi_cmd = ["dovi_tool", "-m", "2", "convert", "--discard", str(raw_temp), "-o", str(output_file)]

        ret, _, _ = self.media.run_logged(dovi_cmd)
        spinner.stop()

        raw_temp.unlink(missing_ok=True)

        if ret == 0:
            print(f"\r\033[K[1/3] Converting... Done.")

        if ret == 130:
            return 130
        return ret

    def _convert_validate(self, filepath: Path, mode: str) -> Optional[int]:
        """Validate if file is ready for conversion. Returns return_code if should stop, None if ok."""
        if not filepath.exists():
            print(f"File not found: {filepath}")
            return 1

        self.analyze_file(filepath)

        # Safety check: Complex FEL
        if self.scan_result and self.scan_result.verdict == "COMPLEX":
            if self.config.force_mode:
                print(f"{RED}Complex FEL detected. Force Mode enabled. Proceeding...{RESET}")
            else:
                if mode == "auto":
                    return 2
                print(f"{RED}Error: Complex FEL detected (not safe to convert. Use -force to override).{RESET}")
                return 1

        # Simple FEL Advisory
        if "FEL (Simple)" in self.dovi_status:
            if self.batch_running and self.config.include_simple:
                print(f"{CYAN}[i] Simple-FEL: Explicitly included.{RESET}")
            else:
                print(f"{YELLOW}[!] WARNING: This is a 'Simple FEL' file.{RESET}")
                print("    Scan found no active brightness expansion.")
                print("    Use -inspect for a full RPU analysis if in doubt.")
                try:
                    reply = input("Proceed with conversion? (y/N) ").strip().lower()
                except EOFError:
                    reply = "n"
                if reply != "y":
                    print("Conversion cancelled.")
                    return 1

        # Check if not Profile 7
        if self.action in ("IGNORE", "SKIP") or "CONVERT" not in self.action:
            if mode == "auto":
                return 2
            print(f"{RED}Error: Input file is not a Dolby Vision Profile 7 file.{RESET}")
            return 1

        return None

    def _convert_setup_paths(self, filepath: Path, batch_source_dir: Optional[Path] = None) -> Optional[Tuple[Path, Path, Path, Path]]:
        """Setup paths. Returns (conv_hevc, temp_mkv, backup_mkv, final_output_path) or None on error."""
        base_name = filepath.stem

        # Choose suffix based on conversion mode
        suffix = "hdr10" if self.config.hdr10_mode else "p81"

        # Use temp_dir for intermediate files if specified, otherwise use source directory
        # Route conv_hevc to temp_dir if specified (Design B: only HEVC goes to temp)
        if self.config.temp_dir:
            conv_hevc = self.config.temp_dir / f"{base_name}.{suffix}.hevc"
        else:
            conv_hevc = filepath.with_name(f"{base_name}.{suffix}.hevc")

        # temp_mkv uses .tmp extension to prevent automation retrigger
        # Always stays in source directory for efficient I/O
        temp_mkv = filepath.with_name(f"{base_name}.{suffix}.tmp")
        backup_mkv = filepath.with_suffix(".mkv.bak.dovi_convert")

        # Calculate final output path
        if self.config.output_dir:
            if batch_source_dir:
                # Batch mode: preserve basename and subdirectory structure
                batch_basename = batch_source_dir.name
                rel_path = filepath.relative_to(batch_source_dir)
                final_output_path = self.config.output_dir / batch_basename / rel_path
            else:
                # Convert mode: place directly in output directory
                final_output_path = self.config.output_dir / f"{base_name}.mkv"

            # Ensure output subdirectories exist
            final_output_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            # No -o flag: output in source directory (standard behavior)
            final_output_path = filepath

        self.temp_files.extend([conv_hevc, temp_mkv])

        if backup_mkv.exists():
            print(f"{RED}Skipping: Backup file already exists.{RESET}")
            return None

        return conv_hevc, temp_mkv, backup_mkv, final_output_path

    def _convert_perform_conversion(self, filepath: Path, conv_hevc: Path) -> int:
        """Run the actual conversion. Returns 0=success, 1=fail, 130=abort."""
        if self.config.safe_mode:
            if self.convert_legacy(filepath, conv_hevc) != 0:
                if self.abort_requested:
                    return 130
                print(f"{RED}Safe Mode Failed.{RESET}")
                return 1
            return 0
        else:
            turbo_res, fail_reason = self.convert_turbo(filepath, conv_hevc)

            if turbo_res == 0:
                return 0
            elif turbo_res == 130 or self.abort_requested:
                return 130
            else:
                print(f"{RED}Standard Mode Failed.{RESET}")

                if fail_reason == "CRITICAL":
                    print(f"{RED}CRITICAL ERROR: Disk Full or Permission Denied.{RESET}")
                    return 1
                elif fail_reason == "STREAM_ERROR":
                    print(f"{YELLOW}Reason: Stream/Timestamp Error (Likely Seamless Branching).{RESET}")

                if self.batch_running:
                    print(f"{YELLOW}Batch Mode: Skipping file. Retry manually with -safe.{RESET}")
                    return 1
                else:
                    print(f"{YELLOW}Suggestion: This file may require Safe Mode (Disk Extraction).{RESET}")
                    try:
                        reply = input("Retry with Safe Mode? (Y/n) ").strip().lower()
                    except EOFError:
                        reply = "n"
                    if reply == "n":
                        return 1

                    print("[Retry] ", end="")
                    if self.convert_legacy(filepath, conv_hevc) != 0:
                        if self.abort_requested:
                            return 130
                        print(f"{RED}Safe Mode also failed.{RESET}")
                        return 1
                    return 0

    def _convert_mux(self, filepath: Path, conv_hevc: Path, temp_mkv: Path, fps_orig: str) -> int:
        """Mux the new file. Returns 0=success, 1=fail, 130=abort."""
        mux_args = ["-o", str(temp_mkv)]

        if self.video_info and self.video_info.delay != 0:
            mux_args.extend(["--sync", f"0:{self.video_info.delay}"])

        mux_args.extend(["--default-duration", f"0:{fps_orig}fps"])
        mux_args.extend(["--language", f"0:{self.video_info.language if self.video_info else 'und'}"])

        if self.video_info and self.video_info.name:
            mux_args.extend(["--track-name", f"0:{self.video_info.name}"])

        mux_args.append(str(conv_hevc))
        mux_args.extend(["--no-video", str(filepath)])

        spinner = Spinner(f"[2/3] Muxing (Cloning Metadata + {fps_orig}fps)... ")
        spinner.start()

        ret, _, _ = self.media.run_logged(["mkvmerge"] + mux_args)
        spinner.stop()

        if ret == 130 or self.abort_requested:
            return 130
        if ret != 0:
            print(f"{RED}Mux Failed.{RESET}")
            return 1

        print(f"\r\033[K[2/3] Muxing (Cloning Metadata + {fps_orig}fps)... Done.")
        return 0

    def _convert_finalize(self, filepath: Path, temp_mkv: Path, backup_mkv: Path, conv_hevc: Path, start_time: float, orig_size: int, final_output_path: Path) -> int:
        """Verify, swap and print metrics. Returns 0=success, 1=fail."""
        # Step 4: Verification
        spinner = Spinner("[3/3] Verifying... ")
        spinner.start()

        frames_orig = self.media.get_frame_count(filepath)
        frames_new = self.media.get_frame_count(temp_mkv)
        spinner.stop()

        if frames_orig and frames_orig != frames_new:
            # DVY-33: Smart Verification Fallback
            # MediaInfo metadata in the source might be wrong (e.g. Avatar.mkv).
            # We run a slow but accurate ffprobe stream count on the ORIGINAL file to double-check.
            print(f"\r\033[K[3/3] Verifying... {YELLOW}Metadata Mismatch ({frames_orig} vs {frames_new}). Checking Stream...{RESET}")

            ff_orig = self.media.get_frame_count_ffprobe(filepath)

            if ff_orig == frames_new:
                 print(f"\r\033[K[3/3] Verifying... {GREEN}Success!{RESET} (Source Metadata was incorrect)")
                 if self.config.debug_mode:
                     self.media.log(f"Frame verification mismatch resolved: MI_Orig={frames_orig}, FF_Orig={ff_orig}, New={frames_new}")
            else:
                print(f"\r\033[K[3/3] Verifying... {RED}FAIL: Frame mismatch!{RESET} (Stream Verified: {ff_orig} vs {frames_new})")
                return 1
        else:
            print(f"\r\033[K[3/3] Verifying... {GREEN}Success!{RESET}")

        # Print metrics
        self.print_metrics(temp_mkv, frames_new, start_time, orig_size)

        # Step 5: Create backup and move output
        filepath.rename(backup_mkv)

        if final_output_path == filepath:
            # No -o flag: rename .tmp to .mkv in same directory
            temp_mkv.rename(filepath)
        else:
            # With -o: move to destination and rename .tmp to .mkv
            spinner = Spinner("Moving to output directory... ")
            spinner.start()
            shutil.move(str(temp_mkv), str(final_output_path))
            spinner.stop()
            print(f"\r\033[KMoving to output directory... Done.")
            print(f"Output saved to: {CYAN}{final_output_path}{RESET}")

        if self.config.delete_backup:
            backup_mkv.unlink()
            print(f"{YELLOW}Original Source deleted (-delete active).{RESET}")
        else:
            print(f"Original Source saved as: {CYAN}{backup_mkv}{RESET}")

        conv_hevc.unlink(missing_ok=True)
        return 0


    def cmd_convert(self, filepath: Path, mode: str = "manual", batch_source_dir: Optional[Path] = None) -> int:
        """Convert a single file. Returns 0=success, 1=error, 2=skip, 130=abort."""

        # 1. Validation
        res = self._convert_validate(filepath, mode)
        if res is not None:
            return res

        # Only print Processing line in manual mode (batch mode already announces the file)
        if mode == "manual":
            print(f"{BOLD}Processing:{RESET} {filepath}")

        # 2. Setup (Get FPS and paths)
        fps_orig = self.media.get_fps(filepath)
        if not fps_orig:
            print(f"{RED}Error: Could not detect Frame Rate.{RESET}")
            return 1

        paths = self._convert_setup_paths(filepath, batch_source_dir)
        if not paths:
            return 1
        conv_hevc, temp_mkv, backup_mkv, final_output_path = paths

        # Initialize metrics
        start_time = time.time()
        orig_size = get_file_size(filepath)

        # 3. Conversion
        try:
            res = self._convert_perform_conversion(filepath, conv_hevc)
            if res != 0:
                return res

            # 4. Muxing
            res = self._convert_mux(filepath, conv_hevc, temp_mkv, fps_orig)
            if res != 0:
                return res

            # 5. Finalize
            return self._convert_finalize(filepath, temp_mkv, backup_mkv, conv_hevc, start_time, orig_size, final_output_path)

        finally:
            # Critical Cleanup: Ensures temp files are deleted even if we return early (e.g. Frame Mismatch)
            self._cleanup()

    def cmd_check_single(self, filepath: Path) -> None:
        """Scan and report on a single file."""
        self.analyze_file(filepath)

        if self.video_info and self.video_info.mi_info_string == "FILE_NOT_FOUND":
            print(f"{RED}Error: File '{filepath}' not found.{RESET}")
            return

        delay_ms = (self.video_info.delay // 1000000) if self.video_info else 0
        name = filepath.name

        print("---------------------------------------------------")
        print(f"{BOLD}File:{RESET}   {name}")
        print(f"{BOLD}Status:{RESET} {self.dovi_status}")
        print(f"{BOLD}Action:{RESET} {self.action}")
        print("---------------------------------------------------")

        # Simple FEL Advisory
        if "FEL (Simple)" in self.dovi_status:
            print()
            self._print_simple_fel_advisory()

    def cmd_check_all(self, max_depth: int = 1, files: List[Path] = None) -> None:
        """Scan all MKV files in directory or provided list."""
        if files:
            location = f"{len(files)} file(s)"
        elif max_depth > 1:
            location = f"recursively ({max_depth} levels deep)"
        else:
            location = "in current directory"

        print(f"{CYAN}Running Scanning {location}...{RESET}")

        # Use provided files or find them
        mkv_files = files if files else self._find_mkv_files(max_depth)

        # DVY-35: Early return if no files found
        if not mkv_files:
            print("No MKV files found.")
            return

        # Print table header
        print(f"{'Filename':<50} {'Format':<36} Action")
        print("-" * 96)

        simple_count = 0
        candidate_count = 0
        filtered_count = 0
        candidates_only = self.config.candidates_only

        # Only show directory headers if multiple directories
        unique_dirs = len(set(f.parent for f in mkv_files))
        show_headers = unique_dirs > 1
        current_directory = None

        for mkv_file in mkv_files:
            self.analyze_file(mkv_file)

            # DVY-46: Filter for candidates only
            is_candidate = "CONVERT" in self.action
            if candidates_only and not is_candidate:
                filtered_count += 1
                continue

            if is_candidate:
                candidate_count += 1

            # Directory header for grouping (only if multiple directories)
            if show_headers and mkv_file.parent != current_directory:
                if current_directory is not None:
                    print()  # Blank line between directories
                print(f"{BOLD}DIRECTORY: {mkv_file.parent}{RESET}")
                current_directory = mkv_file.parent

            name = mkv_file.name
            if len(name) > 50:
                name = name[:47] + "..."

            if "FEL (Simple)" in self.dovi_status:
                simple_count += 1

            # Strip ANSI codes for length calculation
            import re
            status_plain = re.sub(r'\033\[[0-9;]*m', '', self.dovi_status)
            action_plain = re.sub(r'\033\[[0-9;]*m', '', self.action)

            print(f"{name:<50} {self.dovi_status:<36} {self.action}")

        # DVY-46: Summary for candidates-only mode
        if candidates_only:
            if candidate_count == 0:
                print("\nNo conversion candidates found.")
            else:
                print(f"\nShowing {candidate_count} candidate(s). {filtered_count} file(s) filtered.")

        # Conditional Advisory
        if simple_count > 0:
            print()
            self._print_simple_fel_advisory()

    def _print_simple_fel_advisory(self) -> None:
        """Print the Simple FEL advisory block."""
        print("=" * 96)
        print(f"{RED}*{RESET}{BOLD}ADVISORY: UNDERSTANDING 'SIMPLE' (CYAN) VERDICTS{RESET}")
        print("-" * 96)
        print(f"{BOLD}What is 'Simple FEL'?{RESET}")
        print("It means the scan detected no active brightness expansion over the Base Layer. This")
        print("suggests the file is likely safe to convert. But:")
        print()
        print(f"{BOLD}How accurate is the scan?{RESET}")
        print("The script takes 10 samples at different timestamps of the video to analyze peak brightness in")
        print("the FEL. While this is statistically accurate enough to determine whether the FEL expands luminance")
        print("over the Base Layer, it can't guarantee a definitive result. If accurate preservation is paramount")
        print("for a specific file, please verify it with -inspect before converting.")
        print("=" * 96)

    def _find_mkv_files(self, max_depth: int, paths: List[Path] = None) -> List[Path]:
        """
        Find MKV files up to max_depth.
        If paths provided, search those. Otherwise search current directory.
        """
        files = []

        # Default to current directory if no paths specified
        search_dirs = paths if paths else [Path.cwd()]

        for search_path in search_dirs:
            if search_path.is_file():
                # Direct file - include if it's an MKV
                if search_path.suffix.lower() == ".mkv" and not search_path.name.startswith("._"):
                    files.append(search_path)
            elif search_path.is_dir():
                # Directory - glob for MKV files
                if max_depth == 1:
                    for f in search_path.glob("*.mkv"):
                        if not f.name.startswith("._"):
                            files.append(f)
                else:
                    for depth in range(max_depth + 1):
                        pattern = "/".join(["*"] * depth) + "/*.mkv" if depth > 0 else "*.mkv"
                        for f in search_path.glob(pattern):
                            if not f.name.startswith("._") and "._" not in str(f):
                                files.append(f)

        return sorted(set(files))

    def collect_inputs(self, args: List[str], command: str, depth: int = 1) -> List[Path]:
        """
        Shared input handler - the 'bouncer'.
        Validates inputs based on command rules and returns list of files to process.

        Rules:
        - convert: Files only (directories error)
        - batch: Directories only (files error)
        - scan: Both files and directories allowed
        """
        files: List[Path] = []
        directories: List[Path] = []

        for arg in args:
            path = Path(arg)
            if path.is_file():
                if command == "batch":
                    print(f"{RED}Error: '{arg}' is a file. Use -convert for specific files.{RESET}")
                    sys.exit(1)
                files.append(path)
            elif path.is_dir():
                if command == "convert":
                    print(f"{RED}Error: '{arg}' is a directory. Use -batch to process directories.{RESET}")
                    sys.exit(1)
                directories.append(path)
            else:
                print(f"{RED}Error: '{arg}' not found.{RESET}")
                sys.exit(1)

        # If directories provided, find MKV files in them
        if directories:
            files.extend(self._find_mkv_files(depth, directories))

        # If no args at all, default to current directory (for scan/batch)
        if not args and command != "convert":
            files = self._find_mkv_files(depth)

        return files


    def _build_batch_source_map(self, source_dirs: List[Path], max_depth: int) -> Dict[Path, Path]:
        """
        Map files to their source directories and detect basename collisions.
        Only called when -o flag is used.

        Args:
            source_dirs: List of directories specified by user
            max_depth: Recursion depth for file discovery

        Returns:
            Dictionary mapping file_path -> source_directory

        Raises:
            SystemExit: If basename collision detected
        """
        # Pre-flight check: detect basename collisions
        basenames = [d.name for d in source_dirs]
        if len(basenames) != len(set(basenames)):
            # Find duplicates
            seen = {}
            duplicates = {}
            for d in source_dirs:
                basename = d.name
                if basename in seen:
                    if basename not in duplicates:
                        duplicates[basename] = [seen[basename]]
                    duplicates[basename].append(d)
                else:
                    seen[basename] = d

            # Format error message
            print(f"{RED}Error: Basename collision detected.{RESET}")
            for basename, paths in duplicates.items():
                print(f"\nMultiple source directories share the same name '{basename}':")
                for p in paths:
                    print(f"  - {p}")
                print(f"\nBoth would write to: {self.config.output_dir}/{basename}/")

            print(f"\n{BOLD}Solution:{RESET} Process directories separately:")
            for i, (basename, paths) in enumerate(duplicates.items()):
                for j, p in enumerate(paths):
                    suffix = f"_{j+1}" if len(paths) > 1 else ""
                    print(f"  dovi_convert -batch {p} -o {self.config.output_dir}/{basename}{suffix}/")

            sys.exit(1)

        # Build file-to-source mapping
        file_map: Dict[Path, Path] = {}
        for source_dir in source_dirs:
            files = self._find_mkv_files(max_depth, [source_dir])
            for file in files:
                file_map[file] = source_dir

        return file_map

    def cmd_batch(self, max_depth: int = 1, files: List[Path] = None, source_dirs: List[Path] = None) -> None:
        """Batch processing of directory or provided file list."""

        conversion_queue: List[Path] = []
        simple_fel_queue: List[Path] = []

        ignored_count = 0
        skipped_count = 0
        complex_count = 0
        simple_count = 0
        forced_count = 0
        total_batch_size = 0

        if files:
            print(f"{BOLD}Processing {len(files)} file(s)...{RESET}")
        else:
            print(f"{BOLD}Scanning for Profile 7 files (Depth: {max_depth})...{RESET}")

        # Use provided files or find them
        mkv_files = files if files else self._find_mkv_files(max_depth)

        for mkv_file in mkv_files:
            self.analyze_file(mkv_file)

            is_simple = "FEL (Simple)" in self.dovi_status

            if "CONVERT" in self.action:
                conversion_queue.append(mkv_file)

                if "FORCED" in self.action:
                    forced_count += 1
                elif is_simple:
                    simple_count += 1
                    simple_fel_queue.append(mkv_file)
                else:
                    simple_count += 1  # MEL counts here too

                total_batch_size += get_file_size(mkv_file)

            elif self.action == "IGNORE":
                ignored_count += 1
            elif self.scan_result and self.scan_result.verdict == "COMPLEX":
                complex_count += 1
            else:
                skipped_count += 1

        if len(conversion_queue) == 0 and complex_count == 0:
            print(f"No Profile 7 files found (Ignored: {ignored_count}).")
            self.batch_running = False
            return

        # Interactive overview
        queue_count = len(conversion_queue)
        total_size_gb = human_size_gb(total_batch_size)
        simple_fel_count = len(simple_fel_queue)
        mel_count = simple_count - simple_fel_count

        print(f"\n{BOLD}Batch Overview:{RESET}")

        if mel_count > 0:
            print(f"  Convert:        {GREEN}{mel_count}{RESET}   (MEL - Safe)")
        if simple_fel_count > 0:
            print(f"  Convert:        {CYAN}{simple_fel_count}{RESET}   (Simple FEL - Likely Safe)")
        if forced_count > 0:
            print(f"  Convert:        {YELLOW}{forced_count}{RESET}   (Complex FEL - Forced)")
        if complex_count > 0:
            print(f"  Skip:           {RED}{complex_count}{RESET}   (Complex FEL)")
        print(f"  Queue Size:     {CYAN}{total_size_gb}{RESET} ({queue_count} files)")

        # Safety gate
        if self.config.auto_yes:
            if simple_fel_count > 0 and not self.config.include_simple:
                print(f"\n{YELLOW}[!] {simple_fel_count} Simple-FEL files detected.{RESET}")
                print(f"    To analyze them, use {BOLD}-inspect{RESET}, to include them in batch conversions, use {BOLD}-include-simple{RESET}")
                print(f"    Skipping {simple_fel_count} files. Proceeding with the remaining files...")

                # Filter out Simple FEL files
                conversion_queue = [f for f in conversion_queue if f not in simple_fel_queue]

                # Update counters
                simple_count -= simple_fel_count
                simple_fel_count = 0
                queue_count = len(conversion_queue)

                if queue_count == 0:
                    print(f"\nNo safe files remaining for conversion.")
                    self.batch_running = False
                    return

            print(f"{YELLOW}Auto-Yes (-y) active. Starting conversion immediately...{RESET}")
            time.sleep(2)
        else:
            if queue_count == 0:
                print("\nNo files eligible for conversion.")
                print(f"Ignored: {ignored_count} (Not P7), Complex FEL: {complex_count} (Unsafe), Skipped: {skipped_count} (Invalid).")
                self.batch_running = False
                return

            try:
                reply = input("\nShow file list? (y/N) ").strip().lower()
            except EOFError:
                reply = "n"

            if reply == "y":
                print(f"\n{BOLD}Conversion Queue:{RESET}")
                for f in conversion_queue:
                    print(f" - {f}")

            if simple_fel_count > 0:
                print(f"\n{YELLOW}[!] WARNING: Batch includes {simple_fel_count} 'Simple FEL' files.{RESET}")
                print("    For details, run -scan first.")
                try:
                    reply = input("    Include Simple-FEL files in batch? (y/N) ").strip().lower()
                except EOFError:
                    reply = "n"

                if reply == "y":
                    self.config.include_simple = True
                else:
                    print(f"    {YELLOW}Excluding {simple_fel_count} Simple FEL files from run.{RESET}")
                    # Filter out Simple FEL files
                    conversion_queue = [f for f in conversion_queue if f not in simple_fel_queue]
                    queue_count = len(conversion_queue)

                    if queue_count == 0:
                        print(f"\n{YELLOW}No files remaining after exclusion. Exiting.{RESET}")
                        self.batch_running = False
                        return

            try:
                reply = input(f"\nProceed with conversion of {queue_count} files? (Y/n) ").strip().lower()
            except EOFError:
                reply = "n"

            if reply not in ("y", ""):
                print("Batch cancelled.")
                self.batch_running = False
                return

        print()
        print("=" * 51)
        self.batch_running = True
        print("BATCH PROCESSING STARTED")
        print("=" * 51)

        # Build file-to-source mapping if -o flag is used
        file_source_map: Dict[Path, Path] = {}
        if self.config.output_dir and source_dirs:
            file_source_map = self._build_batch_source_map(source_dirs, max_depth)

        success_list: List[str] = []
        fail_list: List[str] = []
        success_simple_count = 0
        success_forced_count = 0
        success_simple_fel_count = 0

        for idx, filepath in enumerate(conversion_queue, 1):
            if self.abort_requested:
                break

            print("Batch:")
            print(f"{BOLD}[{idx}/{queue_count}]{RESET} {filepath.name}")
            print("---------------------------------------------------")

            # Re-analyze for fresh state
            self.analyze_file(filepath)

            # Get source directory from map (None if -o not used or file not in map)
            batch_source = file_source_map.get(filepath, None)

            res = self.cmd_convert(filepath, "auto", batch_source)

            if res == 130 or self.abort_requested:
                break
            elif res == 0:
                success_list.append(filepath.name)
                if self.scan_result and self.scan_result.verdict == "COMPLEX":
                    success_forced_count += 1
                else:
                    success_simple_count += 1
                    if "Simple" in self.dovi_status:
                        success_simple_fel_count += 1
            elif res == 2:
                skipped_count += 1
            else:
                fail_list.append(filepath.name)

        self.batch_running = False

        print(f"\n{'=' * 51}")
        if self.abort_requested:
            print(f"           {YELLOW}{BOLD}BATCH ABORTED BY USER{RESET}")
        else:
            print(f"           {BOLD}BATCH PROCESSING COMPLETE{RESET}")
        print("=" * 51)
        print("Processed:")

        if success_simple_count > 0:
            mel_count = success_simple_count - success_simple_fel_count
            if success_simple_fel_count > 0:
                breakdown = f"({CYAN}{success_simple_fel_count} Simple FEL{RESET} / {GREEN}{mel_count} MEL{RESET})"
            else:
                breakdown = f"({GREEN}MEL / Safe{RESET})"
            print(f"  - Converted:   {GREEN}{success_simple_count}{RESET}   {breakdown}")

        if success_forced_count > 0:
            print(f"  - Converted:   {YELLOW}{success_forced_count}{RESET}   (Complex FEL - Forced)")

        if len(success_list) == 0:
            print("  - Converted:   0")
        print(f"  - Failed:      {RED}{len(fail_list)}{RESET}")
        print()
        print("Not Processed:")
        print(f"  - Ignored:     {CYAN}{ignored_count}{RESET}   (Not Profile 7)")
        print(f"  - Complex FEL: {RED}{complex_count}{RESET}   (Unsafe / Skipped)")
        print(f"  - Invalid:     {YELLOW}{skipped_count}{RESET}   (Corrupt / No Track)")

        if fail_list:
            print("---------------------------------------------------")
            print(f"{YELLOW}Failed Files (Likely Seamless Branching / Stream Issues):{RESET}")
            for f in fail_list:
                print(f" - {f}")
            print()
            print(f"{BOLD}Suggestion:{RESET} Try converting these specific files using Safe Mode:")
            print('  dovi_convert -convert "filename.mkv" -safe')

        print("=" * 51)
        send_notification("dovi_convert", "Batch Complete.")

    def cmd_cleanup(self, recursive: bool = False) -> None:
        """Clean up backup files."""
        if recursive:
            print(f"{BOLD}Scanning for .bak.dovi_convert files (Recursive)...{RESET}")
        else:
            print(f"{BOLD}Scanning for .bak.dovi_convert files (Current Directory)...{RESET}")

        files: List[Path] = []
        total_size = 0
        cwd = Path.cwd()

        # Find backup files
        pattern = "**/*.mkv.bak.dovi_convert" if recursive else "*.mkv.bak.dovi_convert"

        for f in cwd.glob(pattern):
            parent_file = Path(str(f).replace(".bak.dovi_convert", ""))

            if parent_file.exists():
                files.append(f)
                total_size += get_file_size(f)
            else:
                print(f"{YELLOW}Skipping Orphan Backup:{RESET} {f.name}")

        if not files:
            print("No valid backup files found.")
            return

        print(f"\n{BOLD}Files found:{RESET}")
        for f in files:
            print(f" - {f}")

        size_gb = human_size_gb(total_size)
        print(f"\nFound {BOLD}{len(files)} valid backups{RESET} utilizing {BOLD}{size_gb}{RESET}.")

        if self.config.auto_yes:
            print(f"{YELLOW}Auto-Yes (-y) active. Deleting files...{RESET}")
            reply = "y"
        else:
            try:
                reply = input("Delete them? (y/N) ").strip().lower()
            except EOFError:
                reply = "n"

        if reply == "y":
            for f in files:
                f.unlink()
                print(f"Deleted: {f.name}")
        else:
            print("Cancelled.")

    def _inspect_mel_fast_pass(self, filepath: Path) -> bool:
        """Run a quick check for MEL to avoid full scan if possible. Returns True if MEL detected."""
        spinner = Spinner("Checking EL Structure (Pre-Flight)... ")
        spinner.start()

        mel_detected = False
        pf_hevc = filepath.parent / f"inspect_pf_{int(time.time())}_{os.getpid()}.hevc"
        pf_rpu = pf_hevc.with_suffix(".rpu")
        pf_json = pf_hevc.with_suffix(".json")

        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(filepath),
                 "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-t", "1",
                 str(pf_hevc)],
                capture_output=True,
                stdin=subprocess.DEVNULL
            )

            if pf_hevc.exists() and pf_hevc.stat().st_size > 0:
                subprocess.run(
                    ["dovi_tool", "extract-rpu", str(pf_hevc), "-o", str(pf_rpu)],
                    capture_output=True
                )

                if pf_rpu.exists() and pf_rpu.stat().st_size > 0:
                    subprocess.run(
                        ["dovi_tool", "export", "-i", str(pf_rpu), "-d", f"all={pf_json}"],
                        capture_output=True
                    )

                    if pf_json.exists() and pf_json.stat().st_size > 0:
                        content = pf_json.read_text()
                        if '"el_type":"MEL"' in content:
                            mel_detected = True
        except Exception as e:
            if self.config.debug_mode:
                self.media.log(f"MEL fast pass exception: {e}")

        pf_hevc.unlink(missing_ok=True)
        pf_rpu.unlink(missing_ok=True)
        pf_json.unlink(missing_ok=True)
        spinner.stop()

        if mel_detected:
            print(f"\r\033[KChecking EL Structure... Done (MEL Detected).")
            print("---------------------------------------------------")
            print(f"{BOLD}VERDICT:{RESET}    {GREEN}MEL (Minimal Enhancement Layer){RESET}")
            print("---------------------------------------------------")
            print(f"{BOLD}ADVISORY:{RESET}")
            print("File is identified as MEL (empty enhancement layer).")
            print("It contains no video data to discard.")
            print("Absolutely safe to convert.")
            print("=" * 51)
            print()
            return True

        print(f"\r\033[KChecking EL Structure... Done (FEL Detected - Proceeding).")
        return False

    def _inspect_extract_rpu_loop(self, filepath: Path, temp_rpu: Path) -> bool:
        """Handle RPU extraction with retry logic. Returns True on success."""
        use_safe_mode = self.config.safe_mode

        while True:
            if not use_safe_mode:
                spinner = Spinner("Extracting RPU... ")
                spinner.start()

                try:
                    ffmpeg_proc = subprocess.Popen(
                        ["ffmpeg", "-v", "error", "-i", str(filepath),
                         "-map", f"0:{self.video_info.track_id}",
                         "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )

                    dovi_proc = subprocess.Popen(
                        ["dovi_tool", "extract-rpu", "-", "-o", str(temp_rpu)],
                        stdin=ffmpeg_proc.stdout,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )

                    ffmpeg_proc.stdout.close()
                    _, dovi_stderr = dovi_proc.communicate()
                    _, ffmpeg_stderr = ffmpeg_proc.communicate()

                    ffmpeg_status = ffmpeg_proc.returncode
                    dovi_status = dovi_proc.returncode

                    if ffmpeg_status != 0:
                        status = 1
                        if self.config.debug_mode:
                            self.media.log(f"ffmpeg failed: {ffmpeg_stderr.decode() if ffmpeg_stderr else 'Unknown error'}")
                    else:
                        status = dovi_status
                except Exception:
                    status = 1

                spinner.stop()

                if status == 0 and temp_rpu.exists() and temp_rpu.stat().st_size > 0:
                    print(f"\r\033[KExtracting RPU... Done.")
                    return True
                else:
                    print(f"\r\033[KExtracting RPU... {RED}Failed.{RESET}")
                    temp_rpu.unlink(missing_ok=True)

                    if self.config.auto_yes:
                        print(f"{YELLOW}Retrying with Safe Mode (Auto-Yes).{RESET}")
                        use_safe_mode = True
                        continue
                    else:
                        try:
                            reply = input("Retry using Safe Mode (Extraction to Disk)? [Y/n] ").strip().lower()
                        except EOFError:
                            reply = "n"
                        if reply in ("y", ""):
                            use_safe_mode = True
                            continue
                        else:
                            print(f"{RED}Aborted.{RESET}")
                            return False
            else:
                raw_temp = filepath.parent / f"inspect_temp_{int(time.time())}_{os.getpid()}.hevc"

                spinner = Spinner("Extracting Track (Safe Mode)... ")
                spinner.start()

                ret, _, _ = self.media.run_logged(
                    ["mkvextract", str(filepath), "tracks", f"{self.video_info.track_id}:{raw_temp}"]
                )
                spinner.stop()

                if ret != 0:
                    print(f"\n{RED}Extracting Track Failed.{RESET}")
                    raw_temp.unlink(missing_ok=True)
                    return False

                print(f"\r\033[KExtracting Track... Done.")

                spinner = Spinner("Extracting RPU... ")
                spinner.start()

                ret, _, _ = self.media.run_logged(
                    ["dovi_tool", "extract-rpu", str(raw_temp), "-o", str(temp_rpu)]
                )
                spinner.stop()
                raw_temp.unlink(missing_ok=True)

                if ret == 0 and temp_rpu.exists() and temp_rpu.stat().st_size > 0:
                    print(f"\r\033[KExtracting RPU... Done.")
                    return True
                else:
                    print(f"\n{RED}RPU Extraction Failed.{RESET}")
                    temp_rpu.unlink(missing_ok=True)
                    return False

    def _inspect_analyze_peak(self, temp_json: Path) -> Tuple[int, int]:
        """Analyze L1 metadata for peak brightness. Returns (peak_nits, frame_count)."""
        spinner = Spinner("Calculating Peak Brightness (99.9th)... ")
        spinner.start()

        try:
            # 1. Try Fast Stream Parser (Low Memory)
            max_vals = self._extract_l1_stream(temp_json)

            if not max_vals:
                # 2. Fallback to Deep JSON Parse
                if self.config.debug_mode:
                    print(f"\n{YELLOW}[!] Fast stream scan failed. Falling back to deep JSON parse...{RESET}")

                json_content = temp_json.read_text()
                data = json.loads(json_content)

                max_vals = []
                def find_l1(obj):
                    if isinstance(obj, dict):
                        for key in ["Level1", "l1", "L1"]:
                            if key in obj:
                                l1_data = obj[key]
                                if isinstance(l1_data, dict):
                                    for mkey in ["max_pq", "max", "Max"]:
                                        if mkey in l1_data:
                                            val = l1_data[mkey]
                                            if isinstance(val, (int, float)):
                                                max_vals.append(int(val))
                        for v in obj.values():
                            find_l1(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            find_l1(item)

                find_l1(data)

            max_vals.sort()
            frame_count = len(max_vals)
            if frame_count > 0:
                idx = int(frame_count * 0.999)
                robust_peak = max_vals[min(idx, len(max_vals) - 1)]
            else:
                robust_peak = 0

        except Exception:
            frame_count = 0
            robust_peak = 0

        spinner.stop()

        if robust_peak > 0:
            robust_peak = pq_to_nits(robust_peak)

        return robust_peak, frame_count

    def _inspect_print_report(self, filepath: Path, peak_nits: int, frame_count: int) -> None:
        """Print the final inspection report."""
        print(f"\r\033[KCalculating Peak Brightness... Done.")

        bl_peak, is_default = self.media.get_bl_peak(filepath)
        threshold = bl_peak + 50

        if frame_count == 0:
            verdict = f"{YELLOW}NO L1 METADATA{RESET}"
            advisory = f"{RED}WARNING:{RESET} No valid L1 brightness metadata found in FEL.\nThis is unusual for non-MEL files. Proceed with caution."
            robust_peak_str = "N/A"
        elif peak_nits > threshold:
            verdict = f"{RED}COMPLEX FEL (Active Brightness Expansion){RESET}"
            diff = peak_nits - bl_peak
            advisory = f"{BOLD}ADVISORY:{RESET}\nFEL Peak ({peak_nits} nits) exceeds Base Layer ({bl_peak} nits).\nThis indicates active brightness expansion in the FEL.\nConversion will likely cause clipping or tone-mapping errors."
            robust_peak_str = str(peak_nits)
        else:
            verdict = f"{GREEN}SIMPLE / SAFE{RESET}"
            advisory = f"{BOLD}ADVISORY:{RESET}\nFEL Peak ({peak_nits} nits) is within safe range of Base Layer ({bl_peak} nits).\nSafe to convert."
            robust_peak_str = str(peak_nits)

        default_suffix = " (default)" if is_default else ""
        print(f"{'Base Layer Peak (MaxCLL):':<26} {bl_peak} nits{default_suffix}")
        print(f"{'L1 Analysis:':<26} {frame_count} frames analyzed")
        print(f"{'FEL Peak Brightness:':<26} {robust_peak_str} nits")

        if isinstance(peak_nits, int) and peak_nits > threshold:
            print(f"{'Brightness Expansion:':<26} {RED}+{peak_nits - bl_peak} nits (Active){RESET}")
        else:
            print(f"{'Brightness Expansion:':<26} {GREEN}None (Safe){RESET}")
            print("---------------------------------------------------")
            print(f"{BOLD}VERDICT:{RESET}    {verdict}")
            print("---------------------------------------------------")
            print(advisory)
            print("=" * 51)
            print()

    def cmd_inspect(self, filepath: Path) -> None:
        """Full frame-by-frame RPU inspection."""
        if not filepath.exists():
            print(f"{RED}Error: File '{filepath}' not found.{RESET}")
            return

        # Basic validation
        self.video_info = self.media.get_video_info(filepath)
        mi = self.video_info.mi_info_string

        if "dvhe.07" not in mi and "Profile 7" not in mi:
            print(f"{RED}Error: File is not Dolby Vision Profile 7.{RESET}")
            print(f"Detected: {self.dovi_status} (Info: {mi})")
            return

        print()
        print("=" * 51)
        print("FULL RPU STRUCTURE INSPECTION")
        print("=" * 51)
        print(f"File:       {BOLD}{filepath.name}{RESET}")
        print("Format:     DV Profile 7 (Scanning...)")
        print("---------------------------------------------------")

        # 1. MEL Fast Pass
        if self._inspect_mel_fast_pass(filepath):
            return

        # 2. Extract RPU
        temp_rpu = filepath.parent / f"inspect_{int(time.time())}_{os.getpid()}.rpu"
        temp_json = filepath.parent / f"inspect_{int(time.time())}_{os.getpid()}.json"

        if not self._inspect_extract_rpu_loop(filepath, temp_rpu):
            return

        # 3. Export Metadata
        spinner = Spinner("Exporting Metadata... ")
        spinner.start()

        ret, _, _ = self.media.run_logged(
            ["dovi_tool", "export", "-i", str(temp_rpu), "-d", f"all={temp_json}"]
        )

        spinner.stop()
        temp_rpu.unlink(missing_ok=True)

        if ret != 0 or not temp_json.exists() or temp_json.stat().st_size == 0:
            print(f"\r\033[KExporting Metadata... {RED}Failed.{RESET}")
            temp_json.unlink(missing_ok=True)
            return

        print(f"\r\033[KExporting Metadata... Done.")

        # 4. Analyze
        peak_nits, frame_count = self._inspect_analyze_peak(temp_json)
        temp_json.unlink(missing_ok=True)

        # 5. Print Report
        self._inspect_print_report(filepath, peak_nits, frame_count)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main() -> None:
    """Main entry point."""
    # Pre-flight: Ghost directory check
    try:
        Path.cwd()
    except OSError:
        print(f"{RED}Error: Ghost directory detected.{RESET} Please 'cd .' or restart terminal.")
        sys.exit(1)

    # Pre-flight: Dependency check
    missing = DependencyManager.find_missing()
    if missing:
        print(f"{RED}Missing dependencies:{RESET} {' '.join(missing)}")
        print()
        try:
            reply = input("Would you like to install them automatically? (y/N) ").strip().lower()
        except EOFError:
            reply = "n"

        if reply == "y":
            if sys.platform == "darwin" and not shutil.which("brew"):
                print()
                print(f"{YELLOW}Homebrew is not installed.{RESET}")
                print("It's the recommended way to install dependencies on macOS.")
                print()
                print("Install it from: https://brew.sh")
                print()
                print("Then run dovi_convert again.")
                sys.exit(1)
            DependencyManager.install_dependencies(missing)
        else:
            print()
            print("Please install the missing dependencies manually:")
            for dep in missing:
                if dep == "dovi_tool":
                    print(f"  - dovi_tool: https://github.com/quietvoid/dovi_tool")
                else:
                    print(f"  - {dep}")

            if sys.platform == "darwin" and not shutil.which("brew"):
                print()
                print("Tip: Install Homebrew (https://brew.sh) - a universal package manager.")
                print("     Once installed, dovi_convert will use it to install dependencies automatically.")
            sys.exit(1)

    # Parse arguments (two-pass like Bash)
    config = Config()
    args = []

    for arg in sys.argv[1:]:
        if arg == "-force":
            config.force_mode = True
        elif arg == "-safe":
            config.safe_mode = True
        elif arg == "-y":
            config.auto_yes = True
        elif arg == "-include-simple":
            config.include_simple = True
        elif arg == "-debug":
            config.debug_mode = True
        elif arg == "-delete":
            config.delete_backup = True
        elif arg == "-hdr10":
            config.hdr10_mode = True
        elif arg in ("-temp", "--temp-dir", "-o"):
            # Mark for pass 2 (value parsing)
            args.append(arg)
        else:
            args.append(arg)

    # Pass 2: Parse -temp and -o value arguments
    final_args = []
    i = 0
    while i < len(args):
        if args[i] in ("-temp", "--temp-dir"):
            if i + 1 < len(args):
                temp_path = Path(args[i + 1])
                # Pre-flight validation
                if not temp_path.exists():
                    print(f"{RED}Error: Temp directory does not exist: {temp_path}{RESET}")
                    sys.exit(1)
                if not temp_path.is_dir():
                    print(f"{RED}Error: Temp path is not a directory: {temp_path}{RESET}")
                    sys.exit(1)
                # Writability check
                try:
                    test_file = temp_path / ".dovi_convert_write_test"
                    test_file.touch()
                    test_file.unlink()
                except Exception:
                    print(f"{RED}Error: Temp directory is not writable: {temp_path}{RESET}")
                    sys.exit(1)
                config.temp_dir = temp_path
                i += 2
            else:
                print(f"{RED}Error: -temp requires a path argument.{RESET}")
                sys.exit(1)
        elif args[i] == "-o":
            if i + 1 < len(args):
                output_path = Path(args[i + 1])
                # Create directory if it doesn't exist
                try:
                    output_path.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    print(f"{RED}Error: Could not create output directory: {output_path}{RESET}")
                    print(f"       {e}")
                    sys.exit(1)
                if not output_path.is_dir():
                    print(f"{RED}Error: Output path is not a directory: {output_path}{RESET}")
                    sys.exit(1)
                # Writability check
                try:
                    test_file = output_path / ".dovi_convert_write_test"
                    test_file.touch()
                    test_file.unlink()
                except Exception:
                    print(f"{RED}Error: Output directory is not writable: {output_path}{RESET}")
                    sys.exit(1)
                config.output_dir = output_path
                i += 2
            else:
                print(f"{RED}Error: -o requires a path argument.{RESET}")
                sys.exit(1)
        else:
            final_args.append(args[i])
            i += 1
    args = final_args

    app = DoviConvertApp(config)

    # No command: show usage
    if not args:
        UpdateChecker.check_foreground()
        app.print_usage()
        UpdateChecker.check_background()
        sys.exit(0)

    command = args[0]
    rest = args[1:]

    if command in ("-check", "-scan"):
        # Smart -r parsing: extract depth from anywhere in args
        depth = 1
        paths = []
        i = 0
        while i < len(rest):
            if rest[i] == "-r":
                depth = 5  # default if -r with no number
                if i + 1 < len(rest) and rest[i + 1].isdigit():
                    depth = int(rest[i + 1])
                    i += 1
            elif rest[i] == "-candidates":
                config.candidates_only = True
            else:
                paths.append(rest[i])
            i += 1

        # Use shared handler - accepts both files and directories
        files = app.collect_inputs(paths, "scan", depth)

        if len(files) == 1:
            # Single file - use existing single-file scan
            app.cmd_check_single(files[0])
        else:
            # Multiple files or directory scan
            app.cmd_check_all(depth, files if paths else None)

    elif command == "-convert":
        if not rest:
            print("Usage: -convert [file] [file2] ...")
            sys.exit(1)

        # Use shared handler - validates files, rejects directories
        files = app.collect_inputs(rest, "convert")

        if not files:
            print(f"{RED}Error: No valid MKV files found.{RESET}")
            sys.exit(1)

        if len(files) == 1:
            # Single file - existing behavior
            result = app.cmd_convert(files[0], "manual")
            sys.exit(result)
        else:
            # Multiple files - loop with summary
            success_count = 0
            fail_list = []

            for idx, filepath in enumerate(files, 1):
                print(f"\n{'=' * 51}")
                print(f"[{idx}/{len(files)}] {filepath.name}")
                print("=" * 51)

                result = app.cmd_convert(filepath, "manual")
                if result == 0:
                    success_count += 1
                elif result == 130:
                    # User abort
                    break
                else:
                    fail_list.append(filepath.name)

            # Summary
            print(f"\n{'=' * 51}")
            print(f"Processed {success_count} of {len(files)} files.")
            if fail_list:
                print(f"Failed: {', '.join(fail_list)}")
            print("=" * 51)
            sys.exit(0 if not fail_list else 1)

    elif command in ("-inspect", "--inspect"):
        if not rest:
            print("Usage: -inspect [file]")
            sys.exit(1)
        app.cmd_inspect(Path(rest[0]))

    elif command == "-batch":
        # HDR10 mode is single-file only
        if config.hdr10_mode:
            print(f"{RED}Error: -hdr10 is not available in batch mode.{RESET}")
            print("       HDR10 conversion should be done one file at a time.")
            sys.exit(1)

        # Smart -r parsing: extract depth from anywhere in args
        depth = 1
        paths = []
        i = 0
        while i < len(rest):
            if rest[i] == "-r":
                depth = 5  # default if -r with no number
                if i + 1 < len(rest) and rest[i + 1].isdigit():
                    depth = int(rest[i + 1])
                    i += 1
            else:
                paths.append(rest[i])
            i += 1

        # Track source directories for -o mode (before collect_inputs resolves to files)
        source_dirs = [Path(p) for p in paths if Path(p).is_dir()] if paths else None

        # Use shared handler - directories only, rejects files
        files = app.collect_inputs(paths, "batch", depth)

        app.cmd_batch(depth, files if paths else None, source_dirs)

    elif command == "-cleanup":
        recursive = "-r" in rest
        app.cmd_cleanup(recursive)

    elif command == "-update-check":
        UpdateChecker.check_manual()

    elif command in ("-help", "--help"):
        UpdateChecker.check_foreground()
        app.print_help()

    else:
        print(f"{RED}Unknown command: {command}{RESET}")
        app.print_usage()
        sys.exit(1)

    # Trigger background update check on clean exit
    UpdateChecker.check_background()


if __name__ == "__main__":
    main()
