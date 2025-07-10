# Discord Music Bot

This bot uses Nextcord application commands to play music from YouTube, YouTube Music, and Spotify. Songs and playlists are downloaded with `yt-dlp` and `spotdl`. Commands can also be issued over a simple HTTP endpoint. A minimal web page is served directly from the bot for basic control.

## Features

- `/join` – join your voice channel
- `/play <query or url>` – download a track (YouTube or Spotify) and queue it
- `/playlist <url>` – download a playlist and add tracks to the queue (queue size up to 10)
- `/remove_playlist` – remove songs added by the last playlist command
- `/skip` – skip the current song
- `/loop` – toggle loop mode for the current song
- `/back` – replay the previous song
- `/queue` – display queued tracks
- `/volume <0-100>` – set playback volume
- The bot speaks events like downloads and currently playing tracks using TTS
- **Show Queue** context command via the Apps menu when right clicking the bot
- Downloads are aborted if they take too long (30s for YouTube, 5m for Spotify)

Audio is streamed at the connected channel's bitrate (clamped to 384 kb/s) or
a default of 128 kb/s for higher quality.

After a song finishes playing it is removed from disk. The queue is limited to 10 entries.

## Running

1. Install dependencies:
   ```bash
   pip install nextcord yt-dlp spotdl gtts
   ```
   The bot also requires `ffmpeg` installed on the system.
2. Set the `DISCORD_TOKEN` environment variable with your bot token.
3. Ensure `ffmpeg` is installed and in your PATH.
4. Run the bot:
   ```bash
   python bot.py
   ```

   A sample `discord_music_bot.service` is provided for running with `systemctl`.

The bot exposes an HTTP control server on port `8080` by default. Browse to `http://localhost:8080/` for a small control page. Set `HTTP_CONTROL_PORT` to change the port.
The page includes a progress and volume slider for seeking and adjusting playback volume.

Spotify downloads require a configured `spotdl` installation and may need Spotify credentials. See [spotdl documentation](https://github.com/spotDL/spotify-downloader) for setup details.
