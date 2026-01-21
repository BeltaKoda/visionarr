"""
Visionarr - Dolby Vision Profile Converter

Entry point for the application. Supports:
- Daemon mode: Scheduled filesystem scans for Profile 7 files
- Manual mode: Interactive console for one-off operations
"""

import argparse
import logging
import select
import signal
import sys
import termios
import time
import tty
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .banner import print_banner
from .config import Config, load_config, validate_config
from .notifications import Notifier
from .processor import DoViProfile, ELType, Processor
from .state import StateDB


__version__ = "1.0.0"

logger = logging.getLogger("visionarr")


def _getch() -> str:
    """Read a single keypress without requiring Enter (Unix/Linux only)."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def _confirm(prompt: str) -> bool:
    """Single-keypress y/n confirmation. Returns True if 'y' pressed."""
    print(prompt, end=" ", flush=True)
    ch = _getch().lower()
    print(ch)  # Echo the keypress
    return ch == "y"



def setup_logging(config: Config) -> None:
    """Configure logging based on config."""
    log_format = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    
    if config.log_file:
        log_path = Path(config.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))
    
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format=log_format,
        datefmt=date_format,
        handlers=handlers
    )


class Visionarr:
    """Main application class."""
    
    def __init__(self, config: Config):
        self.config = config
        self.running = False
        
        # Initialize components
        self.state = StateDB(config.database_path)
        self.processor = Processor(
            temp_dir=config.temp_dir,
            backup_enabled=config.backup_enabled
        )
        
        # Scan tracking
        self.last_delta_scan: Optional[datetime] = None
        self.last_full_scan_date: Optional[str] = None  # Track by date string
        
        # Conversion tracking (no queue - process directly)
        self.is_converting = False
        
        # Initialize notifier if webhook configured
        self.notifier: Optional[Notifier] = None
        if config.webhook_url:
            self.notifier = Notifier(config.webhook_url)
    
    def _convert_file(self, file_path: Path, title: str) -> bool:
        """
        Convert a single file. Returns True if converted, False if skipped.
        This replaces the queue-based processing with direct conversion.
        """
        file_path_str = str(file_path)
        
        # Mark as currently converting (for status display)
        self.state.set_current_conversion(file_path_str, title)
        self.is_converting = True
        start_time = datetime.now()
        
        try:
            # Check if already processed
            if self.state.is_processed(file_path_str):
                logger.info(f"Already processed, skipping: {file_path}")
                return False
            
            # Check if file exists
            if not file_path.exists():
                logger.warning(f"File not found, skipping: {file_path}")
                return False
            
            # Analyze file
            analysis = self.processor.analyze_file(file_path)
            
            if not analysis.needs_conversion:
                if analysis.has_dovi:
                    logger.info(f"Already Profile 8, skipping: {file_path}")
                else:
                    logger.debug(f"No DoVi content, skipping: {file_path}")
                return False
            
            # Check disk space
            if not self.processor.check_disk_space(file_path):
                raise Exception("Insufficient disk space for conversion")
            
            # Perform conversion
            logger.info(f"Profile 7 detected, converting: {title}")
            self.processor.convert_to_profile8(file_path)
            
            # Mark as processed
            el_type_str = analysis.el_type.value if analysis.el_type else "UNKNOWN"
            self.state.mark_processed(
                file_path_str,
                original_profile="7",
                new_profile="8",
                file_size_bytes=analysis.file_size_bytes,
                el_type=el_type_str
            )
            
            # Remove from discovered files
            self.state.remove_discovered(file_path_str)
            
            # Calculate duration
            duration = (datetime.now() - start_time).total_seconds()
            logger.info(f"Completed: {title} ({duration:.1f}s)")
            
            # Send success notification
            if self.notifier:
                self.notifier.notify_conversion_success(file_path, title, duration)
            
            return True
            
        except Exception as e:
            logger.error(f"Conversion failed for {title}: {e}")
            self.state.mark_failed(file_path_str, str(e))
            
            # Send failure notification
            if self.notifier:
                self.notifier.notify_conversion_failed(file_path, title, str(e))
            
            return False
            
        finally:
            # Clear current conversion status
            self.state.clear_current_conversion()
            self.is_converting = False

    
    def run_daemon(self) -> None:
        """Run in daemon mode with scheduled filesystem scans."""
        print_banner(__version__)
        
        # Set running flag early so idle loop works
        self.running = True
        
        # Check for first-run protection - idle until setup complete
        auto_mode = self.state.get_setting("auto_process_mode") or "off"
        if auto_mode == "off":
            logger.warning("=" * 60)
            logger.warning("AUTO-PROCESSING DISABLED")
            logger.warning("=" * 60)
            logger.warning("")
            logger.warning("To enable automatic processing:")
            logger.warning("  Run: docker exec -it visionarr menu")
            logger.warning("")
            logger.warning("In the menu, go to Settings and set:")
            logger.warning("  Auto Processing Mode: ALL, MOVIES, or SHOWS")
            logger.warning("")
            logger.warning("Waiting for auto-processing to be enabled...")
            logger.warning("=" * 60)
            
            # Idle loop - wait for setup to be completed via menu
            while self.running:
                auto_mode = self.state.get_setting("auto_process_mode") or "off"
                if auto_mode != "off":
                    break
                time.sleep(30)  # Check every 30 seconds
            
            if not self.running:
                return  # Container stopped while waiting
            
            logger.info(f"Auto-processing enabled! Mode: {auto_mode.upper()}")
        
        # Cleanup any orphaned files
        cleaned = self.processor.cleanup_orphaned_files()
        if cleaned:
            logger.info(f"Cleaned up {cleaned} orphaned work directories")
        
        # Send startup notification
        if self.notifier:
            self.notifier.notify_startup()
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logger.info(f"Daemon started. Delta scan every {self.config.delta_scan_interval_minutes} min, "
                    f"Full scan on {self.config.full_scan_day} at {self.config.full_scan_time}")
        
        # Main scheduler loop
        while self.running:
            try:
                now = datetime.now()
                
                # Check for full scan (weekly at configured time)
                if self._should_run_full_scan(now):
                    logger.info("Starting scheduled full library scan...")
                    self._run_daemon_full_scan()
                    self.last_full_scan_date = now.strftime("%Y-%m-%d")
                
                # Check for delta scan
                if self._should_run_delta_scan(now):
                    logger.info("Starting scheduled delta scan...")
                    self._run_daemon_delta_scan()
                    self.last_delta_scan = now
                
                # Process discovered files directly (no queue)
                # Check auto-processing before EACH file so disabling stops immediately
                self._process_next_discovered()
                
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            
            # Short sleep between processing attempts
            # If actively converting, loop immediately; otherwise wait a bit
            if not self.is_converting:
                for _ in range(60):
                    if not self.running:
                        break
                    time.sleep(1)
        
        # Shutdown
        self._shutdown()

    
    def _should_run_delta_scan(self, now: datetime) -> bool:
        """Check if it's time for a delta scan."""
        if self.last_delta_scan is None:
            return True  # First scan
        
        elapsed = (now - self.last_delta_scan).total_seconds() / 60
        return elapsed >= self.config.delta_scan_interval_minutes
    
    def _should_run_full_scan(self, now: datetime) -> bool:
        """Check if it's time for a full scan (weekly at configured time)."""
        # Check if it's the right day of week
        day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        current_day = day_names[now.weekday()]
        
        if current_day != self.config.full_scan_day.lower():
            return False
        
        # Check if we're at or past the scheduled time
        try:
            hour, minute = map(int, self.config.full_scan_time.split(":"))
            scheduled_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            scheduled_time = now.replace(hour=3, minute=0, second=0, microsecond=0)
        
        if now < scheduled_time:
            return False
        
        # Check if we've already run today
        today = now.strftime("%Y-%m-%d")
        return self.last_full_scan_date != today
    
    def _run_daemon_delta_scan(self) -> None:
        """Run a delta scan - skip already scanned files."""
        # Batch load all scanned paths for O(1) lookups
        scanned_paths = self.state.get_all_scanned_paths()
        logger.info(f"Delta scan: {len(scanned_paths)} files in scan cache")
        
        for mkv_file in self._find_all_mkvs():
            if not self.running:
                break
            
            file_path_str = str(mkv_file)
            
            # Skip if already scanned (any profile)
            if file_path_str in scanned_paths:
                continue
            
            try:
                analysis = self.processor.analyze_file(mkv_file)
                
                # Record scan result for ALL files
                profile_str = None
                if analysis.dovi_profile:
                    profile_str = str(analysis.dovi_profile.value)
                el_type_str = analysis.el_type.value if analysis.el_type else "UNKNOWN"
                self.state.add_scanned(
                    file_path_str,
                    analysis.has_dovi,
                    profile_str,
                    analysis.file_size_bytes,
                    el_type_str
                )
                
                if analysis.needs_conversion:
                    el_type_str = analysis.el_type.value if analysis.el_type else "UNKNOWN"
                    self.state.add_discovered(file_path_str, mkv_file.stem, el_type_str)
                    if analysis.el_type == ELType.FEL_COMPLEX:
                        logger.info(f"Found Profile 7 FEL [COMPLEX] (will skip auto): {mkv_file.name}")
                    elif analysis.el_type == ELType.FEL_SIMPLE:
                        logger.info(f"Found Profile 7 FEL [SIMPLE] (safe for auto): {mkv_file.name}")
                    else:
                        logger.info(f"Found Profile 7 MEL: {mkv_file.name}")
            except Exception as e:
                logger.warning(f"Error analyzing {mkv_file.name}: {e}")
    
    def _run_daemon_full_scan(self) -> None:
        """Run a full scan - re-check everything including processed files."""
        for mkv_file in self._find_all_mkvs():
            if not self.running:
                break
            
            file_path_str = str(mkv_file)
            
            try:
                analysis = self.processor.analyze_file(mkv_file)
                
                # Record/update scan result for ALL files
                profile_str = None
                if analysis.dovi_profile:
                    profile_str = str(analysis.dovi_profile.value)
                el_type_str = analysis.el_type.value if analysis.el_type else "UNKNOWN"
                self.state.add_scanned(
                    file_path_str,
                    analysis.has_dovi,
                    profile_str,
                    analysis.file_size_bytes,
                    el_type_str
                )
                
                if analysis.needs_conversion:
                    if not self.state.is_discovered(file_path_str) and not self.state.is_processed(file_path_str):
                        el_type_str = analysis.el_type.value if analysis.el_type else "UNKNOWN"
                        self.state.add_discovered(file_path_str, mkv_file.stem, el_type_str)
                        if analysis.el_type == ELType.FEL_COMPLEX:
                            logger.info(f"Found Profile 7 FEL [COMPLEX] (will skip auto): {mkv_file.name}")
                        elif analysis.el_type == ELType.FEL_SIMPLE:
                            logger.info(f"Found Profile 7 FEL [SIMPLE] (safe for auto): {mkv_file.name}")
                        else:
                            logger.info(f"Found Profile 7 MEL: {mkv_file.name}")
            except Exception as e:
                logger.warning(f"Error analyzing {mkv_file.name}: {e}")

    
    def _find_all_mkvs(self) -> List[Path]:
        """Find all MKV files in media directories based on auto_process_mode."""
        auto_mode = self.state.get_setting("auto_process_mode") or "off"
        movies_dir = Path("/movies")
        tv_dir = Path("/tv")
        
        files = []
        
        # Scan based on mode
        if auto_mode in ("all", "movies"):
            if movies_dir.exists():
                files.extend(movies_dir.rglob("*.mkv"))
        
        if auto_mode in ("all", "shows"):
            if tv_dir.exists():
                files.extend(tv_dir.rglob("*.mkv"))
        
        return files
    
    def _process_next_discovered(self) -> None:
        """
        Process the next discovered file directly (no queue).
        Checks auto-processing setting before each file so disabling stops immediately.
        """
        # Check if auto-processing is still enabled
        auto_mode = self.state.get_setting("auto_process_mode") or "off"
        if auto_mode == "off":
            return  # Auto-processing disabled, don't process anything
        
        # Check if we should process FEL files automatically
        process_fel = self.state.get_setting("auto_process_fel") == "true"
        
        # Get candidate files
        if process_fel:
            # Get all Profile 7 files (MEL and FEL)
            all_files = self.state.get_discovered()
            if not all_files:
                return
            item = all_files[0]
        else:
            # Get the next MEL file only (skip FEL for auto-processing)
            mel_files = self.state.get_mel_files()
            if not mel_files:
                # Check if we have FEL files that are being skipped
                fel_count = len(self.state.get_fel_files())
                if fel_count > 0:
                    logger.debug(f"Skipping {fel_count} FEL file(s) from auto-processing")
                return  # Nothing to process (or only FEL files)
            item = mel_files[0]
        
        # Process just ONE file, then return to main loop
        # This allows the loop to check running/auto_mode before next file
        file_path = Path(item['file_path'])
        
        if not file_path.exists():
            # File disappeared, remove from discovered
            self.state.remove_discovered(item['file_path'])
            logger.warning(f"File no longer exists, removed from queue: {item['title']}")
            return
        
        # Convert this file (blocking)
        self._convert_file(file_path, item['title'])
    
    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals."""
        logger.info("Shutdown signal received")
        self.running = False
    
    def _shutdown(self) -> None:
        """Clean shutdown."""
        logger.info("Shutting down...")
        
        # Clear any conversion in progress marker
        self.state.clear_current_conversion()
        
        # Send shutdown notification
        if self.notifier:
            self.notifier.notify_shutdown()
        
        logger.info("Shutdown complete")
    
    def run_manual(self) -> None:
        """Run in manual/interactive mode."""
        self._run_manual_mode()

    
    def _run_manual_mode(self) -> None:
        """Run interactive manual mode."""
        print_banner(__version__)
        

        while True:
            # Get auto-processing status from database
            auto_mode = self.state.get_setting("auto_process_mode") or "off"
            
            print("\n" + "=" * 50)
            print("         VISIONARR MANUAL MODE          ")
            print("=" * 50)
            
            if auto_mode == "off":
                print("⚠️  AUTO-PROCESSING: OFF")
                print("   Go to Settings to enable.")
            else:
                print(f"✅ AUTO-PROCESSING: {auto_mode.upper()}")
            print("-" * 50)
            
            print("  1. 🔍 Quick Scan (limited files) ⭐ Good for first run")
            print("  2. ⏱️  Delta Scan (New files only)")
            print("  3. 📚 Scan Entire Library")
            print("  4. 📝 Manual Conversion (Select & Convert)")
            print("  5. 📋 View Discovered Files")
            print("  6. ✅ View Processed Files")
            print("  7. 📊 View Status (Live)")
            print("  8. ⚙️  Settings")
            print("  9. 🗄️  Database Management")
            print("  0. 🚪 Exit (Return to Shell)")
            print("=" * 50)
            print("\nPress a number to select:")
            
            choice = _getch()
            if choice == "0": choice = "10" # Map 0 to 10 for easier logic
            print(choice if choice != "10" else "0")  # Echo the keypress
            
            if choice == "1":
                self._manual_test_scan()
            elif choice == "2":
                self._manual_delta_scan()
            elif choice == "3":
                self._manual_scan_library()
            elif choice == "4":
                self._manual_select_convert()
            elif choice == "5":
                self._manual_view_db()
            elif choice == "6":
                self._manual_view_processed()
            elif choice == "7":
                self._manual_view_status_live()
            elif choice == "8":
                self._manual_settings()
            elif choice == "9":
                self._manual_db_management()
            elif choice == "10":
                print("\nReturning to shell. Type 'menu' to return to this screen.")
                print("Goodbye!")
                break
            else:
                print("\nInvalid option")

    def _toggle_auto_mode(self, currently_enabled: bool) -> None:
        """Toggle auto-processing mode on/off."""
        print("\n" + "=" * 50)
        
        if currently_enabled:
            print("DISABLE AUTO-PROCESSING")
            print("=" * 50)
            print("This will stop the daemon from automatically scanning")
            print("and converting files. Manual conversion will still work.")
            print("")
            if _confirm("Disable auto-processing? (y/n):"):
                self.state.reset_initial_setup()
                print("\n🔴 Auto-processing DISABLED")
                print("   Daemon will not auto-scan until re-enabled.")
        else:
            print("ENABLE AUTO-PROCESSING")
            print("=" * 50)
            print("This will allow the daemon to automatically scan for")
            print("Profile 7 files and convert them based on schedule settings.")
            print("")
            print("⚠️  Make sure you've done a Quick Scan first to verify")
            print("   detection works correctly on your library!")
            print("")
            if _confirm("Enable auto-processing? (y/n):"):
                self.state.mark_initial_setup_complete()
                print("\n🟢 Auto-processing ENABLED")
                print("   Daemon will now auto-scan based on schedule settings.")
        
        input("\nPress Enter to continue...")

    def _manual_test_scan(self) -> None:
        """Quick scan with user-defined limit."""
        print("\n" + "=" * 50)
        print("QUICK SCAN")
        print("=" * 50)
        print("Scan a limited number of files - great for first run")
        print("to verify detection works before scanning entire library.")
        
        try:
            limit = int(input("\nHow many files to scan? (e.g., 50): ").strip())
        except ValueError:
            print("Invalid number.")
            return

        self._scan_library_impl(limit=limit, only_new=False)

    def _manual_delta_scan(self) -> None:
        """Fast scan for new files not in DB."""
        print("\n" + "=" * 50)
        print("DELTA SCAN (NEW FILES ONLY)")
        print("=" * 50)
        print("Scanning for new Profile 7 files not yet in the database.")
        print("This is usually much faster than a full scan.")
        
        self._scan_library_impl(limit=None, skip_confirmation=True, only_new=True)

    def _manual_scan_library(self) -> None:
        """Scan entire library."""
        # Don't skip confirmation here - user explicitly chose "Scan Entire Library"
        self._scan_library_impl(limit=None, skip_confirmation=False, only_new=False)

    def _scan_library_impl(self, limit: Optional[int] = None, skip_confirmation: bool = False, only_new: bool = False) -> List[Path]:
        """Implementation of library scan."""
        # Only ask for confirmation if it's a full scan and we're not skipping
        if limit is None and not skip_confirmation:
            if not _confirm("\n⚠️  This will scan ALL files. Continue? (y/n):"):
                return []

        
        print("\n" + "=" * 50)
        if only_new:
            print("DELTA SCAN (NEW FILES)")
        else:
            print(f"{'TEST' if limit else 'FULL'} LIBRARY SCAN")
        print("=" * 50)
        
        # Get directories to scan
        scan_dirs = []
        movies_dir = Path("/movies")
        tv_dir = Path("/tv")
        
        if movies_dir.exists():
            scan_dirs.append(("Movies", movies_dir))
        if tv_dir.exists():
            scan_dirs.append(("TV Shows", tv_dir))
        
        if not scan_dirs:
            print("❌ No media directories found (/movies, /tv)")
            return []
        
        # OPTIMIZATION: Batch load scanned paths into memory for O(1) lookups
        known_paths: set = set()
        if only_new:
            print("   Loading scan cache from database...")
            known_paths = self.state.get_all_scanned_paths()
            print(f"   Loaded {len(known_paths)} previously scanned files (will skip these)")
        
        total_files = 0
        skipped_files = 0
        analyzed_files = 0
        profile7_files = []
        errors = []
        stopped = False
        
        print("\n💡 Press Ctrl+C to stop scan and see results so far\n")
        
        try:
            for name, directory in scan_dirs:
                if stopped:
                    break
                    
                print(f"📂 Scanning {name}: {directory}")
                
                # OPTIMIZATION: Use generator directly instead of list() for immediate start
                for mkv_file in directory.rglob("*.mkv"):
                    if limit and total_files >= limit:
                        print(f"\n   Reached limit of {limit} files")
                        stopped = True
                        break
                    
                    total_files += 1
                    file_path_str = str(mkv_file)
                    
                    # OPTIMIZATION: Check against in-memory set instead of DB queries
                    if only_new and file_path_str in known_paths:
                        skipped_files += 1
                        # OPTIMIZATION: Throttle console output (every 100 skipped files)
                        if skipped_files % 100 == 0:
                            print(f"   [{total_files} checked | {skipped_files} skipped | {len(profile7_files)} P7]", end="\r")
                        continue
                    
                    # Progress indication for files being analyzed
                    analyzed_files += 1
                    print(f"   [{analyzed_files} analyzed | {len(profile7_files)} Profile 7] {mkv_file.name[:40]}...", end="\r")

                    try:
                        analysis = self.processor.analyze_file(mkv_file)
                        
                        # Record scan result for ALL files (Profile 7, 8, 5, or no DoVi)
                        profile_str = None
                        if analysis.dovi_profile:
                            profile_str = str(analysis.dovi_profile.value)
                        el_type_str = analysis.el_type.value if analysis.el_type else "UNKNOWN"
                        self.state.add_scanned(
                            file_path_str,
                            analysis.has_dovi,
                            profile_str,
                            analysis.file_size_bytes,
                            el_type_str
                        )
                        
                        if analysis.needs_conversion:
                            profile7_files.append(mkv_file)
                            # Save to DB for Manual Conversion selection with EL type
                            el_type_str = analysis.el_type.value if analysis.el_type else "UNKNOWN"
                            self.state.add_discovered(file_path_str, mkv_file.stem, el_type_str)
                            if analysis.el_type == ELType.FEL_COMPLEX:
                                print(f"\n   ⚠️  PROFILE 7 FEL (skip auto): {mkv_file.name}")
                            elif analysis.el_type == ELType.FEL_SIMPLE:
                                print(f"\n   ✅ PROFILE 7 FEL (Simple/Safe): {mkv_file.name}")
                            elif analysis.el_type == ELType.MEL:
                                print(f"\n   ✅ PROFILE 7 MEL: {mkv_file.name}")
                            else:
                                print(f"\n   ❔ PROFILE 7 UNKNOWN (EL detection failed): {mkv_file.name}")
                    except PermissionError:
                        errors.append(f"Permission denied: {mkv_file}")
                    except Exception as e:
                        errors.append(f"{mkv_file.name}: {str(e)[:50]}")
                        
                print() 

                
        except KeyboardInterrupt:
            print("\n\n⚠️  Scan interrupted by user")
        
        print("\n" + "=" * 50)
        print("SCAN RESULTS")
        print("=" * 50)
        print(f"Total files checked: {total_files}")
        if only_new:
            print(f"Skipped (already known): {skipped_files}")
        print(f"Profile 7 found: {len(profile7_files)}")
        if profile7_files:
            # Get breakdown from state for just this scan
            mel_count = 0
            simple_fel = 0
            complex_fel = 0
            unknown_el = 0
            
            # We can count them by re-scanning the discovered files or just tracking them during scan
            # Since we just added them to discovered_files, let's just count from our in-memory list if we had one
            # But we only store Path objects in profile7_files. Let's just do a quick DB check for these paths.
            for p in profile7_files:
                res = self.state.get_scanned_file(str(p))
                if res:
                    el = res.get('el_type')
                    if el == 'MEL': mel_count += 1
                    elif el == 'FEL_SIMPLE': simple_fel += 1
                    elif el == 'FEL_COMPLEX': complex_fel += 1
                    else: unknown_el += 1
            
            print(f"   • MEL: {mel_count}")
            print(f"   • Simple FEL (Safe): {simple_fel}")
            print(f"   • Complex FEL (Unsafe): {complex_fel}")
            if unknown_el: print(f"   • Unknown EL: {unknown_el}")
        
        print(f"Errors: {len(errors)}")
        
        # Log to Docker logs
        logger.info(f"Scan complete: {total_files} scanned, {len(profile7_files)} Profile 7 found")
        
        if profile7_files:
            print("\n📋 Profile 7 files found:")
            for i, f in enumerate(profile7_files[:20]):
                print(f"   {i+1}. {f.name}")
            if len(profile7_files) > 20:
                print(f"   ... and {len(profile7_files) - 20} more")
        
        if errors:
            print(f"\n⚠️  {len(errors)} Errors encountered (check logs for details)")
            if len(errors) <= 5:
                for e in errors: print(f"   • {e}")

        # Return found files for potential use
        if limit is None:
            input("\nPress Enter to continue...")
        return profile7_files


    def _manual_select_convert(self) -> None:
        """Select files to convert from previously discovered Profile 7 files."""
        # Get discovered files from DB (not yet processed)
        discovered = self.state.get_discovered()
        
        print("\n" + "=" * 55)
        print("       MANUAL CONVERSION - Select Files        ")
        print("=" * 55)
        
        if not discovered:
            print("\nNo Profile 7 files found in database.")
            print("\nTo discover files, run one of these first:")
            print("  1. Test Scan")
            print("  2. Scan Recent Imports")
            print("  3. Scan Entire Library")
            input("\nPress Enter to continue...")
            return
        
        # Create index mapping for selection (maintains selection across filters)
        # Key: original index in discovered list, Value: the discovered item
        selected_indices = set()  # Stores indices from original discovered list
        search_term = ""
        
        while True:
            # Apply filter
            if search_term:
                # filtered contains (original_index, item) tuples
                filtered = [(i, d) for i, d in enumerate(discovered) 
                           if search_term.lower() in d['title'].lower() or search_term.lower() in d['file_path'].lower()]
                print(f"\n🔍 Filter: '{search_term}' ({len(filtered)} matches)")
            else:
                filtered = [(i, d) for i, d in enumerate(discovered)]
                print(f"\nFound {len(discovered)} Profile 7 file(s) from previous scans")
            
            print("-" * 55)
            
            if not filtered:
                print("No files match your search.")
                print(f"Selected: {len(selected_indices)} file(s) total")
                print("-" * 55)
                print("s=search, c=clear filter, d=done (convert selected), q=cancel")
                cmd = input("> ").strip().lower()
                if cmd == "s":
                    search_term = input("Search for: ").strip()
                elif cmd == "c":
                    search_term = ""
                elif cmd == "d":
                    if not selected_indices:
                        print("⚠️  No files selected.")
                        continue
                    break
                elif cmd == "q":
                    return
                continue
            
            # Pagination setup (5 files per page)
            page_size = 5
            total_pages = (len(filtered) + page_size - 1) // page_size
            current_page = 0
            
            while True:
                # Display current page
                print(f"\nPage {current_page + 1}/{total_pages}:")
                start_idx = current_page * page_size
                end_idx = min(start_idx + page_size, len(filtered))
                
                for display_num, (orig_idx, item) in enumerate(filtered[start_idx:end_idx], start=start_idx + 1):
                    mark = "[x]" if orig_idx in selected_indices else "[ ]"
                    el_type = item.get('el_type', 'UNK')
                    if el_type == 'FEL_COMPLEX':
                        el_tag = "[FEL ⚠️]"
                    elif el_type == 'FEL_SIMPLE':
                        el_tag = "[FEL ✅]"
                    else:
                        el_tag = f"[{el_type}]"
                    title = item['title'][:42] + "..." if len(item['title']) > 42 else item['title']
                    print(f"  {mark} {display_num}. {el_tag} {title}")
                
                print("-" * 55)
                print(f"Selected: {len(selected_indices)} file(s)")
                print("💡 FEL = Full Enhancement Layer (lossy conversion)")
                print("   MEL = Minimal Enhancement Layer (safe conversion)")
                filter_hint = f" | filter:'{search_term}'" if search_term else ""
                print(f"[1-{len(filtered)}]=toggle, n/p=page, s=search, d=done, q=quit{filter_hint}")
                
                cmd = input("> ").strip().lower()
                
                if cmd == "n":
                    if current_page < total_pages - 1:
                        current_page += 1
                elif cmd == "p":
                    if current_page > 0:
                        current_page -= 1
                elif cmd == "s":
                    search_term = input("Search for: ").strip()
                    break  # Re-filter
                elif cmd == "c":
                    search_term = ""
                    break  # Clear filter
                elif cmd == "d":
                    if not selected_indices:
                        print("⚠️  No files selected. Select some files or press 'q' to cancel.")
                        continue
                    break
                elif cmd == "q":
                    return
                elif cmd == "a":
                    # Select all in current filter
                    for orig_idx, _ in filtered:
                        selected_indices.add(orig_idx)
                else:
                    # Handle number selection (using display numbers from filtered list)
                    try:
                        parts = cmd.replace(",", " ").split()
                        for part in parts:
                            display_idx = int(part) - 1
                            if 0 <= display_idx < len(filtered):
                                orig_idx = filtered[display_idx][0]
                                if orig_idx in selected_indices:
                                    selected_indices.remove(orig_idx)
                                else:
                                    selected_indices.add(orig_idx)
                    except ValueError:
                        print("Invalid input.")
            
            # Check if we should exit the outer loop (done was pressed)
            if cmd == "d" and selected_indices:
                break
        
        # Process selected files directly (no queue)
        print(f"\nConverting {len(selected_indices)} file(s)...")
        print("-" * 55)
        
        converted = 0
        failed = 0
        for i, idx in enumerate(selected_indices, 1):
            item = discovered[idx]
            file_path = Path(item['file_path'])
            print(f"\n[{i}/{len(selected_indices)}] {item['title'][:45]}...")
            
            try:
                if self._convert_file(file_path, item['title']):
                    converted += 1
                    print(f"   ✅ Converted successfully")
                else:
                    print(f"   ⏭️  Skipped (already converted or no DoVi)")
            except Exception as e:
                failed += 1
                print(f"   ❌ Failed: {str(e)[:40]}")
        
        print("\n" + "=" * 55)
        print(f"CONVERSION COMPLETE: {converted} converted, {failed} failed")
        print("=" * 55)
        input("\nPress Enter to continue...")



    def _manual_view_status_live(self) -> None:
        """Live view of queue and processing status."""
        print("Starting live view... (Press 'q' to exit)")
        
        # Save terminal settings
        old_settings = termios.tcgetattr(sys.stdin)
        
        try:
            # Set terminal to cbreak mode for single-char input
            tty.setcbreak(sys.stdin.fileno())
            
            while True:
                # Clear screen
                print("\033[H\033[J", end="")
                print("=" * 50)
                print(f"VISIONARR LIVE STATUS  {time.strftime('%H:%M:%S')}")
                print("=" * 50)
                
                # Get current conversion from DB
                current = self.state.get_current_conversion()
                discovered = self.state.get_discovered()
                processed_count = len(self.state.get_processed_files(limit=10000))
                
                print(f"📊 Stats: {processed_count} converted | {len(discovered)} pending")
                print("-" * 50)
                
                if current:
                    print("Currently Processing:")
                    print(f"  🔄 {current['title']}")
                else:
                    print("No active conversions.")
                    
                print("\nPending Files:")
                if discovered:
                    for item in discovered[:5]:
                        title = item['title'][:40] + "..." if len(item['title']) > 40 else item['title']
                        print(f"  ⏳ {title}")
                    if len(discovered) > 5:
                        print(f"     ... {len(discovered)-5} more")
                else:
                    print("  (none)")

                print("-" * 50)
                print("Press 'q' to return to menu")
                
                # Check for 'q' key with timeout (refresh every 2 seconds)
                for _ in range(20):  # 20 x 0.1s = 2s
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        ch = sys.stdin.read(1)
                        if ch.lower() == 'q':
                            return
        finally:
            # Restore terminal settings
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


    def _manual_view_db(self) -> None:
        """View discovered Profile 7 files in database with pagination."""
        discovered = self.state.get_discovered()
        
        print("\n" + "=" * 55)
        print("       VIEW DISCOVERED FILES        ")
        print("=" * 55)
        
        if not discovered:
            print("\nNo Profile 7 files in database.")
            print("Run a scan to discover files needing conversion.")
            input("\nPress Enter to continue...")
            return
        
        # Search/filter state
        search_term = ""
        filtered = discovered
        
        while True:
            # Apply filter if search term is set
            if search_term:
                filtered = [d for d in discovered if search_term.lower() in d['title'].lower() or search_term.lower() in d['file_path'].lower()]
                print(f"\n🔍 Filter: '{search_term}' ({len(filtered)} matches)")
            else:
                filtered = discovered
                print(f"\nFound {len(filtered)} Profile 7 file(s) in database")
            
            print("-" * 55)
            
            if not filtered:
                print("No files match your search.")
                print("-" * 55)
                print("s=search, c=clear filter, q=quit")
                cmd = input("> ").strip().lower()
                if cmd == "s":
                    search_term = input("Search for: ").strip()
                elif cmd == "c":
                    search_term = ""
                elif cmd == "q":
                    return
                continue
            
            # Pagination (5 per page)
            page_size = 5
            total_pages = (len(filtered) + page_size - 1) // page_size
            current_page = 0
            
            while True:
                print(f"\nPage {current_page + 1}/{total_pages}:")
                start_idx = current_page * page_size
                end_idx = min(start_idx + page_size, len(filtered))
                
                for i in range(start_idx, end_idx):
                    item = filtered[i]
                    el_type = item.get('el_type', 'UNK')
                    if el_type == 'FEL_COMPLEX':
                        el_tag = "[FEL ⚠️]"
                    elif el_type == 'FEL_SIMPLE':
                        el_tag = "[FEL ✅]"
                    elif el_type == 'MEL':
                        el_tag = "[MEL]"
                    else:
                        el_tag = f"[{el_type}]"
                    title = item['title'][:45] + "..." if len(item['title']) > 45 else item['title']
                    print(f"  {i+1}. {el_tag} {title}")
                    print(f"      {item['file_path'][:60]}...")
                
                print("-" * 55)
                filter_hint = f" | filter:'{search_term}'" if search_term else ""
                print(f"n=next, p=prev, s=search, c=clear, q=quit{filter_hint}")
                
                cmd = input("> ").strip().lower()
                
                if cmd == "n":
                    if current_page < total_pages - 1:
                        current_page += 1
                elif cmd == "p":
                    if current_page > 0:
                        current_page -= 1
                elif cmd == "s":
                    search_term = input("Search for: ").strip()
                    break  # Re-filter and restart pagination
                elif cmd == "c":
                    search_term = ""
                    break  # Clear filter and restart
                elif cmd == "q":
                    return


    def _manual_view_processed(self) -> None:
        """View successfully converted files in database with pagination."""
        processed = self.state.get_processed_files()
        
        print("\n" + "=" * 55)
        print("       VIEW CONVERTED FILES          ")
        print("=" * 55)
        
        if not processed:
            print("\nNo files have been converted yet.")
            print("Completed conversions will appear here.")
            input("\nPress Enter to continue...")
            return
        
        print(f"\nFound {len(processed)} converted file(s) in database")
        print("-" * 55)
        
        # Pagination (5 per page)
        page_size = 5
        total_pages = (len(processed) + page_size - 1) // page_size
        current_page = 0
        
        while True:
            print(f"\nPage {current_page + 1}/{total_pages}:")
            start_idx = current_page * page_size
            end_idx = min(start_idx + page_size, len(processed))
            
            for i in range(start_idx, end_idx):
                item = processed[i]
                file_path = Path(item.file_path)
                title = file_path.name
                if len(title) > 45:
                    title = title[:42] + "..."
                
                # Check for backup file
                backup_path = file_path.with_suffix(".mkv.original")
                backup_status = "YES" if backup_path.exists() else "NO"
                
                processed_at = item.processed_at.strftime("%Y-%m-%d %H:%M")
                if item.el_type == 'FEL_COMPLEX':
                    el_tag = "[FEL ⚠️]"
                elif item.el_type == 'FEL_SIMPLE':
                    el_tag = "[FEL ✅]"
                else:
                    el_tag = f"[{item.el_type}]"
                print(f"  {i+1}. {el_tag} {title}")
                print(f"      Profile: {item.original_profile} -> {item.new_profile} | {processed_at} | Backup: {backup_status}")
            
            print("-" * 55)
            print("n=next, p=prev, c=cleanup backups, q=quit")
            
            cmd = input("> ").strip().lower()
            
            if cmd == "n":
                if current_page < total_pages - 1:
                    current_page += 1
            elif cmd == "p":
                if current_page > 0:
                    current_page -= 1
            elif cmd == "c":
                self._manual_cleanup_backups()
                # Refresh list after cleanup
                processed = self.state.get_processed_files()
                if not processed:
                    return
                total_pages = (len(processed) + page_size - 1) // page_size
                current_page = min(current_page, total_pages - 1)
            elif cmd == "q":
                return

    def _manual_cleanup_backups(self) -> None:
        """Bulk cleanup of .mkv.original backup files."""
        processed = self.state.get_processed_files(limit=1000)
        backups_found = []
        total_size = 0
        
        print("\n" + "=" * 50)
        print("        BACKUP CLEANUP          ")
        print("=" * 50)
        print("Scanning for existing .mkv.original files...")
        
        for item in processed:
            backup_path = Path(item.file_path).with_suffix(".mkv.original")
            if backup_path.exists():
                backups_found.append(backup_path)
                total_size += backup_path.stat().st_size
        
        if not backups_found:
            print("\nNo original backup files found.")
            input("\nPress Enter to continue...")
            return
        
        size_gb = total_size / (1024**3)
        print(f"\nFound {len(backups_found)} backup file(s)")
        print(f"Total space to reclaim: {size_gb:.2f} GB")
        print("\n⚠️  WARNING: This will permanently delete original files.")
        print("   Make sure your converted files are working correctly!")
        
        if not _confirm("\nDelete all original backups? (y/n):"):
            print("Cleanup cancelled.")
            return

            
        print("\nDeleting backups...")
        deleted_count = 0
        for path in backups_found:
            try:
                path.unlink()
                deleted_count += 1
                if deleted_count % 5 == 0:
                    print(f"  Progress: {deleted_count}/{len(backups_found)}...")
            except Exception as e:
                logger.error(f"Failed to delete {path}: {e}")
                
        print(f"\n✅ Cleaned up {deleted_count} backup files.")
        print(f"   Reclaimed {size_gb:.2f} GB of space.")
        input("\nPress Enter to continue...")
    
    def _manual_settings(self) -> None:
        """Settings submenu with persistent database-backed settings."""
        while True:
            # Get current settings from database
            settings = self.state.get_all_settings()
            auto_mode = settings.get("auto_process_mode", "off").upper()
            backup_enabled = settings.get("backup_enabled", "true") == "true"
            backup_status = "ON ⚠️" if backup_enabled else "OFF"
            delta_interval = settings.get("delta_scan_interval", "30")
            full_day = settings.get("full_scan_day", "sunday").capitalize()
            full_time = settings.get("full_scan_time", "03:00")
            fel_auto = settings.get("auto_process_fel", "false") == "true"
            fel_status = "ON (Auto)" if fel_auto else "OFF (Manual Only)"
            
            print("\n" + "=" * 50)
            print("           SETTINGS           ")
            print("=" * 50)
            print(f"  1. Change Auto Processing Mode [Currently: {auto_mode}]")
            print(f"  2. Backup Originals: {backup_status}")
            print(f"  3. Delta Scan Interval: {delta_interval} min")
            print(f"  4. Full Scan Day: {full_day}")
            print(f"  5. Full Scan Time: {full_time}")
            print(f"  6. Toggle FEL Auto-Processing: {fel_status}")
            print("  7. ← Back")
            print("=" * 50)
            print("\nPress a number to select:")
            
            choice = _getch()
            print(choice)  # Echo the keypress
            
            if choice == "1":
                self._change_auto_process_mode()
            elif choice == "2":
                self._toggle_backup_setting()
            elif choice == "3":
                try:
                    val = input("Enter delta scan interval (minutes): ").strip()
                    minutes = int(val)
                    if 1 <= minutes <= 1440:
                        self.state.set_setting("delta_scan_interval", str(minutes))
                        print(f"✅ Delta scan interval set to {minutes} minutes")
                    else:
                        print("Must be between 1 and 1440 minutes")
                except ValueError:
                    print("Invalid number")
            elif choice == "4":
                days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                print("Days: " + ", ".join(days))
                day = input("Enter day of week: ").strip().lower()
                if day in days:
                    self.state.set_setting("full_scan_day", day)
                    print(f"✅ Full scan day set to {day.capitalize()}")
                else:
                    print("Invalid day")
            elif choice == "5":
                time_str = input("Enter time (HH:MM, 24h format): ").strip()
                try:
                    hour, minute = map(int, time_str.split(":"))
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        self.state.set_setting("full_scan_time", time_str)
                        print(f"✅ Full scan time set to {time_str}")
                    else:
                        print("Invalid time range")
                except ValueError:
                    print("Invalid format. Use HH:MM")
            elif choice == "6":
                new_val = "false" if fel_auto else "true"
                self.state.set_setting("auto_process_fel", new_val)
                print(f"\n✅ Profile 7 FEL Auto-Processing set to: {'ENABLED' if new_val == 'true' else 'DISABLED'}")
                if new_val == "true":
                    print("   ⚠️  Note: FEL files will now be auto-converted to Profile 8 (Lossy).")
                input("\nPress Enter to continue...")
            elif choice == "7":
                break
    
    def _change_auto_process_mode(self) -> None:
        """Submenu to change auto processing mode."""
        current_mode = self.state.get_setting("auto_process_mode") or "off"
        
        print("\n" + "=" * 50)
        print("     AUTO PROCESSING MODE     ")
        print("=" * 50)
        print(f"  Current: {current_mode.upper()}")
        print("")
        print("  1. OFF    - Disable auto-processing")
        print("  2. ALL    - Process movies AND shows")
        print("  3. MOVIES - Process only /movies")
        print("  4. SHOWS  - Process only /tv")
        print("  5. ← Back (no change)")
        print("=" * 50)
        print("\nPress a number to select:")
        
        choice = _getch()
        print(choice)  # Echo the keypress
        
        mode_map = {
            "1": "off",
            "2": "all",
            "3": "movies",
            "4": "shows"
        }
        
        if choice in mode_map:
            new_mode = mode_map[choice]
            self.state.set_setting("auto_process_mode", new_mode)
            print(f"\n✅ Auto processing mode set to: {new_mode.upper()}")
            input("\nPress Enter to continue...")
        elif choice == "5":
            return  # Back, no change

    def _toggle_backup_setting(self) -> None:
        """Toggle backup of original files with storage warning."""
        current = self.state.get_setting("backup_enabled") or "true"
        backup_enabled = current == "true"
        
        print("\n" + "=" * 50)
        print("BACKUP ORIGINAL FILES")
        print("=" * 50)
        
        if backup_enabled:
            print("Currently: ENABLED")
            print("")
            print("When enabled, original files are renamed to .mkv.original")
            print("after conversion. This preserves the original in case of issues.")
            print("")
            print("Disabling this will DELETE original files after conversion.")
            print("")
            if _confirm("Disable backups? (y/n):"):
                self.state.set_setting("backup_enabled", "false")
                self.processor.backup_enabled = False
                print("\n🔴 Backups DISABLED - originals will be deleted after conversion")
        else:
            print("Currently: DISABLED")
            print("")
            print("⚠️  WARNING: Enabling backups will DOUBLE your storage requirements!")
            print("")
            print("Example: A 50GB movie will use 100GB total (original + converted)")
            print("")
            print("This is recommended for safety but may not be practical for")
            print("large libraries with limited storage.")
            print("")
            if _confirm("Enable backups? (y/n):"):
                self.state.set_setting("backup_enabled", "true")
                self.processor.backup_enabled = True
                print("\n🟢 Backups ENABLED - originals kept as .mkv.original")
        
        input("\nPress Enter to continue...")


    def _manual_db_management(self) -> None:
        """Database management submenu."""
        while True:
            # Get scan cache stats
            scan_stats = self.state.get_scanned_stats()
            
            print("\n" + "=" * 50)
            print("         DATABASE MANAGEMENT              ")
            print("=" * 50)
            print(f"   Scan Cache: {scan_stats['total']} files")
            print(f"   ({scan_stats['profile_7']} P7 | {scan_stats['profile_8']} P8 | {scan_stats['no_dovi']} no DoVi)")
            print("-" * 50)
            print("  1. 🔄 Clear Scan Cache (force rescan)")
            print("  2. 🗑️  Clear Entire Database")
            print("  3. 📤 Export Database to JSON")
            print("  4. ← Back")
            print("=" * 50)
            
            choice = input("\nSelect option: ").strip()
            
            if choice == "1":
                print("\n⚠️  This will clear the scan cache only.")
                print("   Next Delta Scan will re-analyze ALL files.")
                print("   (Processed/converted files are NOT affected)")
                if _confirm("\nClear scan cache? (y/n):"):

                    count = self.state.clear_scanned()
                    print(f"✅ Scan cache cleared ({count} records removed)")
                else:
                    print("❌ Cancelled")
            elif choice == "2":
                print("\n⚠️  This will clear ALL database records:")
                print("   - All processed file history")
                print("   - All failed file records")
                print("   - All discovered Profile 7 files")
                print("   - All scan cache records")
                print("   - Initial setup status (auto-mode disabled)")
                confirm = input("\nType 'clear' to confirm: ").strip().lower()
                if confirm == "clear":
                    count = self.state.clear_database()
                    self.state.clear_scanned()
                    print(f"✅ Database cleared ({count} records removed)")
                    print("   You will need to run a scan and complete setup again.")
                else:
                    print("❌ Cancelled")
            elif choice == "3":
                json_data = self.state.export_to_json()
                export_path = self.config.config_dir / "visionarr_export.json"
                export_path.write_text(json_data)
                print(f"✅ Exported to {export_path}")
            elif choice == "4":
                break

    
    def _complete_initial_setup(self) -> None:
        """Complete initial setup to enable automatic conversion mode."""
        print("\n" + "=" * 55)
        print("      COMPLETE REQUIRED INITIAL SETUP      ")
        print("=" * 55)
        print("")
        print("Before enabling automatic conversions, please confirm:")
        print("")
        print("  ✓ You have tested scanning with 'Test Scan'")
        print("    and verified Profile 7 detection works")
        print("")
        print("  ✓ You understand that automatic mode will convert")
        print("    ALL newly imported Profile 7 files without asking")
        print("")
        print("  ⚠️  This WILL modify your media files!")
        print("")
        print("=" * 55)
        
        confirm = input("\nType 'enable' to complete setup and enable auto-mode: ").strip().lower()
        
        if confirm == "enable":
            self.state.mark_initial_setup_complete()
            print("\n✅ Initial setup complete!")
            print("   Automatic conversion mode is now ENABLED.")
            print("   Restart the container in daemon mode to begin.")
        else:
            print("\n❌ Setup not completed. Auto-mode remains disabled.")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Visionarr - Dolby Vision Profile Converter"
    )
    parser.add_argument(
        "--manual", "-m",
        action="store_true",
        help="Run in manual/interactive mode"
    )
    parser.add_argument(
        "--version", "-v",
        action="version",
        version=f"Visionarr {__version__}"
    )
    
    args = parser.parse_args()
    
    # Load configuration
    config = load_config()
    
    # Override manual mode from args
    if args.manual:
        config.manual_mode = True
    
    # Setup logging
    setup_logging(config)
    
    # Validate configuration
    if not validate_config(config):
        sys.exit(1)
    
    # Create and run application
    app = Visionarr(config)
    
    if config.manual_mode:
        app.run_manual()
    else:
        app.run_daemon()


if __name__ == "__main__":
    main()
