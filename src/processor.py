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
        tools = ["mediainfo", "ffmpeg", "mkvmerge", "dovi_tool", "dovi_convert"]
        missing = []
        
        for tool in tools:
            if not shutil.which(tool):
                missing.append(tool)
        
        if missing:
            raise ProcessorError(f"Missing required tools: {', '.join(missing)}")
        
        logger.info("All required tools verified: mediainfo, ffmpeg, mkvmerge, dovi_tool, dovi_convert")
    
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

    def _run_dovi_convert(
        self,
        args: list,
        timeout: int = 7200
    ) -> subprocess.CompletedProcess:
        """
        Run dovi_convert CLI command.
        
        Uses cryptochrome's dovi_convert for FEL detection and conversion.
        """
        cmd = ["dovi_convert"] + args
        logger.debug(f"Running: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result
        except subprocess.TimeoutExpired:
            raise ProcessorError(f"dovi_convert timed out after {timeout}s")
        except Exception as e:
            raise ProcessorError(f"dovi_convert error: {e}")

    def _parse_dovi_convert_scan(self, output: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse dovi_convert -scan output to extract verdict and reason.
        
        Returns (verdict, reason):
        - verdict: "SAFE", "COMPLEX", "SKIP", "ERROR", or None
        - reason: Human-readable reason string
        """
        # dovi_convert outputs table format with columns:
        # Filename | Format | Action
        # For analysis: look for patterns in output
        
        output_lower = output.lower()
        
        # Check for MEL (always safe)
        if "mel" in output_lower and "safe" in output_lower:
            return ("SAFE", "MEL detected")
        
        # Check for Simple FEL
        if "simple" in output_lower or ("fel" in output_lower and "safe" in output_lower):
            return ("SAFE", "Simple FEL")
        
        # Check for Complex FEL
        if "complex" in output_lower or "skip" in output_lower:
            return ("COMPLEX", "Complex FEL detected")
        
        # Check for Profile 8 (already converted)
        if "profile 8" in output_lower or "8.1" in output_lower:
            return ("SKIP", "Already Profile 8")
        
        # Check for non-DoVi
        if "hdr10" in output_lower or "sdr" in output_lower:
            return ("SKIP", "Not Dolby Vision")
        
        return (None, "Could not parse dovi_convert output")
    
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

    def _pq_to_nits(self, code_val: int) -> int:
        """
        Convert PQ code value (0-4095) to nits using ST.2084 EOTF.
        
        Adapted from cryptochrome/dovi_convert.
        """
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
            import math
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

    def _get_bl_peak_nits(self, file_path: Path) -> Tuple[int, bool]:
        """
        Get base layer peak brightness (MaxCLL) in nits.
        
        Adapted from cryptochrome/dovi_convert.
        Returns (value, is_default) tuple for transparency.
        """
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%MaxCLL%", str(file_path)],
                capture_output=True, text=True, timeout=60
            )
            maxcll_str = result.stdout.strip()
            if maxcll_str and maxcll_str.isdigit():
                maxcll = int(maxcll_str)
                if maxcll >= 100:  # Sanity check
                    return (maxcll, False)
        except Exception as e:
            logger.debug(f"Could not get MaxCLL: {e}")

        return (1000, True)  # Default when MaxCLL unavailable

    def _get_duration_ms(self, file_path: Path) -> int:
        """
        Get video duration in milliseconds.
        
        Adapted from cryptochrome/dovi_convert.
        """
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%Duration%", str(file_path)],
                capture_output=True, text=True, timeout=60
            )
            dur_str = result.stdout.strip().split(".")[0]
            return int(dur_str) if dur_str else 0
        except Exception:
            return 0

    def _extract_l1_max(self, json_content: str) -> Optional[int]:
        """
        Extract max L1 value from RPU JSON.
        
        Adapted from cryptochrome/dovi_convert.
        """
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

    def _check_fel_complexity(self, file_path: Path) -> bool:
        """
        Analyze file to detect Complex FEL using dovi_convert.
        
        Delegates to cryptochrome's dovi_convert for detection.
        Returns True if complex (unsafe), False if simple (safe).
        """
        logger.debug(f"Running dovi_convert -scan on {file_path.name}")
        
        try:
            result = self._run_dovi_convert(["-scan", str(file_path)], timeout=300)
            
            # Log the output for debugging
            output = result.stdout + result.stderr
            logger.debug(f"dovi_convert -scan output: {output[:500]}")
            
            # Parse the result
            verdict, reason = self._parse_dovi_convert_scan(output)
            
            if verdict == "SAFE":
                logger.info(f"dovi_convert verdict: SAFE ({reason})")
                return False  # Not complex, safe to convert
            elif verdict == "COMPLEX":
                logger.info(f"dovi_convert verdict: COMPLEX ({reason})")
                return True  # Complex FEL, unsafe
            else:
                # Unknown or skip - default to complex for safety
                logger.warning(f"dovi_convert verdict unclear: {reason}")
                return True
                
        except ProcessorError as e:
            logger.warning(f"dovi_convert scan failed: {e}, assuming complex")
            return True  # Default to complex on error

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

    def _get_video_info(self, file_path: Path) -> dict:
        """
        Extract video track information from MKV file using mkvmerge.
        
        Adapted from cryptochrome/dovi_convert.
        Returns dict with track_id, delay, language, name, or None on error.
        """
        try:
            result = subprocess.run(
                ["mkvmerge", "-J", str(file_path)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                logger.warning(f"mkvmerge -J failed for {file_path.name}")
                return None

            mkv_json = json.loads(result.stdout)

            # Find first video track
            for track in mkv_json.get("tracks", []):
                if track.get("type") == "video":
                    props = track.get("properties", {})
                    return {
                        "track_id": track.get("id"),
                        "delay": props.get("minimum_timestamp", 0),
                        "language": props.get("language", "und"),
                        "name": props.get("track_name", ""),
                    }

            logger.warning(f"No video track found in {file_path.name}")
            return None

        except Exception as e:
            logger.warning(f"Video info extraction failed: {e}")
            return None

    def _get_fps(self, file_path: Path) -> Optional[str]:
        """
        Get video frame rate.
        
        Adapted from cryptochrome/dovi_convert.
        """
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%FrameRate%", str(file_path)],
                capture_output=True, text=True, timeout=60
            )
            fps = result.stdout.strip()
            return fps if fps else None
        except Exception:
            return None

    def _get_frame_count(self, file_path: Path) -> int:
        """
        Get video frame count using mediainfo (fast).
        
        Adapted from cryptochrome/dovi_convert.
        """
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=Video;%FrameCount%", str(file_path)],
                capture_output=True, text=True, timeout=60
            )
            return int(result.stdout.strip()) if result.stdout.strip() else 0
        except Exception:
            return 0

    def _get_frame_count_ffprobe(self, file_path: Path) -> int:
        """
        Get accurate frame count using ffprobe stream analysis (slow).
        Used as verification fallback when mediainfo metadata is wrong.
        
        Adapted from cryptochrome/dovi_convert.
        """
        try:
            cmd = [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-count_packets", "-show_entries", "stream=nb_read_packets",
                "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            return int(result.stdout.strip()) if result.stdout.strip() else 0
        except Exception as e:
            logger.debug(f"ffprobe frame count failed: {e}")
            return 0

    def _convert_turbo(self, input_file: Path, output_file: Path) -> Tuple[int, str]:
        """
        Pipe-based conversion (fast, no temp video file).
        
        Adapted from cryptochrome/dovi_convert.
        Returns (status, error_type): 0=success, 1=fail, error_type for classification.
        """
        logger.info("Using turbo mode (pipe-based conversion)...")

        try:
            # Create the pipe command: ffmpeg -> dovi_tool
            ffmpeg_proc = subprocess.Popen(
                ["ffmpeg", "-y", "-v", "error", "-i", str(input_file),
                 "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            dovi_cmd = ["dovi_tool", "-m", "2", "convert", "--discard", "-", "-o", str(output_file)]

            dovi_proc = subprocess.Popen(
                dovi_cmd,
                stdin=ffmpeg_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # Allow ffmpeg_proc to receive SIGPIPE
            ffmpeg_proc.stdout.close()

            _, dovi_stderr = dovi_proc.communicate(timeout=7200)  # 2 hour timeout
            _, ffmpeg_stderr = ffmpeg_proc.communicate()

            ffmpeg_status = ffmpeg_proc.returncode
            dovi_status = dovi_proc.returncode

        except subprocess.TimeoutExpired:
            logger.error("Conversion timed out after 2 hours")
            return (1, "TIMEOUT")
        except Exception as e:
            logger.error(f"Conversion exception: {e}")
            return (1, "UNKNOWN")

        # Check BOTH return codes
        if ffmpeg_status != 0:
            if output_file.exists():
                output_file.unlink()

            stderr_text = ffmpeg_stderr.decode() if ffmpeg_stderr else ""
            if any(err in stderr_text for err in ["No space left on device", "Permission denied", "Read-only file system"]):
                logger.error(f"Critical error: {stderr_text}")
                return (1, "CRITICAL")

            # Treat any ffmpeg failure as a stream error
            logger.warning(f"ffmpeg failed: {stderr_text[:200]}")
            return (1, "STREAM_ERROR")

        if dovi_status == 0:
            logger.info("Turbo mode conversion successful")
            return (0, "")

        if output_file.exists():
            output_file.unlink()

        # Error classification from dovi_tool
        stderr_text = dovi_stderr.decode() if dovi_stderr else ""

        if any(err in stderr_text for err in ["No space left on device", "Permission denied", "Read-only file system"]):
            logger.error(f"Critical error: {stderr_text}")
            return (1, "CRITICAL")

        if any(err in stderr_text for err in ["Invalid data", "Invalid NAL unit", "conversion failed", "Error splitting"]):
            logger.warning(f"Stream error: {stderr_text[:200]}")
            return (1, "STREAM_ERROR")

        logger.error(f"Unknown dovi_tool error: {stderr_text[:200]}")
        return (1, "UNKNOWN")

    def _convert_safe(self, input_file: Path, output_file: Path, video_info: dict) -> int:
        """
        Safe mode conversion using disk extraction.
        Used as fallback for problematic files (Seamless Branching, etc.)
        
        Adapted from cryptochrome/dovi_convert.
        Returns 0=success, 1=fail.
        """
        logger.info("Using safe mode (disk extraction)...")

        if not video_info or video_info.get("track_id") is None:
            logger.error("Cannot use safe mode: no video track info")
            return 1

        raw_temp = self.temp_dir / f"extract_{input_file.stem}_{os.getpid()}.hevc"

        try:
            # Step 1: Extract video track to disk
            logger.info("Extracting video track...")
            result = subprocess.run(
                ["mkvextract", str(input_file), "tracks", f"{video_info['track_id']}:{raw_temp}"],
                capture_output=True, text=True, timeout=3600
            )

            if result.returncode != 0:
                logger.error(f"mkvextract failed: {result.stderr[:200]}")
                return 1

            # Step 2: Convert with dovi_tool
            logger.info("Converting to Profile 8...")
            result = subprocess.run(
                ["dovi_tool", "-m", "2", "convert", "--discard", str(raw_temp), "-o", str(output_file)],
                capture_output=True, text=True, timeout=3600
            )

            if result.returncode != 0:
                logger.error(f"dovi_tool convert failed: {result.stderr[:200]}")
                return 1

            logger.info("Safe mode conversion successful")
            return 0

        except subprocess.TimeoutExpired:
            logger.error("Safe mode conversion timed out")
            return 1
        except Exception as e:
            logger.error(f"Safe mode conversion failed: {e}")
            return 1
        finally:
            # Clean up extracted file
            if raw_temp.exists():
                try:
                    raw_temp.unlink()
                except OSError:
                    pass

    def _verify_conversion(self, original_file: Path, converted_file: Path) -> bool:
        """
        Verify conversion by comparing frame counts.
        Uses ffprobe fallback when metadata doesn't match.
        
        Adapted from cryptochrome/dovi_convert.
        """
        frames_orig = self._get_frame_count(original_file)
        frames_new = self._get_frame_count(converted_file)

        if frames_orig and frames_orig != frames_new:
            # Metadata might be wrong - verify with ffprobe
            logger.warning(f"Frame count mismatch ({frames_orig} vs {frames_new}), verifying with ffprobe...")
            ff_orig = self._get_frame_count_ffprobe(original_file)

            if ff_orig == frames_new:
                logger.info("Verification passed (source metadata was incorrect)")
                return True
            else:
                logger.error(f"Frame verification failed: ffprobe={ff_orig} vs new={frames_new}")
                return False
        else:
            logger.info("Verification passed")
            return True

    def convert_to_profile8(self, file_path: Path, force_backup: bool = False) -> Path:
        """
        Convert a Profile 7 MKV to Profile 8 using dovi_convert.
        
        Delegates conversion to cryptochrome's dovi_convert which handles:
        - Turbo mode (pipe-based) with safe mode fallback
        - Metadata preservation (delay, language, track name)
        - Frame count verification
        
        Args:
            file_path: Path to the MKV file to convert
            force_backup: If True, keep backup regardless of backup_enabled setting

        Returns the path to the converted file.
        """
        logger.info(f"Starting Profile 7 → 8 conversion via dovi_convert: {file_path}")
        
        # Determine backup path (dovi_convert creates .mkv.bak.dovi_convert by default)
        dovi_backup = file_path.with_suffix(".mkv.bak.dovi_convert")
        our_backup = file_path.with_suffix(".mkv.original")
        
        try:
            # Run dovi_convert -convert (handles muxing, verification, backup internally)
            result = self._run_dovi_convert(["-convert", str(file_path)], timeout=7200)
            
            # Check for success
            output = result.stdout + result.stderr
            logger.debug(f"dovi_convert -convert output: {output[:1000]}")
            
            if result.returncode != 0:
                raise ProcessorError(f"dovi_convert -convert failed: {output[:500]}")
            
            # dovi_convert creates backup at .mkv.bak.dovi_convert
            # Rename to our backup format if needed
            should_backup = self.backup_enabled or force_backup
            if dovi_backup.exists():
                if should_backup:
                    # Rename to our format
                    if our_backup.exists():
                        our_backup.unlink()  # Remove old backup if exists
                    shutil.move(str(dovi_backup), str(our_backup))
                    if force_backup and not self.backup_enabled:
                        logger.info(f"Original backed up (Complex FEL safety): {our_backup}")
                    else:
                        logger.info(f"Original backed up to: {our_backup}")
                else:
                    # User doesn't want backup, delete it
                    dovi_backup.unlink()
                    logger.info("Backup deleted (backup disabled)")
            
            logger.info(f"Conversion complete: {file_path}")
            return file_path
            
        except ProcessorError:
            raise
        except Exception as e:
            logger.error(f"Conversion failed: {e}")
            raise ProcessorError(f"Conversion failed: {e}")
    
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
