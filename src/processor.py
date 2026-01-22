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


class Processor:
    """
    DoVi detection and conversion processor.
    
    Uses a two-stage detection approach:
    1. Fast scan with mediainfo to check for DoVi presence
    2. Confirm profile with dovi_tool for candidates
    """
    
    def __init__(self, temp_dir: Path, backup_enabled: bool = True):
        self.temp_dir = temp_dir
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
    
    def _preallocate_file(self, file_path: Path, size_bytes: int) -> None:
        """
        Pre-allocate a file to reserve disk space (thick provisioning).
        This prevents issues on Unraid where thin-provisioned files can outgrow
        the cache drive mid-write.
        """
        size_gb = size_bytes / (1024**3)
        logger.debug(f"Pre-allocating {size_gb:.2f} GB for {file_path.name}")
        
        try:
            subprocess.run(
                ["fallocate", "-l", str(size_bytes), str(file_path)],
                check=True, capture_output=True, text=True
            )
            logger.debug(f"Pre-allocation successful: {file_path.name}")
        except subprocess.CalledProcessError as e:
            # Fallback: truncate (may use sparse file on some filesystems)
            logger.warning(f"fallocate failed ({e}), using truncate fallback")
            try:
                with open(file_path, 'wb') as f:
                    f.truncate(size_bytes)
            except Exception as fallback_error:
                logger.warning(f"Truncate fallback also failed: {fallback_error}")
                # Continue anyway - dovi_tool will create the file
        except FileNotFoundError:
            # fallocate not available on this system
            logger.debug("fallocate not available, skipping pre-allocation")

    def _pq_to_nits(self, pq_value: float) -> float:
        """Convert PQ (ST.2084) value to nits."""
        m1 = 0.1593017578125
        m2 = 78.84375
        c1 = 0.8359375
        c2 = 18.8515625
        c3 = 18.6875

        if pq_value <= 0:
            return 0.0

        pq_pow = pow(pq_value, 1.0 / m2)
        num = max(pq_pow - c1, 0.0)
        den = c2 - c3 * pq_pow

        if den <= 0:
            return 10000.0

        linear = pow(num / den, 1.0 / m1)
        return linear * 10000.0

    def _get_bl_peak_nits(self, file_path: Path) -> float:
        """Get base layer peak brightness (MaxCLL) from MediaInfo."""
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=JSON", str(file_path)],
                capture_output=True, text=True, timeout=60
            )
            info = json.loads(result.stdout)

            for track in info.get("media", {}).get("track", []):
                if track.get("@type") == "Video":
                    maxcll = track.get("MaxCLL", "")
                    if maxcll:
                        match = re.search(r"(\d+)", str(maxcll))
                        if match:
                            return float(match.group(1))

                    luminance = track.get("MasteringDisplay_Luminance", "")
                    if "max" in luminance.lower():
                        match = re.search(r"max:\s*(\d+)", luminance.lower())
                        if match:
                            return float(match.group(1))
        except Exception as e:
            logger.debug(f"Could not get MaxCLL: {e}")

        return 1000.0  # Default

    def _check_fel_complexity(self, file_path: Path, threshold_nits: float = 50.0) -> bool:
        """
        Comprehensive FEL complexity check.

        Checks 3 indicators at 10 sample points:
        1. L1 brightness expansion (max_pq > BL peak + threshold)
        2. Active mapping coefficients (non-zero polynomial/MMR)
        3. NLQ offsets (non-zero enhancement processing)

        Returns True if complex (unsafe), False if simple (safe).
        """
        import uuid

        bl_peak = self._get_bl_peak_nits(file_path)
        logger.debug(f"Base layer peak: {bl_peak} nits")

        # Get duration
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=JSON", str(file_path)],
                capture_output=True, text=True, timeout=60
            )
            info = json.loads(result.stdout)
            duration = 0.0
            for track in info.get("media", {}).get("track", []):
                if track.get("@type") == "Video":
                    duration = float(track.get("Duration", 0))
                    break
            if duration <= 0:
                logger.warning("Could not get duration, assuming complex FEL")
                return True
        except Exception:
            return True

        # Sample at 10 points
        sample_points = [duration * (0.05 + 0.1 * i) for i in range(10)]
        brightness_issues = 0
        mapping_issues = 0
        nlq_issues = 0
        valid_samples = 0

        for timestamp in sample_points:
            try:
                rpu_path = self.temp_dir / f"rpu_check_{uuid.uuid4().hex}.bin"
                json_path = self.temp_dir / f"rpu_check_{uuid.uuid4().hex}.json"

                try:
                    # Extract single frame RPU
                    ffmpeg_cmd = [
                        "ffmpeg", "-v", "error", "-y",
                        "-ss", str(timestamp),
                        "-i", str(file_path),
                        "-c:v", "copy",
                        "-bsf:v", "hevc_mp4toannexb",
                        "-f", "hevc",
                        "-frames:v", "1",
                        "-"
                    ]
                    dovi_cmd = ["dovi_tool", "extract-rpu", "-", "-o", str(rpu_path)]

                    ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    dovi_proc = subprocess.Popen(dovi_cmd, stdin=ffmpeg_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    ffmpeg_proc.stdout.close()
                    dovi_proc.communicate(timeout=30)

                    if dovi_proc.returncode != 0 or not rpu_path.exists():
                        continue

                    subprocess.run(
                        ["dovi_tool", "export", "-i", str(rpu_path), "-d", f"all={json_path}"],
                        capture_output=True, timeout=30
                    )

                    if not json_path.exists():
                        continue

                    with open(json_path, 'r') as f:
                        content = f.read()

                    valid_samples += 1

                    # Check 1: L1 brightness expansion
                    max_pq_match = re.search(r'"max_pq"\s*:\s*(\d+)', content)
                    if max_pq_match:
                        max_pq = int(max_pq_match.group(1)) / 4095.0
                        l1_nits = self._pq_to_nits(max_pq)
                        if l1_nits > bl_peak + threshold_nits:
                            brightness_issues += 1

                    # Check 2: Active mapping coefficients
                    # Look for non-zero polynomial or MMR data
                    if re.search(r'"mapping_idc"\s*:\s*[01]', content):
                        # Check for non-trivial coefficients
                        coef_match = re.search(r'"poly_coef[^"]*"\s*:\s*\[\s*([^\]]+)\]', content)
                        if coef_match:
                            coefs = coef_match.group(1)
                            # If any coefficient > small threshold, mapping is active
                            if re.search(r'[1-9]\d*', coefs):
                                mapping_issues += 1

                    # Check 3: NLQ offsets
                    nlq_offset_match = re.search(r'"nlq_offset"\s*:\s*(\d+)', content)
                    if nlq_offset_match:
                        offset = int(nlq_offset_match.group(1))
                        if offset > 0:
                            nlq_issues += 1

                finally:
                    for p in [rpu_path, json_path]:
                        if p.exists():
                            try:
                                p.unlink()
                            except OSError:
                                pass

            except Exception as e:
                logger.debug(f"Sample at {timestamp:.1f}s failed: {e}")
                continue

        # Need at least 50% valid samples
        if valid_samples < 5:
            logger.warning(f"Insufficient samples ({valid_samples}/10), assuming complex")
            return True

        # Log findings
        if brightness_issues or mapping_issues or nlq_issues:
            logger.info(
                f"FEL complexity: brightness={brightness_issues}, "
                f"mapping={mapping_issues}, nlq={nlq_issues} issues in {valid_samples} samples"
            )
            return True

        return False

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
        
        # Stage 1: Fast mediainfo check (now returns profile and el_type if found)
        has_dovi, profile, video_codec, el_type = self._check_dovi_mediainfo(file_path)
        
        if not has_dovi:
            return MediaAnalysis(
                file_path=file_path,
                has_dovi=False,
                dovi_profile=None,
                el_type=None,
                video_codec=video_codec,
                is_mkv=is_mkv,
                file_size_bytes=file_size
            )
        
        # Stage 2: Fallback to dovi_tool if mediainfo profile is unknown
        if profile == DoViProfile.UNKNOWN:
            profile = self._get_dovi_profile(file_path)
        
        # Stage 3: For Profile 7, always run accurate EL type detection (includes complexity check)
        # This overrides mediainfo's heuristic to properly classify FEL_SIMPLE vs FEL_COMPLEX
        if profile == DoViProfile.PROFILE_7:
            el_type = self._detect_el_type(file_path)
        
        return MediaAnalysis(
            file_path=file_path,
            has_dovi=True,
            dovi_profile=profile,
            el_type=el_type,
            video_codec=video_codec,
            is_mkv=is_mkv,
            file_size_bytes=file_size
        )
    
    def _check_dovi_mediainfo(self, file_path: Path) -> Tuple[bool, DoViProfile, Optional[str], Optional[ELType]]:
        """
        Quick check for DoVi and profile using mediainfo.
        Returns (has_dovi, profile, video_codec, el_type).
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
            el_type = ELType.UNKNOWN
            
            video_tracks = [t for t in tracks if t.get("@type") == "Video"]
            
            for track in video_tracks:
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
                            # Check if mediainfo explicitly mentions FEL for this or subsequent tracks
                            # Note: This is just a hint - _detect_el_type will do proper complexity analysis
                            for t in video_tracks:
                                features = t.get("HDR_Format_AdditionalFeatures", "").upper()
                                comm_name = t.get("HDR_Format_Commercial_Name", "").upper()
                                if "FEL" in features or "FULL ENHANCEMENT" in comm_name:
                                    el_type = ELType.FEL_COMPLEX  # Conservative default, refined later
                                    break
                                elif "MEL" in features or "MINIMAL ENHANCEMENT" in comm_name:
                                    el_type = ELType.MEL

                            # Heuristic: Dual video tracks in P7 usually means FEL if not explicitly MEL
                            if el_type == ELType.UNKNOWN and len(video_tracks) > 1:
                                # Check the second track's properties
                                el_track = video_tracks[1]
                                width = int(el_track.get("Width", 0))
                                bitrate = int(el_track.get("BitRate", 0))
                                if width >= 1920 or bitrate > 1000000: # Broad heuristic
                                    el_type = ELType.FEL_COMPLEX  # Conservative default, refined later
                                else:
                                    el_type = ELType.MEL
                        elif ".08" in profile_str:
                            profile = DoViProfile.PROFILE_8
                        elif ".05" in profile_str:
                            profile = DoViProfile.PROFILE_5
                        elif "dvav.04" in profile_str:
                            profile = DoViProfile.UNKNOWN
                        break
                    
                    if "Dolby Vision" in hdr_format:
                        has_dovi = True
                        break
            
            return has_dovi, profile, video_codec, el_type
            
        except Exception as e:
            logger.warning(f"mediainfo check failed: {e}")
            return False, DoViProfile.UNKNOWN, None, ELType.UNKNOWN
    
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

    def _detect_el_type(self, file_path: Path) -> ELType:
        """
        Detect Enhancement Layer type (FEL vs MEL) for Profile 7 files.

        Uses dovi_tool extract-rpu + export to get JSON with el_type field.
        Falls back to MEL if detection fails (most releases are MEL).
        """
        import uuid

        # Try with short sample first (fast), then longer if needed
        for duration in [5, 30]:
            try:
                rpu_path = self.temp_dir / f"rpu_{uuid.uuid4().hex}.bin"
                json_path = self.temp_dir / f"rpu_{uuid.uuid4().hex}.json"

                try:
                    # Pipe ffmpeg to dovi_tool extract-rpu (no temp video file)
                    ffmpeg_cmd = [
                        "ffmpeg", "-v", "error", "-y",
                        "-i", str(file_path),
                        "-c:v", "copy",
                        "-bsf:v", "hevc_mp4toannexb",
                        "-f", "hevc",
                        "-t", str(duration),
                        "-"
                    ]

                    dovi_cmd = ["dovi_tool", "extract-rpu", "-", "-o", str(rpu_path)]

                    # Run piped: ffmpeg | dovi_tool
                    ffmpeg_proc = subprocess.Popen(
                        ffmpeg_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    dovi_proc = subprocess.Popen(
                        dovi_cmd,
                        stdin=ffmpeg_proc.stdout,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    ffmpeg_proc.stdout.close()
                    dovi_proc.communicate(timeout=300)

                    if dovi_proc.returncode != 0 or not rpu_path.exists():
                        continue  # Try next duration

                    # Export RPU to JSON
                    export_result = subprocess.run(
                        ["dovi_tool", "export", "-i", str(rpu_path), "-d", f"all={json_path}"],
                        capture_output=True, text=True, timeout=60
                    )

                    if export_result.returncode != 0 or not json_path.exists():
                        continue

                    # Parse JSON for el_type
                    with open(json_path, 'r') as f:
                        content = f.read()

                    if '"el_type":"FEL"' in content or '"el_type": "FEL"' in content:
                        # FEL detected - check complexity to determine if safe
                        is_complex = self._check_fel_complexity(file_path)
                        if is_complex:
                            logger.info(f"Detected Complex FEL: {file_path.name}")
                            return ELType.FEL_COMPLEX
                        else:
                            logger.info(f"Detected Simple FEL (safe): {file_path.name}")
                            return ELType.FEL_SIMPLE
                    elif '"el_type":"MEL"' in content or '"el_type": "MEL"' in content:
                        logger.info(f"Detected MEL (Minimal Enhancement Layer): {file_path.name}")
                        return ELType.MEL

                    # el_type not in JSON - try next duration for more data

                finally:
                    # Clean up temp files
                    for p in [rpu_path, json_path]:
                        if p.exists():
                            try:
                                p.unlink()
                            except OSError:
                                pass

            except Exception as e:
                logger.debug(f"EL detection attempt failed (duration={duration}s): {e}")
                continue

        # All attempts failed - default to MEL (most releases are MEL)
        logger.warning(f"EL type detection failed for {file_path.name}, assuming MEL")
        return ELType.MEL
    
    # -------------------------------------------------------------------------
    # Conversion
    # -------------------------------------------------------------------------
    
    def convert_to_profile8(self, file_path: Path) -> Path:
        """
        Convert a Profile 7 MKV to Profile 8.

        Optimized pipeline using dovi_tool's direct MKV input:
        1. dovi_tool converts directly from MKV → P8 HEVC
        2. mkvmerge remuxes with original audio/subs
        3. Atomic swap with backup

        Returns the path to the converted file.
        """
        logger.info(f"Starting Profile 7 → 8 conversion: {file_path}")

        work_dir = self.temp_dir / f"convert_{file_path.stem}_{os.getpid()}"
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            hevc_p8_path = work_dir / "video_p8.hevc"
            output_partial = file_path.with_suffix(".mkv.partial")
            output_backup = file_path.with_suffix(".mkv.original")

            # Step 1: Convert directly from MKV (no ffmpeg extraction needed)
            logger.info("Step 1/2: Converting to Profile 8...")

            # Pre-allocate for Unraid cache safety
            source_size = file_path.stat().st_size
            # Estimate HEVC size as ~80% of MKV (audio/subs removed)
            estimated_hevc_size = int(source_size * 0.8)
            self._preallocate_file(hevc_p8_path, estimated_hevc_size)

            self._run_command(
                [
                    "dovi_tool", "-m", "2",
                    "convert", "--discard",
                    "-i", str(file_path),  # Direct MKV input
                    "-o", str(hevc_p8_path)
                ],
                "Profile 7 to 8 conversion"
            )

            # Step 2: Remux with original audio/subtitles
            logger.info("Step 2/2: Remuxing final MKV...")
            self._run_command(
                [
                    "mkvmerge",
                    "-o", str(output_partial),
                    str(hevc_p8_path),
                    "--no-video", str(file_path)
                ],
                "MKV remux"
            )

            # Atomic swap
            logger.info("Performing atomic file swap...")

            if self.backup_enabled:
                shutil.move(str(file_path), str(output_backup))
                logger.info(f"Original backed up to: {output_backup}")
            else:
                file_path.unlink()

            shutil.move(str(output_partial), str(file_path))
            logger.info(f"Conversion complete: {file_path}")

            return file_path

        except Exception as e:
            logger.error(f"Conversion failed: {e}")
            if output_partial.exists():
                output_partial.unlink()
            raise
            
        finally:
            # Clean up work directory
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
    
    def check_disk_space(self, file_path: Path, multiplier: float = 1.5) -> bool:
        """
        Check if there's enough disk space for conversion.

        With optimized pipeline (direct MKV input), we need ~1.5x file size for temp files.
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
