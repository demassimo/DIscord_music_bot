# Discord Music Bot

This bot uses Nextcord application commands to play music from YouTube, YouTube Music, and Spotify. Songs and playlists are downloaded with `yt-dlp` and `spotdl`. Commands can also be issued over a simple HTTP endpoint instead of using WebSockets.

## Features

- `/join` – join your voice channel
- `/play <query or url>` – download a track (YouTube or Spotify) and queue it
- `/playlist <url>` – download a playlist and add tracks to the queue (queue size up to 10)
- `/remove_playlist` – remove songs added by the last playlist command
- `/skip` – skip the current song
- `/loop` – toggle loop mode for the current song
- `/back` – replay the previous song
- `/queue` – display queued tracks
- **Show Queue** context command via the Apps menu when right clicking the bot

After a song finishes playing it is removed from disk. The queue is limited to 10 entries.

## Running

1. Install dependencies:
   ```bash
   pip install nextcord yt-dlp spotdl
   ```
2. Set the `DISCORD_TOKEN` environment variable with your bot token.
3. Ensure `ffmpeg` is installed and in your PATH.
4. Run the bot:
   ```bash
   python bot.py
   ```

   A sample `discord_music_bot.service` is provided for running with `systemctl`.

The bot exposes an HTTP control server on port `8080` by default. Set `HTTP_CONTROL_PORT` to change the port.

Spotify downloads require a configured `spotdl` installation and may need Spotify credentials. See [spotdl documentation](https://github.com/spotDL/spotify-downloader) for setup details.
