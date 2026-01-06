# Visionarr

A standalone daemon that monitors Radarr/Sonarr for new imports, detects media with Dolby Vision Profile 7, and automatically converts it to Profile 8 for better device compatibility.

## Why?

Dolby Vision Profile 7 has compatibility issues with many devices:
- **Nvidia Shield** - May strip DoVi layer entirely
- **Apple TV / Infuse** - Inconsistent behavior
- **Many TVs** - Poor or no support

Profile 8 provides the best compatibility while maintaining Dolby Vision quality.

## How It Works

```
Profile 7 MKV → Extract HEVC → Convert RPU Metadata → Remux → Profile 8 MKV
```

**No transcoding occurs.** The video stream is copied bit-for-bit. Only the Dolby Vision metadata (RPU) is modified, meaning:
- ✅ No quality loss
- ✅ Fast processing (disk I/O bound, ~5-10 min for 50GB file)
- ✅ No GPU required

## Features

- 🔄 **Daemon Mode** - Automatic polling of Radarr/Sonarr for new imports
- 🖥️ **Manual Mode** - Interactive console for one-off conversions
- 🛡️ **Atomic Safety** - Original files backed up before replacement
- 📊 **State Tracking** - SQLite database prevents reprocessing
- 🔔 **Notifications** - Optional Discord/Slack webhooks
- 🐳 **Docker Ready** - Includes Unraid template

## Requirements

- Docker (recommended) or:
  - Python 3.12+
  - ffmpeg
  - mkvtoolnix (mkvmerge)
  - dovi_tool
  - mediainfo

## Quick Start

```bash
docker run -d \
  -e RADARR_URL=http://your-radarr:7878 \
  -e RADARR_API_KEY=your-api-key \
  -e DRY_RUN=True \
  -v /path/to/media:/media \
  beltakoda/visionarr
```

## Configuration

See [.env.example](.env.example) for all available options.

## License

MIT License - See [LICENSE](LICENSE)

## Credits

- Inspired by [Unpackerr](https://github.com/Unpackerr/unpackerr)
- Powered by [dovi_tool](https://github.com/quietvoid/dovi_tool)
