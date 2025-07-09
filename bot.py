#!/usr/bin/env python3
import os
import sys
import uuid
import asyncio
import logging
import base64
import re
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import urllib.parse

import nextcord
from nextcord.ext import commands, tasks
import yt_dlp  # ← Make sure this import is here

# ---- Configuration ----
DOWNLOAD_DIR = os.environ.get(
    'DOWNLOAD_DIR',
    '/home/masscom4/domains/musicbot.masscomputing.co.za/downloads'
)
HTTP_CONTROL_PORT = int(os.environ.get('HTTP_CONTROL_PORT', '8080'))
FILE_RETENTION_HOURS = int(os.environ.get('FILE_RETENTION_HOURS', '24'))
AUTH_USER = os.environ.get('HTTP_AUTH_USER', 'admin')
AUTH_PASS = os.environ.get('HTTP_AUTH_PASS', 'secret')

# ---- Logging ----
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s:%(name)s: %(message)s'
)
log = logging.getLogger('musicbot')

# ---- Dependency checks ----
from shutil import which
if not which('ffmpeg'):
    log.error("ffmpeg not found; install with e.g. apt install ffmpeg")
    sys.exit(1)
if not which('spotdl'):
    log.warning("spotdl not found; Spotify support will not work")

# ---- Bot setup ----
intents = nextcord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ---- Shared state ----
downloads_in_progress: set[str] = set()

# ---- Song & Player ----
class Song:
    def __init__(self, title: str, filepath: str):
        self.title = title
        self.filepath = filepath

class MusicPlayer:
    def __init__(self):
        self.queue: list[Song] = []
        self.history: list[Song] = []
        self.loop = False
        self.play_next = asyncio.Event()
        self.voice_client: nextcord.VoiceClient | None = None
        self.current: Song | None = None

    async def add_song(self, query: str) -> Song:
        if len(self.queue) >= 10:
            raise RuntimeError('Queue limit reached (10)')
        song = await download_audio(query)
        self.queue.append(song)
        return song

player = MusicPlayer()

# ---- Download logic ----
async def download_audio(query: str) -> Song:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    downloads_in_progress.add(query)
    try:
        # 1) Spotify track via spotdl subprocess
        if re.search(r'https?://(?:open\.)?spotify\.com/track/', query):
            template = f"{uuid.uuid4()}.%(ext)s"
            outfile = os.path.join(DOWNLOAD_DIR, template)
            proc = await asyncio.create_subprocess_exec(
                'spotdl', query, '--output', outfile,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await proc.communicate()

            prefix = os.path.basename(outfile).split('%')[0]
            entries = [fn for fn in os.listdir(DOWNLOAD_DIR) if fn.startswith(prefix)]
            if not entries:
                raise RuntimeError("spotdl finished but no file found")
            entry = entries[0]
            path = os.path.join(DOWNLOAD_DIR, entry)
            if os.path.isdir(path):
                # pick first audio inside
                for root, _, files in os.walk(path):
                    for f in files:
                        if f.lower().endswith(('.mp3','.m4a','.flac','.wav','.opus','.ogg')):
                            return Song(os.path.splitext(f)[0], os.path.join(root, f))
                raise RuntimeError("spotdl created directory but no audio inside")
            return Song(os.path.splitext(entry)[0], path)

        # 2) YouTube/URL via yt_dlp Python API in executor
        loop = asyncio.get_event_loop()
        def ytdlp_download():
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
                'quiet': True
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=True)
                return info
        info = await loop.run_in_executor(None, ytdlp_download)
        # info dict has 'id' and 'ext'
        file_id = info.get('id')
        ext = info.get('ext')
        title = info.get('title', 'Unknown')
        if not file_id or not ext:
            raise RuntimeError("yt_dlp did not return a valid file id/ext")
        path = os.path.join(DOWNLOAD_DIR, f"{file_id}.{ext}")
        if not os.path.isfile(path):
            raise RuntimeError("yt_dlp finished but file not found on disk")
        return Song(title, path)

    finally:
        downloads_in_progress.discard(query)

# ---- Voice helper ----
async def ensure_voice(interaction: nextcord.Interaction) -> nextcord.VoiceClient:
    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        return vc
    if not interaction.user.voice:
        raise RuntimeError("You must be in a voice channel.")
    vc = await interaction.user.voice.channel.connect()
    player.voice_client = vc
    return vc

# ---- Playback loop ----
async def playback_loop(interaction: nextcord.Interaction):
    vc = player.voice_client
    if not vc:
        return
    while True:
        player.play_next.clear()
        if not player.queue:
            await asyncio.sleep(1)
            continue

        song = player.queue.pop(0)
        player.history.append(song)
        player.current = song

        try:
            # send opus@64kbps
            src = nextcord.FFmpegOpusAudio(song.filepath, bitrate=64)
            vc.play(src, after=lambda _: player.play_next.set())
            await interaction.followup.send(f"Now playing: {song.title}")
        except Exception as e:
            log.error(f"Playback error for {song.title}: {e}")
            continue

        await player.play_next.wait()
        if not player.loop:
            try: os.remove(song.filepath)
            except: pass
        else:
            player.queue.insert(0, song)

# ---- Slash commands ----
@bot.slash_command(description='Join the voice channel')
async def join(interaction: nextcord.Interaction):
    try:
        vc = await ensure_voice(interaction)
        await interaction.response.send_message(f"Joined {vc.channel.name}")
    except Exception as e:
        await interaction.response.send_message(str(e), ephemeral=True)

@bot.slash_command(description='Leave the voice channel')
async def leave(interaction: nextcord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        await vc.disconnect()
        player.current = None
        await interaction.response.send_message("Left the voice channel")
    else:
        await interaction.response.send_message("Not in a voice channel", ephemeral=True)

@bot.slash_command(description='Play a song or playlist')
async def play(interaction: nextcord.Interaction, query: str):
    await interaction.response.defer()
    try:
        await ensure_voice(interaction)

        # convert YouTube Music playlist URL
        m = re.search(r'music\.youtube\.com/playlist\?list=([^&]+)', query)
        if m:
            query = f"https://www.youtube.com/playlist?list={m.group(1)}"

        # Spotify single track
        if re.search(r'https?://(?:open\.)?spotify\.com/track/', query):
            song = await player.add_song(query)
            await interaction.followup.send(f"Added {song.title} to queue")
            if not interaction.guild.voice_client.is_playing():
                bot.loop.create_task(playback_loop(interaction))
            return

        # YouTube playlist
        if 'list=' in query:
            with yt_dlp.YoutubeDL({'quiet':True,'extract_flat':'in_playlist'}) as ydl:
                info = ydl.extract_info(query, download=False)
            entries = info.get('entries', [])
            added = 0
            for e in entries:
                if len(player.queue) >= 10: break
                url = e.get('url') or e.get('webpage_url')
                if not url: continue
                if not url.startswith('http'):
                    url = f"https://www.youtube.com/watch?v={url}"
                try:
                    await player.add_song(url)
                    added += 1
                except RuntimeError:
                    break
            await interaction.followup.send(f"Added {added} songs from playlist")
            if not interaction.guild.voice_client.is_playing():
                bot.loop.create_task(playback_loop(interaction))
            return

        # fallback single track
        song = await player.add_song(query)
        await interaction.followup.send(f"Added {song.title} to queue")
        if not interaction.guild.voice_client.is_playing():
            bot.loop.create_task(playback_loop(interaction))

    except Exception as e:
        await interaction.followup.send(str(e), ephemeral=True)

@bot.slash_command(description='Skip the current song')
async def skip(interaction: nextcord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("Skipped")
    else:
        await interaction.response.send_message("Nothing is playing", ephemeral=True)

@bot.slash_command(description='Toggle loop mode')
async def loop(interaction: nextcord.Interaction):
    player.loop = not player.loop
    state = "on" if player.loop else "off"
    await interaction.response.send_message(f"Loop is now {state}")

@bot.slash_command(description='Show the queue')
async def queue(interaction: nextcord.Interaction):
    if not player.queue:
        return await interaction.response.send_message("Queue is empty", ephemeral=True)
    listing = "\n".join(f"{i+1}. {s.title}" for i, s in enumerate(player.queue))
    await interaction.response.send_message(f"Queue:\n{listing}")

@bot.slash_command(description='Show status')
async def status(interaction: nextcord.Interaction):
    lines = [
        f"Currently playing: {player.current.title if player.current else 'none'}",
        f"Queue length: {len(player.queue)}"
    ]
    if downloads_in_progress:
        lines.append("Downloading:")
        lines.extend(f" - {q}" for q in downloads_in_progress)
    else:
        lines.append("Downloading: none")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

# ---- Cleanup task ----
@tasks.loop(hours=1)
async def periodic_cleanup():
    removed = purge_old_files()
    if removed:
        log.info(f"Periodic cleanup removed {removed} file(s).")

def purge_old_files() -> int:
    cutoff = datetime.now() - timedelta(hours=FILE_RETENTION_HOURS)
    removed = 0
    for fname in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, fname)
        if os.path.isfile(path) and datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
            try: os.remove(path); removed += 1
            except: pass
    return removed

# ---- HTTP control with basic auth ----
async def handle_command(cmd: str):
    vc = player.voice_client
    if not vc:
        return
    if cmd == "skip" and vc.is_playing():
        vc.stop()
    elif cmd == "clear":
        for s in list(player.queue):
            try: os.remove(s.filepath)
            except: pass
        player.queue.clear()

def start_http_server():
    class AuthHandler(BaseHTTPRequestHandler):
        def do_AUTHHEAD(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="MusicBot"')
            self.send_header("Content-type", "text/plain")
            self.end_headers()
        def do_GET(self):
            auth = self.headers.get("Authorization")
            if not auth or not auth.startswith("Basic "):
                return self.do_AUTHHEAD()
            creds = base64.b64decode(auth.split(" ",1)[1]).decode()
            user,pwd = creds.split(":",1)
            if user!=AUTH_USER or pwd!=AUTH_PASS:
                return self.do_AUTHHEAD()
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/command":
                cmd = urllib.parse.parse_qs(parsed.query).get("cmd",[None])[0]
                if cmd:
                    asyncio.run_coroutine_threadsafe(handle_command(cmd), bot.loop)
                self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
            else:
                self.send_response(404); self.end_headers()

    server = HTTPServer(("0.0.0.0", HTTP_CONTROL_PORT), AuthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info(f"HTTP control (auth) on port {HTTP_CONTROL_PORT}")

# ---- Bot events ----
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    periodic_cleanup.start()

@bot.event
async def on_application_command_error(interaction, error):
    log.error(f"Error in command {interaction.command.name}: {error}")
    if isinstance(error, RuntimeError):
        await interaction.response.send_message(str(error), ephemeral=True)

# ---- Entrypoint ----
if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    start_http_server()
    bot.run('MTM5MjYyNDI5MjIredated3V6SkQwN1t3KJvgEo6fk')
