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
            backup_enabled=config.backup_enabled
        )
        
        # Scan tracking
        self.last_delta_scan: Optional[datetime] = None
        self.last_full_scan_date: Optional[str] = None  # Track by date string
        
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
        # Remove from discovered files since it's now processed
        self.state.remove_discovered(str(job.file_path))
        
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
        """Run in daemon mode with scheduled filesystem scans."""
        print_banner(__version__)
        
        # Set running flag early so idle loop works
        self.running = True
        
        # Check for first-run protection - idle until setup complete
        if not self.state.is_initial_setup_complete:
            logger.warning("=" * 60)
            logger.warning("AUTO-PROCESSING DISABLED")
            logger.warning("=" * 60)
            logger.warning("")
            logger.warning("To enable automatic processing:")
            logger.warning("  Run: docker exec -it visionarr menu")
            logger.warning("")
            logger.warning("In the menu:")
            logger.warning("  1. Do a Test Scan to verify detection works")
            logger.warning("  2. Complete Required Initial Setup")
            logger.warning("")
            logger.warning("Waiting for initial setup...")
            logger.warning("=" * 60)
            
            # Idle loop - wait for setup to be completed via menu
            while self.running and not self.state.is_initial_setup_complete:
                time.sleep(30)  # Check every 30 seconds
            
            if not self.running:
                return  # Container stopped while waiting
            
            logger.info("Initial setup detected! Starting auto-processing...")
        
        # Cleanup any orphaned files
        cleaned = self.processor.cleanup_orphaned_files()
        if cleaned:
            logger.info(f"Cleaned up {cleaned} orphaned work directories")
        
        # Start queue workers
        self.queue.start()
        
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
                
                # Process any discovered files that are pending
                self._queue_pending_discoveries()
                
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            
            # Check every minute
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
        """Run a delta scan - skip already processed/discovered files."""
        for mkv_file in self._find_all_mkvs():
            if not self.running:
                break
            
            file_path_str = str(mkv_file)
            
            # Skip if already processed or discovered
            if self.state.is_processed(file_path_str):
                continue
            if self.state.is_discovered(file_path_str):
                continue
            
            try:
                analysis = self.processor.analyze_file(mkv_file)
                if analysis.needs_conversion:
                    self.state.add_discovered(file_path_str, mkv_file.stem)
                    logger.info(f"Found Profile 7: {mkv_file.name}")
            except Exception as e:
                logger.debug(f"Error analyzing {mkv_file.name}: {e}")
    
    def _run_daemon_full_scan(self) -> None:
        """Run a full scan - re-check everything including processed files."""
        for mkv_file in self._find_all_mkvs():
            if not self.running:
                break
            
            file_path_str = str(mkv_file)
            
            try:
                analysis = self.processor.analyze_file(mkv_file)
                if analysis.needs_conversion:
                    if not self.state.is_discovered(file_path_str) and not self.state.is_processed(file_path_str):
                        self.state.add_discovered(file_path_str, mkv_file.stem)
                        logger.info(f"Found Profile 7: {mkv_file.name}")
            except Exception as e:
                logger.debug(f"Error analyzing {mkv_file.name}: {e}")
    
    def _find_all_mkvs(self) -> List[Path]:
        """Find all MKV files in media directories."""
        movies_dir = Path("/movies")
        tv_dir = Path("/tv")
        
        files = []
        if movies_dir.exists():
            files.extend(movies_dir.rglob("*.mkv"))
        if tv_dir.exists():
            files.extend(tv_dir.rglob("*.mkv"))
        return files
    
    def _queue_pending_discoveries(self) -> None:
        """Queue any discovered files for conversion."""
        discovered = self.state.get_discovered()
        for item in discovered:
            file_path = Path(item['file_path'])
            if file_path.exists():
                self.queue.add_job(
                    file_path=file_path,
                    media_id=0,
                    title=item['title']
                )
    
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
        
        # Start queue workers so jobs actually process
        self.queue.start()
        
        while True:
            setup_complete = self.state.is_initial_setup_complete
            
            print("\n" + "=" * 50)
            print("         VISIONARR MANUAL MODE          ")
            print("=" * 50)
            
            if not setup_complete:
                print("⚠️  INITIAL SETUP NOT COMPLETE")
                print("   Auto-conversion is disabled until you complete setup.")
                print("-" * 50)
            
            print("  1. 🔍 Quick Scan (limited files) ⭐ Good for first run")
            print("  2. 📚 Scan Entire Library")
            print("  3. 📝 Manual Conversion (Select & Convert)")
            print("  4. 📋 View Discovered Files")
            print("  5. 📊 View Status (Live)")
            print("  6. ⚙️  Settings")
            print("  7. 🗄️  Database Management")
            print("  8. 🚪 Exit")
            
            if not setup_complete:
                print("-" * 50)
                print("  9. ✅ Complete Required Initial Setup (enable auto-mode)")
            
            print("=" * 50)
            
            choice = input("\nSelect option: ").strip()
            
            if choice == "1":
                self._manual_test_scan()
            elif choice == "2":
                self._manual_scan_library()
            elif choice == "3":
                self._manual_select_convert()
            elif choice == "4":
                self._manual_view_db()
            elif choice == "5":
                self._manual_view_status_live()
            elif choice == "6":
                self._manual_settings()
            elif choice == "7":
                self._manual_db_management()
            elif choice == "8":
                print("\nGoodbye!")
                break
            elif choice == "9" and not setup_complete:
                self._complete_initial_setup()
            else:
                print("\nInvalid option")

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

        self._scan_library_impl(limit=limit)

    def _manual_scan_library(self) -> None:
        """Scan entire library."""
        # Don't skip confirmation here - user explicitly chose "Scan Entire Library"
        self._scan_library_impl(limit=None, skip_confirmation=False)

    def _scan_library_impl(self, limit: Optional[int] = None, skip_confirmation: bool = False) -> List[Path]:
        """Implementation of library scan."""
        # Only ask for confirmation if it's a full scan and we're not skipping
        if limit is None and not skip_confirmation:
            confirm = input("\n⚠️  This will scan ALL files. Continue? (y/n): ").strip().lower()
            if confirm != "y":
                return []
        
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
                    print(f"   [{total_files} scanned | {len(profile7_files)} Profile 7] {mkv_file.name[:45]}...", end="\r")
                    
                    try:
                        analysis = self.processor.analyze_file(mkv_file)
                        if analysis.needs_conversion:
                            profile7_files.append(mkv_file)
                            # Save to DB for Manual Conversion selection
                            self.state.add_discovered(str(mkv_file), mkv_file.stem)
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
        
        print(f"\nFound {len(discovered)} Profile 7 file(s) from previous scans")
        print("-" * 55)
        
        # Pagination setup (5 files per page)
        selected = set()
        page_size = 5
        total_pages = (len(discovered) + page_size - 1) // page_size
        current_page = 0
        
        while True:
            # Display current page
            print(f"\nPage {current_page + 1}/{total_pages}:")
            start_idx = current_page * page_size
            end_idx = min(start_idx + page_size, len(discovered))
            
            for i in range(start_idx, end_idx):
                item = discovered[i]
                mark = "[x]" if i in selected else "[ ]"
                # Truncate title for display
                title = item['title'][:45] + "..." if len(item['title']) > 45 else item['title']
                print(f"  {mark} {i+1}. {title}")
            
            print("-" * 55)
            print(f"Selected: {len(selected)} file(s)")
            print("Enter numbers to toggle (e.g. '1 3'), n=next, p=prev, d=done, q=cancel")
            
            cmd = input("> ").strip().lower()
            
            if cmd == "n":
                if current_page < total_pages - 1:
                    current_page += 1
            elif cmd == "p":
                if current_page > 0:
                    current_page -= 1
            elif cmd == "d":
                if not selected:
                    print("⚠️  No files selected. Select some files or press 'q' to cancel.")
                    continue
                break
            elif cmd == "q":
                return
            else:
                # Handle space-separated numbers like "1 3" or "1,3" or single number
                try:
                    parts = cmd.replace(",", " ").split()
                    for part in parts:
                        idx = int(part) - 1
                        if 0 <= idx < len(discovered):
                            if idx in selected:
                                selected.remove(idx)
                            else:
                                selected.add(idx)
                except ValueError:
                    print("Invalid input. Enter numbers, 'n', 'p', 'd', or 'q'.")
        
        # Queue selected files
        print(f"\nQueuing {len(selected)} file(s) for conversion...")
        for idx in selected:
            item = discovered[idx]
            file_path = Path(item['file_path'])
            self.queue.add_job(
                file_path=file_path,
                media_id=0,
                title=item['title']
            )
            # Note: File is removed from discovered_files in _on_job_complete
            # to handle container restarts gracefully
        
        print(f"✅ {len(selected)} file(s) added to queue")
        print("\n  m = Return to menu")
        print("  s = View live status")
        
        choice = input("\nSelect option: ").strip().lower()
        if choice == "s":
            self._manual_view_status_live()

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
                else:
                    print("No active conversions.")
                    
                print("\nPending:")
                for job in pending[:5]:
                    print(f"  ⏳ {job.title}")
                if len(pending) > 5:
                    print(f"     ... {len(pending)-5} more")

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
        
        print(f"\nFound {len(discovered)} Profile 7 file(s) in database")
        print("-" * 55)
        
        # Pagination (5 per page)
        page_size = 5
        total_pages = (len(discovered) + page_size - 1) // page_size
        current_page = 0
        
        while True:
            print(f"\nPage {current_page + 1}/{total_pages}:")
            start_idx = current_page * page_size
            end_idx = min(start_idx + page_size, len(discovered))
            
            for i in range(start_idx, end_idx):
                item = discovered[i]
                title = item['title'][:45] + "..." if len(item['title']) > 45 else item['title']
                print(f"  {i+1}. {title}")
                print(f"      {item['file_path'][:60]}...")
            
            print("-" * 55)
            print("n=next, p=prev, q=quit")
            
            cmd = input("> ").strip().lower()
            
            if cmd == "n":
                if current_page < total_pages - 1:
                    current_page += 1
            elif cmd == "p":
                if current_page > 0:
                    current_page -= 1
            elif cmd == "q":
                return
    
    def _manual_settings(self) -> None:
        """Settings submenu to adjust scan frequencies."""
        while True:
            print("\n" + "=" * 50)
            print("           SETTINGS           ")
            print("=" * 50)
            print(f"  1. Delta Scan Interval: {self.config.delta_scan_interval_minutes} min")
            print(f"  2. Full Scan Day: {self.config.full_scan_day.capitalize()}")
            print(f"  3. Full Scan Time: {self.config.full_scan_time}")
            print("  4. ← Back")
            print("=" * 50)
            print("\nNote: Changes here apply to this session only.")
            print("For permanent changes, edit environment variables.")
            
            choice = input("\nSelect option: ").strip()
            
            if choice == "1":
                try:
                    val = input("Enter delta scan interval (minutes): ").strip()
                    minutes = int(val)
                    if 1 <= minutes <= 1440:
                        self.config.delta_scan_interval_minutes = minutes
                        print(f"✅ Delta scan interval set to {minutes} minutes")
                    else:
                        print("Must be between 1 and 1440 minutes")
                except ValueError:
                    print("Invalid number")
            elif choice == "2":
                days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                print("Days: " + ", ".join(days))
                day = input("Enter day of week: ").strip().lower()
                if day in days:
                    self.config.full_scan_day = day
                    print(f"✅ Full scan day set to {day.capitalize()}")
                else:
                    print("Invalid day")
            elif choice == "3":
                time_str = input("Enter time (HH:MM, 24h format): ").strip()
                try:
                    hour, minute = map(int, time_str.split(":"))
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        self.config.full_scan_time = time_str
                        print(f"✅ Full scan time set to {time_str}")
                    else:
                        print("Invalid time range")
                except ValueError:
                    print("Invalid format. Use HH:MM")
            elif choice == "4":
                break


    def _manual_db_management(self) -> None:
        """Database management submenu."""
        while True:
            print("\n" + "=" * 44)
            print("         DATABASE MANAGEMENT              ")
            print("=" * 44)
            print("  1. 🗑️  Clear Database (requires new scans)")
            print("  2. 📤 Export Database to JSON")
            print("  3. ← Back")
            print("=" * 44)
            
            choice = input("\nSelect option: ").strip()
            
            if choice == "1":
                print("\n⚠️  This will clear ALL database records:")
                print("   - All processed file history")
                print("   - All failed file records")
                print("   - All discovered Profile 7 files")
                print("   - Initial setup status (auto-mode disabled)")
                confirm = input("\nType 'clear' to confirm: ").strip().lower()
                if confirm == "clear":
                    count = self.state.clear_database()
                    print(f"✅ Database cleared ({count} records removed)")
                    print("   You will need to run a scan and complete setup again.")
                else:
                    print("❌ Cancelled")
            elif choice == "2":
                json_data = self.state.export_to_json()
                export_path = self.config.config_dir / "visionarr_export.json"
                export_path.write_text(json_data)
                print(f"✅ Exported to {export_path}")
            elif choice == "3":
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
