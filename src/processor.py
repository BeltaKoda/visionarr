"""
Visionarr Processor

Core DoVi detection and Profile 7 to Profile 8 conversion logic.
Uses external CLI tools: mediainfo, dovi_tool, ffmpeg, mkvmerge.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class DoViProfile(Enum):
    """Dolby Vision profiles."""
    PROFILE_5 = 5
    PROFILE_7 = 7
    PROFILE_8 = 8
    UNKNOWN = -1


@dataclass
class MediaAnalysis:
    """Result of analyzing a media file."""
    file_path: Path
    has_dovi: bool
    dovi_profile: Optional[DoViProfile]
    video_codec: Optional[str]
    is_mkv: bool
    file_size_bytes: int
    
    @property
    def needs_conversion(self) -> bool:
        """Check if this file needs Profile 7 to 8 conversion."""
        return self.has_dovi and self.dovi_profile == DoViProfile.PROFILE_7


class ProcessorError(Exception):
    """Exception raised during processing."""
    pass


class Processor:
    """
    DoVi detection and conversion processor.
    
    Uses a two-stage detection approach:
    1. Fast scan with mediainfo to check for DoVi presence
    2. Confirm profile with dovi_tool for candidates
    """
    
    def __init__(self, temp_dir: Path, dry_run: bool = True, backup_enabled: bool = True):
        self.temp_dir = temp_dir
        self.dry_run = dry_run
        self.backup_enabled = backup_enabled
        
        # Verify required tools are available
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
    
    def _run_command(
        self,
        cmd: list,
        description: str,
        capture_output: bool = True
    ) -> subprocess.CompletedProcess:
        """Run a shell command with logging."""
        logger.debug(f"Running: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=capture_output,
                text=True,
                timeout=3600  # 1 hour timeout for long conversions
            )
            
            if result.returncode != 0:
                logger.error(f"{description} failed: {result.stderr}")
                raise ProcessorError(f"{description} failed: {result.stderr}")
            
            return result
            
        except subprocess.TimeoutExpired:
            raise ProcessorError(f"{description} timed out after 1 hour")
        except Exception as e:
            raise ProcessorError(f"{description} error: {e}")
    
    # -------------------------------------------------------------------------
    # Detection
    # -------------------------------------------------------------------------
    
    def analyze_file(self, file_path: Path) -> MediaAnalysis:
        """
        Analyze a media file for DoVi content.
        
        Two-stage approach:
        1. Fast mediainfo check for DoVi presence
        2. dovi_tool confirmation for Profile 7 specifically
        """
        if not file_path.exists():
            raise ProcessorError(f"File not found: {file_path}")
        
        file_size = file_path.stat().st_size
        is_mkv = file_path.suffix.lower() == ".mkv"
        
        # Stage 1: Fast mediainfo check (now returns profile if found)
        has_dovi, profile, video_codec = self._check_dovi_mediainfo(file_path)
        
        if not has_dovi:
            return MediaAnalysis(
                file_path=file_path,
                has_dovi=False,
                dovi_profile=None,
                video_codec=video_codec,
                is_mkv=is_mkv,
                file_size_bytes=file_size
            )
        
        # Stage 2: Fallback to dovi_tool if mediainfo profile is unknown
        if profile == DoViProfile.UNKNOWN:
            profile = self._get_dovi_profile(file_path)
        
        return MediaAnalysis(
            file_path=file_path,
            has_dovi=True,
            dovi_profile=profile,
            video_codec=video_codec,
            is_mkv=is_mkv,
            file_size_bytes=file_size
        )
    
    def _check_dovi_mediainfo(self, file_path: Path) -> Tuple[bool, DoViProfile, Optional[str]]:
        """
        Quick check for DoVi and profile using mediainfo.
        Returns (has_dovi, profile, video_codec).
        """
        try:
            result = self._run_command(
                ["mediainfo", "--Output=JSON", str(file_path)],
                "mediainfo analysis"
            )
            
            info = json.loads(result.stdout)
            tracks = info.get("media", {}).get("track", [])
            
            video_codec = None
            has_dovi = False
            profile = DoViProfile.UNKNOWN
            
            for track in tracks:
                if track.get("@type") == "Video":
                    video_codec = track.get("Format", "")
                    
                    # Check for DoVi indicators
                    hdr_format = track.get("HDR_Format", "")
                    hdr_format_profile = track.get("HDR_Format_Profile", "")
                    
                    # Map mediainfo profile strings to our enum
                    profile_str = hdr_format_profile.lower()
                    if "dvhe" in profile_str or "dvav" in profile_str or "dvh1" in profile_str:
                        has_dovi = True
                        if ".07" in profile_str:
                            profile = DoViProfile.PROFILE_7
                        elif ".08" in profile_str:
                            profile = DoViProfile.PROFILE_8
                        elif ".05" in profile_str:
                            profile = DoViProfile.PROFILE_5
                        elif "dvav.04" in profile_str:
                            # Sometimes mediainfo reports profile 4 which is obsolete
                            profile = DoViProfile.UNKNOWN
                        break
                    
                    if "Dolby Vision" in hdr_format:
                        has_dovi = True
                        break
            
            return has_dovi, profile, video_codec
            
        except Exception as e:
            logger.warning(f"mediainfo check failed: {e}")
            return False, DoViProfile.UNKNOWN, None
    
    def _get_dovi_profile(self, file_path: Path) -> DoViProfile:
        """
        Get exact DoVi profile using dovi_tool.
        
        This is more accurate but slower than mediainfo.
        """
        try:
            # First extract a small sample of the HEVC stream
            # dovi_tool needs raw HEVC, not the container
            import uuid
            sample_path = self.temp_dir / f"sample_{uuid.uuid4().hex}.hevc"
            
            try:
                # Extract first 50MB for quick analysis
                self._run_command(
                    [
                        "ffmpeg", "-y",
                        "-i", str(file_path),
                        "-c:v", "copy",
                        "-an", "-sn",
                        "-t", "10",  # First 10 seconds
                        "-f", "hevc",
                        str(sample_path)
                    ],
                    "HEVC sample extraction"
                )
                
                # Analyze with dovi_tool
                result = self._run_command(
                    ["dovi_tool", "info", "-i", str(sample_path), "--summary"],
                    "dovi_tool profile analysis"
                )
                
                # Parse output for profile
                output = result.stdout + result.stderr
                
                # Look for profile number in output
                # dovi_tool output includes "Profile: X" or similar
                if "profile 7" in output.lower() or "dvhe.07" in output.lower():
                    return DoViProfile.PROFILE_7
                elif "profile 8" in output.lower() or "dvhe.08" in output.lower():
                    return DoViProfile.PROFILE_8
                elif "profile 5" in output.lower() or "dvhe.05" in output.lower():
                    return DoViProfile.PROFILE_5
                else:
                    return DoViProfile.UNKNOWN
            
            finally:
                # Always clean up sample, even if analysis failed
                if sample_path.exists():
                    try:
                        sample_path.unlink()
                    except OSError:
                        pass
            
        except Exception as e:
            msg = str(e).lower()
            if "invalid rpu" in msg:
                logger.warning(f"dovi_tool check failed (invalid RPU data): {file_path.name}")
            else:
                logger.warning(f"dovi_tool profile detection failed: {e}")
            return DoViProfile.UNKNOWN
    
    # -------------------------------------------------------------------------
    # Conversion
    # -------------------------------------------------------------------------
    
    def convert_to_profile8(self, file_path: Path) -> Path:
        """
        Convert a Profile 7 MKV to Profile 8.
        
        Pipeline:
        1. Extract HEVC stream (ffmpeg - stream copy)
        2. Extract RPU (dovi_tool)
        3. Convert RPU Profile 7 -> 8 (dovi_tool)
        4. Inject new RPU into HEVC (dovi_tool)
        5. Remux with original audio/subs (mkvmerge)
        6. Atomic swap with backup
        
        Returns the path to the converted file.
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would convert: {file_path}")
            return file_path
        
        logger.info(f"Starting Profile 7 -> 8 conversion: {file_path}")
        
        # Create unique temp directory for this conversion
        work_dir = self.temp_dir / f"convert_{file_path.stem}_{os.getpid()}"
        work_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            # Paths for intermediate files
            hevc_path = work_dir / "video.hevc"
            rpu_path = work_dir / "rpu.bin"
            hevc_p8_path = work_dir / "video_p8.hevc"
            output_partial = file_path.with_suffix(".mkv.partial")
            output_backup = file_path.with_suffix(".mkv.original")
            
            # Step 1: Extract HEVC stream
            logger.info("Step 1/5: Extracting HEVC stream...")
            self._run_command(
                [
                    "ffmpeg", "-y",
                    "-i", str(file_path),
                    "-c:v", "copy",
                    "-an", "-sn",
                    "-f", "hevc",
                    str(hevc_path)
                ],
                "HEVC extraction"
            )
            
            # Step 2: Extract RPU (Dolby Vision metadata)
            logger.info("Step 2/5: Extracting RPU metadata...")
            self._run_command(
                [
                    "dovi_tool", "extract-rpu",
                    "-i", str(hevc_path),
                    "-o", str(rpu_path)
                ],
                "RPU extraction"
            )
            
            # Step 3: Convert RPU from Profile 7 to Profile 8
            logger.info("Step 3/5: Converting RPU to Profile 8...")
            rpu_p8_path = work_dir / "rpu_p8.bin"
            self._run_command(
                [
                    "dovi_tool", "convert",
                    "--mode", "2",  # Mode 2 = Profile 7 to 8.1
                    "-i", str(rpu_path),
                    "-o", str(rpu_p8_path)
                ],
                "RPU conversion"
            )
            
            # Step 4: Inject new RPU back into HEVC
            logger.info("Step 4/5: Injecting Profile 8 RPU...")
            self._run_command(
                [
                    "dovi_tool", "inject-rpu",
                    "-i", str(hevc_path),
                    "--rpu-in", str(rpu_p8_path),
                    "-o", str(hevc_p8_path)
                ],
                "RPU injection"
            )
            
            # Step 5: Remux with original audio/subtitles
            logger.info("Step 5/5: Remuxing final MKV...")
            self._run_command(
                [
                    "mkvmerge",
                    "-o", str(output_partial),
                    str(hevc_p8_path),  # New video
                    "--no-video", str(file_path)  # Audio/subs from original
                ],
                "MKV remux"
            )
            
            # Atomic swap
            logger.info("Performing atomic file swap...")
            
            # Backup original
            if self.backup_enabled:
                shutil.move(str(file_path), str(output_backup))
                logger.info(f"Original backed up to: {output_backup}")
            else:
                file_path.unlink()
            
            # Move partial to final
            shutil.move(str(output_partial), str(file_path))
            logger.info(f"Conversion complete: {file_path}")
            
            return file_path
            
        except Exception as e:
            logger.error(f"Conversion failed: {e}")
            # Clean up partial file if it exists
            if output_partial.exists():
                output_partial.unlink()
            raise
            
        finally:
            # Clean up work directory
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
    
    def check_disk_space(self, file_path: Path, multiplier: float = 2.5) -> bool:
        """
        Check if there's enough disk space for conversion.

        We need approximately 2-2.5x the file size for temp files.
        """
        file_size = file_path.stat().st_size
        required_space = int(file_size * multiplier)

        # Check temp directory space (cross-platform)
        try:
            disk_usage = shutil.disk_usage(self.temp_dir)
            temp_free = disk_usage.free
        except (OSError, AttributeError):
            # Fallback for edge cases
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
        Clean up orphaned partial/temp files from interrupted conversions.

        Called on startup to handle crashed conversions.
        Returns count of files cleaned up.
        """
        count = 0

        # Clean up work directories in temp
        try:
            for item in self.temp_dir.iterdir():
                if item.is_dir() and item.name.startswith("convert_"):
                    logger.info(f"Cleaning up orphaned work directory: {item}")
                    try:
                        shutil.rmtree(item)
                        count += 1
                    except PermissionError as e:
                        logger.warning(f"Permission denied cleaning up {item}: {e}")
                    except OSError as e:
                        logger.warning(f"Could not clean up {item}: {e}")
        except PermissionError as e:
            logger.warning(f"Cannot access temp directory for cleanup: {e}")

        return count
