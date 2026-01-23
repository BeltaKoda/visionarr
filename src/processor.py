"""
Visionarr Processor

Core DoVi detection and Profile 7 to Profile 8 conversion logic.
Wrapper around dovi_convert.
"""

import logging
import os
import shutil
import signal
import contextlib
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

# Import dovi_convert from the library location
# We assume src/lib/dovi_convert.py exists
try:
    from src.lib.dovi_convert import DoviConvertApp, Config as DoviConfig, Spinner

    # Monkeypatch Spinner to prevent log spam (millions of ANSI chars)
    # We do this immediately upon import
    Spinner.start = lambda self: None
    Spinner.stop = lambda self: None
    Spinner._spin = lambda self: None

except ImportError:
    # Fallback for when running tests or different structure
    try:
        from .lib.dovi_convert import DoviConvertApp, Config as DoviConfig, Spinner
        Spinner.start = lambda self: None
        Spinner.stop = lambda self: None
        Spinner._spin = lambda self: None
    except ImportError:
        try:
            # Last resort - maybe in path
            import dovi_convert
            DoviConvertApp = dovi_convert.DoviConvertApp
            DoviConfig = dovi_convert.Config
            # Attempt to patch if Spinner exists
            if hasattr(dovi_convert, 'Spinner'):
                dovi_convert.Spinner.start = lambda self: None
                dovi_convert.Spinner.stop = lambda self: None
                dovi_convert.Spinner._spin = lambda self: None
        except ImportError:
            # Should not happen in Docker, but allows linting
            DoviConvertApp = None
            DoviConfig = None

logger = logging.getLogger(__name__)


class DoViProfile(Enum):
    """Dolby Vision profiles."""
    PROFILE_5 = 5
    PROFILE_7 = 7
    PROFILE_8 = 8
    UNKNOWN = -1


class ELType(Enum):
    """Enhancement Layer types for Profile 7."""
    MEL = "MEL"                           # Minimal Enhancement Layer - safe
    FEL_SIMPLE = "FEL_SIMPLE"             # Simple FEL - safe (negligible enhancement)
    FEL_COMPLEX = "FEL_COMPLEX"           # Complex FEL - NOT safe (quality loss)
    UNKNOWN = "UNKNOWN"


@dataclass
class MediaAnalysis:
    """Result of analyzing a media file."""
    file_path: Path
    has_dovi: bool
    dovi_profile: Optional[DoViProfile]
    el_type: Optional[ELType]  # FEL, MEL, or UNKNOWN for Profile 7
    video_codec: Optional[str]
    is_mkv: bool
    file_size_bytes: int
    
    @property
    def needs_conversion(self) -> bool:
        """Check if this file needs Profile 7 to 8 conversion."""
        return self.has_dovi and self.dovi_profile == DoViProfile.PROFILE_7
    
    @property
    def safe_to_auto_convert(self) -> bool:
        """Check if safe for automatic conversion."""
        if not self.needs_conversion:
            return False
        return self.el_type in (ELType.MEL, ELType.FEL_SIMPLE)


class ProcessorError(Exception):
    """Exception raised during processing."""
    pass


@contextlib.contextmanager
def safe_dovi_app_context():
    """
    Context manager to prevent dovi_convert from hijacking signals.
    DoviConvertApp.__init__ registers global signal handlers, which clobbers Visionarr's handlers.
    We temporarily intercept signal.signal calls during instantiation.
    """
    original_signal = signal.signal

    def noop_signal(sig, handler):
        # Log but do not register the handler
        pass

    signal.signal = noop_signal
    try:
        yield
    finally:
        signal.signal = original_signal


class Processor:
    """
    DoVi detection and conversion processor.
    Wraps dovi_convert.
    """
    
    def __init__(self, temp_dir: Path, backup_enabled: bool = True):
        self.temp_dir = temp_dir
        self.backup_enabled = backup_enabled
        
        if DoviConfig is None:
            raise ProcessorError("dovi_convert library not found. Please check installation.")

        # Initialize dovi_convert configuration
        self.config = DoviConfig()
        self.config.temp_dir = temp_dir
        self.config.debug_mode = False

        self._verify_tools()
    
    def _verify_tools(self) -> None:
        """Verify all required CLI tools are available."""
        tools = ["mediainfo", "ffmpeg", "mkvmerge", "dovi_tool"]
        missing = []
        
        for tool in tools:
            if not shutil.which(tool):
                missing.append(tool)
        
        if missing:
            raise ProcessorError(f"Missing required tools: {', '.join(missing)}")
        
        logger.info("All required tools verified: mediainfo, ffmpeg, mkvmerge, dovi_tool")

    def analyze_file(self, file_path: Path) -> MediaAnalysis:
        """
        Analyze a media file for DoVi content using dovi_convert.
        """
        if not file_path.exists():
            raise ProcessorError(f"File not found: {file_path}")
        
        # Create app instance safely
        with safe_dovi_app_context():
            app = DoviConvertApp(self.config)
        
        # Perform analysis
        try:
            # Suppress stdout during analysis to avoid clutter
            with open(os.devnull, 'w') as devnull:
                with contextlib.redirect_stdout(devnull):
                     app.analyze_file(file_path)
        except Exception as e:
            logger.error(f"dovi_convert analysis failed: {e}")
            raise ProcessorError(f"Analysis failed: {e}")
        
        # Map dovi_convert state to MediaAnalysis
        has_dovi = False
        profile = DoViProfile.UNKNOWN
        el_type = ELType.UNKNOWN
        video_codec = None
        
        # Check if video info was populated
        if app.video_info:
            mi_str = app.video_info.mi_info_string
            if "HEVC" in mi_str:
                video_codec = "HEVC"

            status = app.dovi_status

            if "Profile 7" in status:
                has_dovi = True
                profile = DoViProfile.PROFILE_7

                # Check EL Type from scan result
                if app.scan_result:
                    if app.scan_result.verdict == "COMPLEX":
                        el_type = ELType.FEL_COMPLEX
                    elif app.scan_result.verdict == "SAFE":
                        if "MEL" in app.scan_result.reason:
                            el_type = ELType.MEL
                        else:
                            el_type = ELType.FEL_SIMPLE
                    else:
                        if "Complex" in status:
                            el_type = ELType.FEL_COMPLEX
                        elif "Simple" in status:
                            el_type = ELType.FEL_SIMPLE
                        elif "MEL" in status:
                            el_type = ELType.MEL
                else:
                    if "Complex" in status:
                        el_type = ELType.FEL_COMPLEX
                    elif "Simple" in status:
                        el_type = ELType.FEL_SIMPLE
                    elif "MEL" in status:
                        el_type = ELType.MEL

            elif "Profile 8" in status:
                has_dovi = True
                profile = DoViProfile.PROFILE_8
            elif "Profile 5" in status:
                has_dovi = True
                profile = DoViProfile.PROFILE_5
            elif "Dolby Vision" in status:
                has_dovi = True
                profile = DoViProfile.UNKNOWN
        
        return MediaAnalysis(
            file_path=file_path,
            has_dovi=has_dovi,
            dovi_profile=profile,
            el_type=el_type,
            video_codec=video_codec,
            is_mkv=file_path.suffix.lower() == ".mkv",
            file_size_bytes=file_path.stat().st_size
        )

    def convert_to_profile8(self, file_path: Path, force_backup: bool = False) -> Path:
        """
        Convert a Profile 7 MKV to Profile 8 using dovi_convert.
        """
        logger.info(f"Starting Profile 7 → 8 conversion via dovi_convert: {file_path}")

        run_config = DoviConfig()
        run_config.temp_dir = self.temp_dir

        should_keep_backup = self.backup_enabled or force_backup
        run_config.delete_backup = not should_keep_backup

        # We rely on Visionarr's decision logic, so we force dovi_convert to proceed
        # even if it detects complex FEL (assuming Visionarr allowed it).
        run_config.force_mode = True

        try:
            with safe_dovi_app_context():
                app = DoviConvertApp(run_config)
            
            # Run conversion
            # Redirect stdout to capture progress/logs, or let it print?
            # User might want to see progress in docker logs.
            # Spinner is disabled, so log spam is reduced.
            # dovi_convert prints useful info ("Converting...", "Muxing...", "Success").
            # We'll let it print to stdout, which goes to Docker logs.
            result = app.cmd_convert(file_path, mode="manual")
            
            if result != 0:
                raise ProcessorError(f"dovi_convert failed with exit code {result}")
            
            # Post-processing: Rename backup file
            if should_keep_backup:
                dovi_backup = file_path.with_suffix(".mkv.bak.dovi_convert")
                visionarr_backup = file_path.with_suffix(".mkv.original")
                
                if dovi_backup.exists():
                    logger.info(f"Renaming backup: {dovi_backup.name} -> {visionarr_backup.name}")
                    dovi_backup.rename(visionarr_backup)
                else:
                    logger.warning("Expected backup file not found after conversion.")
            
            logger.info(f"Conversion complete: {file_path}")
            return file_path

        except Exception as e:
            logger.error(f"Conversion failed: {e}")
            raise ProcessorError(f"Conversion failed: {e}")
        finally:
             if 'app' in locals():
                 app._cleanup()

    def check_disk_space(self, file_path: Path, multiplier: float = 1.5) -> bool:
        """
        Check if there's enough disk space for conversion.
        """
        file_size = file_path.stat().st_size
        required_space = int(file_size * multiplier)

        try:
            disk_usage = shutil.disk_usage(self.temp_dir)
            temp_free = disk_usage.free
        except (OSError, AttributeError):
            logger.warning("Could not check disk space, proceeding anyway")
            return True

        if temp_free < required_space:
            logger.warning(
                f"Insufficient temp space: {temp_free / 1e9:.1f}GB available, "
                f"{required_space / 1e9:.1f}GB required"
            )
            return False

        return True
    
    def cleanup_orphaned_files(self) -> int:
        """
        Clean up orphaned partial/temp files.
        """
        count = 0
        try:
            for item in self.temp_dir.iterdir():
                if item.is_dir() and item.name.startswith("convert_"):
                    logger.info(f"Cleaning up orphaned work directory: {item}")
                    try:
                        shutil.rmtree(item)
                        count += 1
                    except Exception as e:
                        logger.warning(f"Could not clean up {item}: {e}")
                elif item.is_file() and (item.name.startswith("probe_") or item.name.startswith("inspect_")):
                     try:
                         item.unlink()
                         count += 1
                     except Exception:
                         pass
        except PermissionError as e:
            logger.warning(f"Cannot access temp directory for cleanup: {e}")

        return count
