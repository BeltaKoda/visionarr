# Visionarr

A standalone daemon that monitors Radarr/Sonarr for new imports, detects media with Dolby Vision Profile 7, and automatically converts it to Profile 8 for better device compatibility.

## Why?

**Dolby Vision Profile 7** was designed for UHD Blu-ray discs. It uses a **dual-layer** format:
- **Base layer:** Standard HDR10 video
- **Enhancement layer:** Additional Dolby Vision metadata (MEL/FEL)

This dual-layer design works great in physical Blu-ray players, but causes problems with streaming:

| Problem | What Happens |
|---------|--------------|
| **Transcoding Triggers** | Plex/Jellyfin/Emby may not recognize client compatibility and trigger unnecessary transcoding |
| **Layer Stripping** | Some devices (Shield, Infuse) can't decode dual-layer, so they strip the DoVi layer and fall back to HDR10 |
| **Playback Failures** | Some players simply refuse to play Profile 7 content |

**Dolby Vision Profile 8** is a **single-layer** format designed for streaming. It embeds the DoVi metadata directly into the video stream, providing:
- ✅ Universal device compatibility
- ✅ No transcoding triggers
- ✅ Full Dolby Vision quality preserved

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
