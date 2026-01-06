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
        
        # Test connections
        for monitor in self.monitors:
            if not monitor.test_connection():
                logger.error(f"Failed to connect to {monitor.name}")
                sys.exit(1)
        
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
        print_banner(__version__)
        
        if self.config.dry_run:
            print("\n⚠️  DRY RUN MODE - No files will be modified\n")
        
        while True:
            # Check setup status
            setup_complete = self.state.is_initial_setup_complete
            
            print("\n" + "=" * 50)
            print("             VISIONARR MANUAL MODE             ")
            print("=" * 50)
            
            if not setup_complete:
                print("  ⚠️  INITIAL SETUP NOT COMPLETE")
                print("  Auto-conversion is disabled until you complete setup.")
                print("-" * 50)
            
            print("  1. Scan Recent Imports")
            print("  2. Scan Entire Library (⚠️  Heavy)")
            print("  3. Process Single File")
            print("  4. View Processing Queue")
            print("  5. View Processed History")
            print("  6. Database Management ▶")
            print("  7. Exit")
            
            if not setup_complete:
                print("-" * 50)
                print("  8. ✅ Complete Initial Setup (enable auto-mode)")
            
            print("=" * 50)
            
            choice = input("\nSelect option: ").strip()
            
            if choice == "1":
                self._manual_scan_recent()
            elif choice == "2":
                self._manual_scan_library()
            elif choice == "3":
                self._manual_process_file()
            elif choice == "4":
                self._manual_view_queue()
            elif choice == "5":
                self._manual_view_history()
            elif choice == "6":
                self._manual_db_management()
            elif choice == "7":
                print("\nGoodbye!")
                break
            elif choice == "8" and not setup_complete:
                self._complete_initial_setup()
            else:
                print("\nInvalid option")
    
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
    
    def _manual_scan_library(self) -> None:
        """Scan entire library - heavy operation."""
        confirm = input("\n⚠️  This can take a LONG time. Continue? (y/n): ").strip().lower()
        if confirm != "y":
            return
        
        print("\nScanning library...")
        # Implementation would iterate through all files
        print("(Full library scan not yet implemented)")
    
    def _manual_process_file(self) -> None:
        """Process a single file by path."""
        path_str = input("\nEnter file path: ").strip()
        if not path_str:
            return
        
        file_path = Path(path_str)
        if not file_path.exists():
            print(f"❌ File not found: {file_path}")
            return
        
        print(f"\nAnalyzing: {file_path.name}")
        analysis = self.processor.analyze_file(file_path)
        
        print(f"  Has DoVi: {analysis.has_dovi}")
        print(f"  Profile: {analysis.dovi_profile}")
        print(f"  Needs conversion: {analysis.needs_conversion}")
        
        if analysis.needs_conversion:
            confirm = input("\nConvert now? (y/n): ").strip().lower()
            if confirm == "y":
                self._process_job(ConversionJob(
                    file_path=file_path,
                    media_id=0,
                    title=file_path.stem
                ))
    
    def _manual_view_queue(self) -> None:
        """View current queue status."""
        jobs = self.queue.get_jobs()
        print(f"\nQueue: {len(jobs)} jobs")
        
        for job in jobs[-10:]:  # Last 10
            status_icon = {
                JobStatus.PENDING: "⏳",
                JobStatus.PROCESSING: "🔄",
                JobStatus.COMPLETED: "✅",
                JobStatus.FAILED: "❌",
                JobStatus.SKIPPED: "⏭️"
            }.get(job.status, "?")
            
            print(f"  {status_icon} {job.title} [{job.status.value}]")
    
    def _manual_view_history(self) -> None:
        """View processed file history."""
        files = self.state.get_processed_files(limit=20)
        stats = self.state.get_stats()
        
        print(f"\n📊 Stats: {stats['processed_count']} processed, {stats['failed_count']} failed")
        print(f"   Total: {stats['total_bytes_processed'] / 1e9:.1f} GB\n")
        
        for f in files[:10]:
            print(f"  ✅ {Path(f.file_path).name}")
            print(f"     {f.original_profile} → {f.new_profile} | {f.processed_at}")
    
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
        print("    using 'Scan Recent Imports' (option 1)")
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
