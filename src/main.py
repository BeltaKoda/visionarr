"""
Visionarr - Dolby Vision Profile Converter

Entry point for the application. Supports:
- Daemon mode: Continuous polling of Radarr/Sonarr for new imports
- Manual mode: Interactive console for one-off operations
"""

import argparse
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .banner import print_banner
from .config import Config, load_config, validate_config
from .monitor.base import BaseMonitor, ImportedMedia
from .monitor.radarr import RadarrMonitor
from .monitor.sonarr import SonarrMonitor
from .notifications import Notifier
from .processor import DoViProfile, Processor
from .queue_manager import ConversionJob, JobStatus, QueueManager
from .state import StateDB

__version__ = "1.0.0"

logger = logging.getLogger("visionarr")


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
            dry_run=config.dry_run,
            backup_enabled=config.backup_enabled
        )
        
        # Initialize monitors
        self.monitors: List[BaseMonitor] = []
        if config.has_radarr:
            self.monitors.append(RadarrMonitor(config.radarr_url, config.radarr_api_key))
        if config.has_sonarr:
            self.monitors.append(SonarrMonitor(config.sonarr_url, config.sonarr_api_key))
        
        # Initialize queue
        self.queue = QueueManager(
            process_callback=self._process_job,
            on_complete_callback=self._on_job_complete,
            on_fail_callback=self._on_job_fail,
            max_workers=config.process_concurrency
        )
        
        # Initialize notifier if webhook configured
        self.notifier: Optional[Notifier] = None
        if config.webhook_url:
            self.notifier = Notifier(config.webhook_url)
    
    def _process_job(self, job: ConversionJob) -> bool:
        """Process a single conversion job. Returns True if converted, False if skipped."""
        file_path = job.file_path
        
        # Check if already processed
        if self.state.is_processed(str(file_path)):
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
        logger.info(f"Profile 7 detected, converting: {job.title}")
        self.processor.convert_to_profile8(file_path)
        
        # Mark as processed
        self.state.mark_processed(
            str(file_path),
            original_profile="7",
            new_profile="8",
            file_size_bytes=analysis.file_size_bytes
        )
        
        return True
    
    def _on_job_complete(self, job: ConversionJob) -> None:
        """Called when a job completes successfully."""
        # Trigger rescan in appropriate monitor
        for monitor in self.monitors:
            # Try to trigger rescan (we don't know which monitor owns this file)
            try:
                monitor.trigger_rescan(job.media_id)
                break
            except Exception:
                continue
        
        # Send notification
        if self.notifier:
            self.notifier.notify_conversion_success(
                job.file_path,
                job.title,
                job.duration_seconds
            )
    
    def _on_job_fail(self, job: ConversionJob) -> None:
        """Called when a job fails after all retries."""
        self.state.mark_failed(str(job.file_path), job.error_message or "Unknown error")
        
        if self.notifier:
            self.notifier.notify_conversion_failed(
                job.file_path,
                job.title,
                job.error_message or "Unknown error"
            )
    
    def run_daemon(self) -> None:
        """Run in daemon mode with continuous polling."""
        print_banner(__version__)
        
        if self.config.dry_run:
            logger.warning("=" * 60)
            logger.warning("DRY RUN MODE - No files will be modified")
            logger.warning("=" * 60)
        
        # Check for first-run protection
        if not self.state.is_initial_setup_complete:
            logger.warning("=" * 60)
            logger.warning("FIRST RUN DETECTED")
            logger.warning("=" * 60)
            logger.warning("")
            logger.warning("For safety, automatic conversions are DISABLED on first run.")
            logger.warning("You must run manual mode first to review and confirm")
            logger.warning("the initial batch of detected Profile 7 files.")
            logger.warning("")
            logger.warning("To enable automatic mode:")
            logger.warning("  1. Run: docker exec -it visionarr python -m src.main --manual")
            logger.warning("  2. Use 'Scan Recent Imports' to review detected files")
            logger.warning("  3. Select '8. Complete Initial Setup' when ready")
            logger.warning("")
            logger.warning("Daemon will now run in DETECTION-ONLY mode...")
            logger.warning("=" * 60)
        
        # Test connections (warn but don't exit - will retry during polling)
        for monitor in self.monitors:
            if not monitor.test_connection():
                logger.warning(f"Cannot connect to {monitor.name} - will retry during polling")
        
        # Cleanup any orphaned files
        cleaned = self.processor.cleanup_orphaned_files()
        if cleaned:
            logger.info(f"Cleaned up {cleaned} orphaned work directories")
        
        # Start queue workers (only if setup complete)
        if self.state.is_initial_setup_complete:
            self.queue.start()
        
        # Send startup notification
        if self.notifier:
            self.notifier.notify_startup()
        
        # Setup signal handlers
        self.running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logger.info(f"Daemon started. Polling every {self.config.poll_interval_seconds}s")
        
        # Main polling loop
        while self.running:
            try:
                self._poll_monitors()
            except Exception as e:
                logger.error(f"Polling error: {e}")
            
            # Wait for next poll
            for _ in range(self.config.poll_interval_seconds):
                if not self.running:
                    break
                time.sleep(1)
        
        # Shutdown
        self._shutdown()
    
    def _poll_monitors(self) -> None:
        """Poll all monitors for recent imports."""
        detection_only = not self.state.is_initial_setup_complete
        
        for monitor in self.monitors:
            try:
                imports = monitor.get_recent_imports(self.config.lookback_minutes)
                
                for media in imports:
                    # Skip if already processed or queued
                    if self.state.is_processed(str(media.file_path)):
                        continue
                    
                    # In detection-only mode, just log what we find
                    if detection_only:
                        # Quick check if it's DoVi Profile 7
                        try:
                            analysis = self.processor.analyze_file(media.file_path)
                            if analysis.needs_conversion:
                                logger.info(
                                    f"[DETECTION-ONLY] Found Profile 7: {media.title} "
                                    f"- Run manual mode to convert"
                                )
                        except Exception:
                            pass  # Ignore analysis errors in detection mode
                        continue
                    
                    # Normal mode: Add to queue
                    self.queue.add_job(
                        file_path=media.file_path,
                        media_id=media.media_id,
                        title=media.title
                    )
                    
            except Exception as e:
                logger.error(f"Error polling {monitor.name}: {e}")
    
    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals."""
        logger.info("Shutdown signal received")
        self.running = False
    
    def _shutdown(self) -> None:
        """Clean shutdown."""
        logger.info("Shutting down...")
        
        # Stop queue and wait for current jobs
        self.queue.stop(wait=True)
        
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
            setup_complete = self.state.is_initial_setup_complete
            
            print("\n" + "=" * 50)
            print("         VISIONARR MANUAL MODE          ")
            print("=" * 50)
            
            if not setup_complete:
                print("⚠️  INITIAL SETUP NOT COMPLETE")
                print("   Auto-conversion is disabled until you complete setup.")
                print("-" * 50)
            
            print("  1. 🧪 Test Scan (X files) (⭐️ Recommended First)")
            print("  2. 🔍 Scan Recent Imports")
            print("  3. 📚 Scan Entire Library (⚠️ Heavy)")
            print("  4. 📝 Manual Conversion (Select & Convert)")
            print("  5. 📊 View Status (Live)")
            print("  6. 🗄️  Database Management ▶")
            print("  7. 🚪 Exit")
            
            if not setup_complete:
                print("-" * 50)
                print("  8. ✅ Complete Initial Setup (enable auto-mode)")
            
            print("=" * 50)
            
            choice = input("\nSelect option: ").strip()
            
            if choice == "1":
                self._manual_test_scan()
            elif choice == "2":
                self._manual_scan_recent()
            elif choice == "3":
                self._manual_scan_library()
            elif choice == "4":
                self._manual_select_convert()
            elif choice == "5":
                self._manual_view_status_live()
            elif choice == "6":
                self._manual_db_management()
            elif choice == "7":
                print("\nGoodbye!")
                break
            elif choice == "8" and not setup_complete:
                self._complete_initial_setup()
            else:
                print("\nInvalid option")

    def _manual_test_scan(self) -> None:
        """Test scan with user-defined limit."""
        print("\n" + "=" * 50)
        print("TEST SCAN")
        print("=" * 50)
        print("This will scan a limited number of files to verify access")
        print("and detection without scanning your entire library.")
        
        try:
            limit = int(input("\nHow many files to scan? (e.g., 50): ").strip())
        except ValueError:
            print("Invalid number.")
            return

        self._scan_library_impl(limit=limit)

    def _manual_scan_library(self) -> None:
        """Scan entire library."""
        confirm = input("\n⚠️  This will scan ALL files. Continue? (y/n): ").strip().lower()
        if confirm != "y":
            return
        self._scan_library_impl(limit=None)

    def _scan_library_impl(self, limit: Optional[int] = None) -> List[Path]:
        """Implementation of library scan."""
        print("\n" + "=" * 50)
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
        
        total_files = 0
        profile7_files = []
        errors = []
        stopped = False
        
        print("\n💡 Press Ctrl+C to stop scan and see results so far\n")
        
        try:
            for name, directory in scan_dirs:
                if stopped:
                    break
                    
                print(f"📂 Scanning {name}: {directory}")
                
                # Find all MKV files
                # Note: rglob can be slow for massive libraries, but fine for now
                mkv_files = list(directory.rglob("*.mkv"))
                print(f"   Found {len(mkv_files)} MKV files")
                
                for i, mkv_file in enumerate(mkv_files, 1):
                    if limit and total_files >= limit:
                        print(f"\n   Reached limit of {limit} files")
                        stopped = True
                        break
                    
                    total_files += 1
                    # Progress indication
                    print(f"   [{i}/{len(mkv_files)}] {mkv_file.name[:60]}...", end="\r")
                    
                    try:
                        analysis = self.processor.analyze_file(mkv_file)
                        if analysis.needs_conversion:
                            profile7_files.append(mkv_file)
                            print(f"\n   ✅ PROFILE 7: {mkv_file.name}")
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
        print(f"Total files scanned: {total_files}")
        print(f"Profile 7: {len(profile7_files)}")
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
        """Select files to convert from library scan."""
        print("\n" + "=" * 50)
        print("MANUAL CONVERSION SELECTION")
        print("=" * 50)
        print("First, we need to find Profile 7 files.")
        print("1. Scan Library (Find files)")
        print("2. Enter Path Manually")
        print("3. Back")
        
        choice = input("\nSelect option: ").strip()
        
        candidates = []
        if choice == "1":
            print("\nRunning quick scan...")
            candidates = self._scan_library_impl(limit=None) # Or maybe prompt for limit?
        elif choice == "2":
             self._manual_process_file()
             return
        else:
             return

        if not candidates:
            print("No Profile 7 files found.")
            input("Press Enter...")
            return

        # Pagination Logic
        selected = set()
        page_size = 10
        total_pages = (len(candidates) + page_size - 1) // page_size
        current_page = 0
        
        while True:
            print(f"\n--- Page {current_page + 1}/{total_pages} ---")
            start_idx = current_page * page_size
            end_idx = min(start_idx + page_size, len(candidates))
            
            for i in range(start_idx, end_idx):
                file = candidates[i]
                mark = "[x]" if i in selected else "[ ]"
                print(f"{i+1}. {mark} {file.name}")
            
            print("-" * 50)
            print("Commands:")
            print("  <number>  Toggle selection")
            print("  a         Select All (this page)")
            print("  n         Next Page")
            print("  p         Previous Page")
            print("  d         Done (Start Conversion)")
            print("  q         Quit")
            
            cmd = input("Command: ").strip().lower()
            
            if cmd == "n":
                if current_page < total_pages - 1: current_page += 1
            elif cmd == "p":
                if current_page > 0: current_page -= 1
            elif cmd == "a":
                for i in range(start_idx, end_idx): selected.add(i)
            elif cmd == "d":
                if not selected:
                    print("No files selected.")
                    continue
                break
            elif cmd == "q":
                return
            elif cmd.isdigit():
                idx = int(cmd) - 1
                if 0 <= idx < len(candidates):
                    if idx in selected: selected.remove(idx)
                    else: selected.add(idx)
            else:
                # Handle comma separated lists like 1,2,3
                try:
                    parts = [int(x.strip()) - 1 for x in cmd.split(',')]
                    for idx in parts:
                        if 0 <= idx < len(candidates):
                           if idx in selected: selected.remove(idx)
                           else: selected.add(idx) 
                except:
                    pass

        # Process selected
        print(f"\nQueuing {len(selected)} files for conversion...")
        for idx in selected:
            file = candidates[idx]
            self._process_job(ConversionJob(
                file_path=file,
                media_id=0,
                title=file.stem
            ))
        print("✅ Added to queue.")
        input("Press Enter to continue...")

    def _manual_view_status_live(self) -> None:
        """Live view of queue and processing status."""
        import time
        import os
        
        print("Starting live view... (Press Ctrl+C to exit)")
        try:
            while True:
                # Clear screen (rudimentary)
                print("\033[H\033[J", end="")
                print("=" * 50)
                print(f"VISIONARR LIVE STATUS  {time.strftime('%H:%M:%S')}")
                print("=" * 50)
                
                jobs = self.queue.get_jobs()
                active = [j for j in jobs if j.status == JobStatus.PROCESSING]
                pending = [j for j in jobs if j.status == JobStatus.PENDING]
                completed = [j for j in jobs if j.status == JobStatus.COMPLETED]
                
                print(f"Queue Stats: 🟢 {len(active)} Running | 🟡 {len(pending)} Pending | ✅ {len(completed)} Done")
                print("-" * 50)
                
                if active:
                    print("Currently Processing:")
                    for job in active:
                        print(f"  🔄 {job.title}")
                        # If we had progress per job, show it here
                else:
                    print("No active conversions.")
                    
                print("\nPending:")
                for job in pending[:5]:
                    print(f"  ⏳ {job.title}")
                if len(pending) > 5: print(f"     ... {len(pending)-5} more")

                print("-" * 50)
                print("Press Ctrl+C to return to menu")
                time.sleep(2)
        except KeyboardInterrupt:
            return

    def _manual_scan_recent(self) -> None:
        """Scan recent imports from monitors."""
        print("\nScanning recent imports...")
        
        for monitor in self.monitors:
            if not monitor.test_connection():
                print(f"  ❌ Cannot connect to {monitor.name}")
                continue
            
            imports = monitor.get_recent_imports(self.config.lookback_minutes)
            print(f"\n{monitor.name}: {len(imports)} recent imports")
            
            for media in imports:
                if self.state.is_processed(str(media.file_path)):
                    print(f"  ✓ {media.title} (already processed)")
                    continue
                
                analysis = self.processor.analyze_file(media.file_path)
                
                if analysis.needs_conversion:
                    print(f"  ⚡ {media.title} - Profile 7 DETECTED")
                    
                    confirm = input("    Convert now? (y/n): ").strip().lower()
                    if confirm == "y":
                        self._process_job(ConversionJob(
                            file_path=media.file_path,
                            media_id=media.media_id,
                            title=media.title
                        ))
                else:
                    status = "Profile 8" if analysis.has_dovi else "No DoVi"
                    print(f"  ○ {media.title} ({status})")
        input("\nPress Enter to continue...")

    def _manual_db_management(self) -> None:
        """Database management submenu."""
        while True:
            print("\n" + "=" * 44)
            print("         DATABASE MANAGEMENT              ")
            print("=" * 44)
            print("  1. Clear Single File (allow reprocess)")
            print("  2. Clear ALL Processed (full rescan)")
            print("  3. Clear Failed Files (retry all)")
            print("  4. Export Database to JSON")
            print("  5. Back")
            print("=" * 44)
            
            choice = input("\nSelect option: ").strip()
            
            if choice == "1":
                path = input("Enter file path to clear: ").strip()
                if self.state.clear_processed(path):
                    print("✅ Cleared")
                else:
                    print("❌ Not found")
            elif choice == "2":
                confirm = input("⚠️  Clear ALL processed records? (type 'yes'): ").strip()
                if confirm == "yes":
                    count = self.state.clear_all_processed()
                    print(f"✅ Cleared {count} records")
            elif choice == "3":
                count = self.state.clear_failed()
                print(f"✅ Cleared {count} failed records")
            elif choice == "4":
                json_data = self.state.export_to_json()
                export_path = self.config.config_dir / "visionarr_export.json"
                export_path.write_text(json_data)
                print(f"✅ Exported to {export_path}")
            elif choice == "5":
                break
    
    def _complete_initial_setup(self) -> None:
        """Complete initial setup to enable automatic conversion mode."""
        print("\n" + "=" * 55)
        print("         COMPLETE INITIAL SETUP         ")
        print("=" * 55)
        print("")
        print("Before enabling automatic conversions, please confirm:")
        print("")
        print("  ✓ You have reviewed detected Profile 7 files")
        print("    using 'Scan Recent Imports' or 'Test Scan' ")
        print("")
        print("  ✓ You understand that automatic mode will convert")
        print("    ALL newly imported Profile 7 files without asking")
        print("")
        print("  ✓ DRY_RUN is set appropriately for your needs")
        print(f"    (Currently: DRY_RUN={'true' if self.config.dry_run else 'FALSE - WILL MODIFY FILES'})")
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
