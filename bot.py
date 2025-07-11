#!/usr/bin/env python3
import os
import sys
import uuid
import asyncio
import logging
import base64
import re
import json
import subprocess
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import urllib.parse

import nextcord
from nextcord.ext import commands, tasks
import yt_dlp
from gtts import gTTS
from shutil import which
import ctypes.util, nextcord, sys


lib = ctypes.util.find_library("opus")           # asks ldconfig where libopus lives
if not lib:                                      # None  package not installed
    sys.stderr.write(
        "libopus not found. sudo apt-get install libopus0 (Debian/Ubuntu) "
        "or sudo dnf install opus (Fedora/RHEL)\n"
    )
    sys.exit(1)

nextcord.opus.load_opus(lib)
if nextcord.opus.is_loaded():
    nextcord.opus.set_opus_application(nextcord.opus.APPLICATION_AUDIO)
reconnect = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

# ---- Configuration ----
DOWNLOAD_DIR = os.environ.get(
    'DOWNLOAD_DIR',
    '/home/masscom4/domains/musicbot.masscomputing.co.za/downloads'
)
DEBUG_ARCHIVE = True           #  flip to False to disable all copying

# where we keep the permanent copies
if DEBUG_ARCHIVE:
    ARCHIVE_ROOT = os.path.join(DOWNLOAD_DIR, "_archive")
    RAW_DIR      = os.path.join(ARCHIVE_ROOT, "raw")   # bit-perfect from yt-dl/spotDL
    ENC_DIR      = os.path.join(ARCHIVE_ROOT, "enc")   # single ffmpeg encode
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(ENC_DIR, exist_ok=True)
# -------------------------------------------------------------------

HTTP_CONTROL_PORT = int(os.environ.get('HTTP_CONTROL_PORT', '8080'))
FILE_RETENTION_HOURS = int(os.environ.get('FILE_RETENTION_HOURS', '24'))
AUTH_USER = os.environ.get('HTTP_AUTH_USER', 'admin')
AUTH_PASS = os.environ.get('HTTP_AUTH_PASS', 'secret')

# ---- Logging ----
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)s:%(name)s: %(message)s'
)
log = logging.getLogger('musicbot')

# ---- Dependency checks ----
if not which('ffmpeg'):
    log.error("ffmpeg not found; install it (e.g. apt install ffmpeg)")
    sys.exit(1)
if not which('spotdl'):
    log.warning("spotdl not found; Spotify support will not work")

# ---- Bot setup ----
intents = nextcord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ---- Shared state ----
downloads_in_progress: dict[str, datetime] = {}
last_channel_id: int | None = None
playback_task: asyncio.Task | None = None
last_playlist_files: set[str] = set()

# ---- Song & Player ----
class Song:
    def __init__(self, title: str, filepath: str, query: str, duration: float = 0.0):
        self.title = title
        self.filepath = filepath
        self.query = query
        self.duration = duration  # seconds

class MusicPlayer:
    def __init__(self):
        self.queue: list[Song] = []
        self.history: list[Song] = []
        self.loop = False
        self.loop_queue = False
        self.play_next = asyncio.Event()
        self.voice_client: nextcord.VoiceClient | None = None
        self.current: Song | None = None
        self.start_time: float = 0.0
        self.seek_pos: float | None = None
        self.volume: float = 1.0
        self.lock = asyncio.Lock()
        self.paused_pos: float | None = None

    async def add_song(self, query: str) -> Song:
        if len(self.queue) >= 10:
            raise RuntimeError('Queue limit reached (10)')
        song = await download_audio(query)
        self.queue.append(song)
        return song

async def add_and_play(query: str):
    """Queue a song and start playback if idle."""
    song = await player.add_song(query)
    vc = player.voice_client
    if not vc or not vc.is_connected():
        if last_channel_id is not None:
            await join_channel(last_channel_id)
            vc = player.voice_client
        else:
            channels = list_voice_channels()
            if channels:
                first_id = next(iter(channels.keys()))
                await join_channel(first_id)
                vc = player.voice_client
    global playback_task
    if not playback_task or playback_task.done():
        playback_task = bot.loop.create_task(playback_loop(None))
    return song

async def add_playlist(url: str) -> list[Song]:
    """Download a playlist and queue its tracks."""
    global last_playlist_files
    last_playlist_files.clear()
    with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': 'in_playlist'}) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get('entries') or []
    added: list[Song] = []
    for e in entries:
        if len(player.queue) >= 10:
            break
        track_url = e.get('url') or e.get('webpage_url')
        if not track_url:
            continue
        if not track_url.startswith('http'):
            track_url = f"https://www.youtube.com/watch?v={track_url}"
        try:
            song = await player.add_song(track_url)
            added.append(song)
            last_playlist_files.add(song.filepath)
        except RuntimeError:
            break
    return added

async def add_playlist_and_play(url: str) -> list[Song]:
    """Add playlist tracks and ensure playback starts."""
    songs = await add_playlist(url)
    vc = player.voice_client
    if not vc or not vc.is_connected():
        if last_channel_id is not None:
            await join_channel(last_channel_id)
            vc = player.voice_client
        else:
            channels = list_voice_channels()
            if channels:
                first_id = next(iter(channels.keys()))
                await join_channel(first_id)
                vc = player.voice_client
    global playback_task
    if vc and (not playback_task or playback_task.done()):
        playback_task = bot.loop.create_task(playback_loop(None))
    return songs

player = MusicPlayer()

# ---- Audio configuration ----
def channel_bitrate() -> int:
    """Return the target bitrate in kbps for the connected voice channel."""
    vc = player.voice_client
    if vc and vc.channel and getattr(vc.channel, 'bitrate', None):
        try:
            # Discord allows up to 384 kb/s for boosted servers.
            # Convert the channel bitrate from bps to kbps and clamp it.
            return max(94, min(vc.channel.bitrate // 1000, 384))
        except Exception:
            pass
    # Default to a higher quality bitrate when the channel doesn't report one
    return 128

def ffmpeg_options(room_kbps: int) -> list[str]:
    return [
        "-vn", "-sn",                   # audio-only
        #"-af",  "loudnorm=I=-16:TP=-1.5:LRA=7",  # gentle LUFS normalise
        "-vbr", "constrained",          # CVBR survives Discord re-muxing better
        #"-application", "audio",        # duplicate-safe but explicit
    ]

# ---- Voice channel helpers ----
def list_voice_channels() -> dict[int, str]:
    if not bot.guilds:
        return {}
    guild = bot.guilds[0]
    return {ch.id: ch.name for ch in guild.voice_channels}

async def join_channel(channel_id: int):
    channels = list_voice_channels()
    if channel_id not in channels:
        return
    guild = bot.guilds[0]
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, nextcord.VoiceChannel):
        return
    vc = guild.voice_client
    if vc and vc.is_connected():
        await vc.move_to(channel)
        player.voice_client = vc
    else:
        player.voice_client = await channel.connect()
    global last_channel_id
    last_channel_id = channel_id

# ---- TTS helper (gTTS) ----
async def speak(text: str):
    vc = player.voice_client
    if not vc:
        return
    while vc.is_playing():
        await asyncio.sleep(0.1)
    tts_mp3 = os.path.join(DOWNLOAD_DIR, f"tts_{uuid.uuid4()}.mp3")
    loop = asyncio.get_event_loop()
    def gen_tts():
        t = gTTS(text, lang='en')
        t.save(tts_mp3)
    try:
        await loop.run_in_executor(None, gen_tts)
    except Exception:
        return
    done = asyncio.Event()
    bit = channel_bitrate()
    opts = ffmpeg_options(channel_bitrate())
    log.debug("FFMPEG OPTS  %s", " ".join(opts))   #  add this
    reconnect = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

    src = await nextcord.FFmpegOpusAudio.from_probe(
        tts_mp3,                        # or song.filepath
        options="-vn -sn"      # nothing that would force re-encode
    )
    vc.play(src, after=lambda _: done.set())
    await done.wait()
    try:
        os.remove(tts_mp3)
    except:
        pass

# ---- Download logic ----
def get_audio_duration(path: str) -> float:
    """Return audio duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, check=True
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0

async def download_audio(query: str) -> Song:
    """
    Download a track (YouTube or Spotify), optionally copy the *raw*
    file and a *single-pass* Opus encode into the _archive/ tree, and
    return the Song pointing at the working copy inside DOWNLOAD_DIR.
    """
    import shutil                                    # new
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    downloads_in_progress[query] = datetime.now()

    try:
        # ------------------------------------------------------------
        # 1) SPOTIFY (spotDL)
        # ------------------------------------------------------------
        if re.search(r'https?://(?:open\.)?spotify\.com/track/', query):
            template  = f"{uuid.uuid4()}.%(ext)s"
            outfile   = os.path.join(DOWNLOAD_DIR, template)
            proc      = await asyncio.create_subprocess_exec(
                'spotdl', query, '--output', outfile,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill(); await proc.communicate()
                raise RuntimeError("Spotify download timed out")

            prefix  = os.path.basename(outfile).split('%')[0]
            entry   = next((x for x in os.listdir(DOWNLOAD_DIR) if x.startswith(prefix)), None)
            if not entry:
                raise RuntimeError("spotDL finished but produced no file")

            path = os.path.join(DOWNLOAD_DIR, entry)
            if os.path.isdir(path):                           # spotDL sometimes makes a folder
                for root, _, files in os.walk(path):
                    for f in files:
                        if f.lower().endswith(('.mp3','.m4a','.flac','.wav','.opus','.ogg')):
                            path = os.path.join(root, f); break
            title = os.path.splitext(os.path.basename(path))[0]
            duration = get_audio_duration(path)

        # ------------------------------------------------------------
        # 2) YOUTUBE (yt-dlp)
        # ------------------------------------------------------------
        else:
            cmd = [
                'yt-dlp', '--print-json', '-f', 'bestaudio/best',
                '-o', os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'), query
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill(); await proc.communicate()
                raise RuntimeError("YouTube download timed out")

            try:
                info = json.loads(out.decode().splitlines()[-1])
            except Exception:
                raise RuntimeError("yt-dlp did not return JSON")

            file_id  = info.get('id');     ext = info.get('ext')
            title    = info.get('title', 'Unknown')
            duration = info.get('duration') or 0
            if not file_id or not ext:
                raise RuntimeError("yt-dlp returned incomplete data")

            path = os.path.join(DOWNLOAD_DIR, f"{file_id}.{ext}")
            if not os.path.isfile(path):
                raise RuntimeError("yt-dlp finished but file is missing")
            if not duration:
                duration = get_audio_duration(path)

        # ----------------------------------------------------------------
        # DEBUG / ARCHIVE    keep both *raw* and *encoded* snapshots
        # ----------------------------------------------------------------
        if DEBUG_ARCHIVE:
            basename = os.path.splitext(os.path.basename(path))[0]

            # 1) raw copy (identical bytes)
            raw_copy = os.path.join(RAW_DIR, os.path.basename(path))
            if not os.path.isfile(raw_copy):
                shutil.copy2(path, raw_copy)

            # 2) single-pass Opus encode at 128k (if not already Opus)
            enc_copy = os.path.join(ENC_DIR, f"{basename}.opus")
            if not os.path.isfile(enc_copy):
                # if the source **is already** Opus we just duplicate it
                if path.lower().endswith('.opus') or info.get("acodec") == "opus":
                    shutil.copy2(path, enc_copy)
                else:
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", path,
                         "-c:a", "libopus", "-b:a", "128k",
                         "-vn", "-sn", enc_copy],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=True
                    )

        # The bot itself keeps using the WORKING copy inside DOWNLOAD_DIR
        return Song(title, path, query, duration)

    finally:
        downloads_in_progress.pop(query, None)


# ---- Voice helper ----
async def ensure_voice(interaction: nextcord.Interaction) -> nextcord.VoiceClient:
    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        player.voice_client = vc
        return vc
    if not interaction.user.voice or not interaction.user.voice.channel:
        raise RuntimeError("You must be in a voice channel.")
    vc = await interaction.user.voice.channel.connect()
    player.voice_client = vc
    global last_channel_id
    last_channel_id = interaction.user.voice.channel.id
    return vc

# ---- Command handler ----
async def handle_command(cmd: str):
    vc = player.voice_client
    if not vc:
        return
    if cmd in ('skip', 'stop') and vc.is_playing():
        vc.stop()
    elif cmd == 'pause' and vc.is_playing():
        player.paused_pos = time.time() - player.start_time
        vc.pause()
    elif cmd == 'resume' and vc.is_paused():
        if player.paused_pos is not None:
            player.start_time = time.time() - player.paused_pos
            player.paused_pos = None
        vc.resume()
    elif cmd == 'loop':
        player.loop = not player.loop
    elif cmd == 'loopqueue':
        player.loop_queue = not player.loop_queue
    elif cmd == 'clear':
        for s in list(player.queue):
            try: os.remove(s.filepath)
            except: pass
        player.queue.clear()

async def remove_at(index: int):
    """Remove a queued song by its index."""
    if index < 0 or index >= len(player.queue):
        return
    song = player.queue.pop(index)
    try:
        os.remove(song.filepath)
    except Exception:
        pass

async def remove_last_playlist():
    """Remove songs added by the last playlist command."""
    global last_playlist_files
    removed = 0
    for song in list(player.queue):
        if song.filepath in last_playlist_files:
            try:
                os.remove(song.filepath)
            except Exception:
                pass
            player.queue.remove(song)
            removed += 1
    last_playlist_files.clear()
    return removed

async def set_volume(level: int):
    """Safely adjust playback volume from within the event loop."""
    level = max(0, min(level, 100))
    async with player.lock:
        player.volume = level / 100
        vc = player.voice_client
        if vc and player.current and (vc.is_playing() or vc.is_paused()):
            pos = player.paused_pos if player.paused_pos is not None else time.time() - player.start_time
            player.paused_pos = pos if vc.is_paused() else None
            player.seek_pos = pos
            vc.stop()

async def seek_to(position: float):
    """Safely seek to a position in the current song."""
    if position < 0:
        position = 0
    async with player.lock:
        player.seek_pos = position
        player.start_time = time.time() - position
        vc = player.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            was_paused = vc.is_paused() or player.paused_pos is not None
            player.paused_pos = position if was_paused else None
            vc.stop()

# ---- Slash commands ----
@bot.slash_command(description='Join your voice channel')
async def join(interaction: nextcord.Interaction):
    try:
        vc = await ensure_voice(interaction)
        await interaction.response.send_message(f" Joined **{vc.channel.name}**")
    except Exception as e:
        await interaction.response.send_message(str(e), ephemeral=True)

@bot.slash_command(description='Leave the voice channel')
async def leave(interaction: nextcord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        await vc.disconnect()
        player.current = None
        global playback_task
        playback_task = None
        await interaction.response.send_message(" Left the voice channel")
    else:
        await interaction.response.send_message("Not in a voice channel", ephemeral=True)

@bot.slash_command(description='Play a song or playlist')
async def play(interaction: nextcord.Interaction, query: str):
    await interaction.response.defer()
    try:
        await ensure_voice(interaction)
        if 'list=' in query:
            await speak("Please wait, downloading playlist this may take a while")
        else:
            await speak("Please wait, downloading song")

        m = re.search(r'music\.youtube\.com/playlist\?list=([^&]+)', query)
        if m:
            query = f"https://www.youtube.com/playlist?list={m.group(1)}"

        if re.search(r'https?://(?:open\.)?spotify\.com/track/', query):
            song = await player.add_song(query)
            await interaction.followup.send(f" Added **{song.title}** to the queue")
        elif 'list=' in query:
            with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': 'in_playlist'}) as ydl:
                info = ydl.extract_info(query, download=False)
            entries = info.get('entries') or []
            added = 0
            for e in entries:
                if len(player.queue) >= 10:
                    break
                url = e.get('url') or e.get('webpage_url')
                if not url:
                    continue
                if not url.startswith('http'):
                    url = f"https://www.youtube.com/watch?v={url}"
                try:
                    await player.add_song(url)
                    added += 1
                except (RuntimeError, IndexError):
                    break
            await interaction.followup.send(f" Added **{added}** songs from playlist")
        else:
            song = await player.add_song(query)
            await interaction.followup.send(f" Added **{song.title}** to the queue")

        global playback_task
        vc = interaction.guild.voice_client
        if vc and (not playback_task or playback_task.done()):
            playback_task = bot.loop.create_task(playback_loop(interaction))

    except Exception as e:
        await interaction.followup.send(str(e), ephemeral=True)

@bot.slash_command(description='Add all songs from a playlist')
async def playlist(interaction: nextcord.Interaction, url: str):
    await interaction.response.defer()
    try:
        await ensure_voice(interaction)
        await speak("Please wait, downloading playlist")
        songs = await add_playlist(url)
        await interaction.followup.send(f" Added **{len(songs)}** songs from playlist")
        global playback_task
        vc = interaction.guild.voice_client
        if vc and (not playback_task or playback_task.done()):
            playback_task = bot.loop.create_task(playback_loop(interaction))
    except Exception as e:
        await interaction.followup.send(str(e), ephemeral=True)

@bot.slash_command(description='Remove songs added by the last playlist command')
async def remove_playlist(interaction: nextcord.Interaction):
    removed = await remove_last_playlist()
    await interaction.response.send_message(f" Removed {removed} playlist songs")

@bot.slash_command(description='Skip the current song')
async def skip(interaction: nextcord.Interaction):
    await handle_command('skip')
    await interaction.response.send_message(" Skipped")

@bot.slash_command(description='Stop playback')
async def stop(interaction: nextcord.Interaction):
    await handle_command('stop')
    await interaction.response.send_message(" Stopped playback")

@bot.slash_command(description='Clear the queue')
async def clear(interaction: nextcord.Interaction):
    await handle_command('clear')
    await interaction.response.send_message(" Cleared the queue")

@bot.slash_command(description='Pause playback')
async def pause(interaction: nextcord.Interaction):
    await handle_command('pause')
    await interaction.response.send_message(" Paused playback")

@bot.slash_command(description='Resume playback')
async def resume(interaction: nextcord.Interaction):
    await handle_command('resume')
    await interaction.response.send_message(" Resumed playback")

@bot.slash_command(description='Toggle loop mode')
async def loop(interaction: nextcord.Interaction):
    player.loop = not player.loop
    await interaction.response.send_message(f" Loop is now **{'on' if player.loop else 'off'}**")

@bot.slash_command(description='Toggle queue loop mode')
async def loopqueue(interaction: nextcord.Interaction):
    player.loop_queue = not player.loop_queue
    await interaction.response.send_message(f" Queue loop is now **{'on' if player.loop_queue else 'off'}**")

@bot.slash_command(description='Replay the previous song')
async def back(interaction: nextcord.Interaction):
    if len(player.history) < 2:
        return await interaction.response.send_message("No previous song", ephemeral=True)
    prev = player.history[-2]
    if len(player.queue) >= 10:
        return await interaction.response.send_message("Queue full", ephemeral=True)
    song = await player.add_song(prev.query)
    player.queue.insert(0, song)
    await interaction.response.send_message(f" Replaying **{song.title}**")
    global playback_task
    vc = interaction.guild.voice_client
    if vc and (not playback_task or playback_task.done()):
        playback_task = bot.loop.create_task(playback_loop(interaction))

@bot.slash_command(description='Show the queue')
async def show_queue(interaction: nextcord.Interaction):
    if not player.queue:
        return await interaction.response.send_message("The queue is empty", ephemeral=True)
    listing = "\n".join(f"{i+1}. {s.title}" for i, s in enumerate(player.queue))
    await interaction.response.send_message(f" Queue:\n{listing}")

@bot.slash_command(description='Set playback volume (0-100)')
async def volume(interaction: nextcord.Interaction, level: int):
    await set_volume(level)
    await interaction.response.send_message(f" Volume set to {max(0, min(level, 100))}%")

@bot.slash_command(description='Show status')
async def status(interaction: nextcord.Interaction):
    lines = [
        f" Currently playing: {player.current.title if player.current else 'none'}",
        f" Queue length: {len(player.queue)}"
    ]
    if downloads_in_progress:
        lines.append(" Downloading:")
        for q, t in downloads_in_progress.items():
            elapsed = int((datetime.now() - t).total_seconds())
            lines.append(f"  {q} ({elapsed}s)")
    else:
        lines.append(" Downloading: none")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

# ---- Playback loop ----
async def playback_loop(interaction: nextcord.Interaction | None = None):
    global playback_task
    vc = player.voice_client
    if not vc:
        playback_task = None
        return
    try:
        while True:
            if not vc.is_connected():
                break
            player.play_next.clear()
            if not player.queue:
                if player.current and not vc.is_playing():
                    player.current = None
                    player.start_time = 0.0
                await asyncio.sleep(1)
                continue
            song = player.queue.pop(0)
            player.history.append(song)
            player.current = song
            player.start_time = time.time()
            try:
                await speak(f"Now playing {song.title}")
                bit = channel_bitrate()
                opts = ffmpeg_options(channel_bitrate())
                log.debug("FFMPEG OPTS  %s", " ".join(opts))   #  add this
                src = await nextcord.FFmpegOpusAudio.from_probe(
                    song.filepath,
                    options="-vn -sn"
                )
                vc.play(src, after=lambda _: player.play_next.set())
                if player.paused_pos is not None:
                    vc.pause()
                if interaction:
                    await interaction.followup.send(f" Now playing **{song.title}**")
            except Exception as e:
                log.error(f"Playback error for {song.title}: {e}")
                continue
            await player.play_next.wait()
            if player.seek_pos is not None:
                pos = player.seek_pos
                player.seek_pos = None
                player.start_time = time.time() - pos
                try:
                    bit = channel_bitrate()
                    opts = ffmpeg_options(channel_bitrate())
                    log.debug("FFMPEG OPTS  %s", " ".join(opts))   #  add this

                    src = await nextcord.FFmpegOpusAudio.from_probe(
                        song.filepath,
                        before_options=f' -ss {pos}',
                        options="-vn -sn"     # audio-only, no extra filters
                    )
                    player.play_next.clear()
                    vc.play(src, after=lambda _: player.play_next.set())
                    if player.paused_pos is not None:
                        vc.pause()
                except Exception as e:
                    log.error(f'Seek error: {e}')
                    player.play_next.set()
                await player.play_next.wait()
            if not player.loop:
                if player.loop_queue:
                    player.queue.append(song)
                else:
                    try:
                        os.remove(song.filepath)
                    except Exception:
                        pass
            else:
                player.queue.insert(0, song)
    finally:
        playback_task = None

# ---- HTTP server serving external index.html + API ----
def start_http_server():
    html_path = os.path.join(os.path.dirname(__file__), 'index.html')

    class AuthHandler(BaseHTTPRequestHandler):
        def do_AUTHHEAD(self):
            self.send_response(401)
            self.send_header('Content-type','application/json')
            self.end_headers()
            self.wfile.write(b'{"error":"auth"}')

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            # serve index.html
            if parsed.path in ('/', '/index.html'):
                try:
                    with open(html_path, 'rb') as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header('Content-type','text/html')
                    self.send_header('Content-length', str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                except FileNotFoundError:
                    self.send_error(404, 'index.html not found')
                return

            # require auth for /api
            if not parsed.path.startswith('/api/'):
                return self.send_error(404)
            auth = self.headers.get('Authorization')
            if not auth or not auth.startswith('Basic '):
                return self.do_AUTHHEAD()
            user, pwd = base64.b64decode(auth.split(' ',1)[1]).decode().split(':',1)
            if user != AUTH_USER or pwd != AUTH_PASS:
                return self.do_AUTHHEAD()

            cmd = parsed.path[len('/api/'):]
            params = urllib.parse.parse_qs(parsed.query)

            if cmd in ('skip','stop','pause','resume','clear','loop','loopqueue'):
                asyncio.run_coroutine_threadsafe(handle_command(cmd), bot.loop)
            elif cmd == 'add' and 'query' in params:
                q = params['query'][0]
                asyncio.run_coroutine_threadsafe(add_and_play(q), bot.loop)
            elif cmd == 'playlist' and 'url' in params:
                url = params['url'][0]
                asyncio.run_coroutine_threadsafe(add_playlist_and_play(url), bot.loop)
            elif cmd == 'remove_playlist':
                asyncio.run_coroutine_threadsafe(remove_last_playlist(), bot.loop)
            elif cmd == 'remove' and 'pos' in params:
                try:
                    pos = int(params['pos'][0]) - 1
                    asyncio.run_coroutine_threadsafe(remove_at(pos), bot.loop)
                except:
                    pass
            elif cmd == 'join' and 'channel' in params:
                try:
                    chan = int(params['channel'][0])
                    asyncio.run_coroutine_threadsafe(join_channel(chan), bot.loop)
                except:
                    pass
            elif cmd == 'seek' and 'pos' in params:
                try:
                    pos = float(params['pos'][0])
                    asyncio.run_coroutine_threadsafe(seek_to(pos), bot.loop)
                except Exception:
                    pass
            elif cmd == 'volume' and 'level' in params:
                try:
                    lvl = int(params['level'][0])
                    asyncio.run_coroutine_threadsafe(set_volume(lvl), bot.loop)
                except Exception:
                    pass
            elif cmd == 'queue':
                pass
            else:
                return self.send_error(400)

            resp = {
                'current': player.current.title if player.current else None,
                'queue': [s.title for s in player.queue],
                'loop': player.loop,
                'loop_queue': player.loop_queue,
                'duration': player.current.duration if player.current else 0,
                'position': (player.paused_pos if player.paused_pos is not None
                            else (time.time() - player.start_time if player.current else 0)),
                'volume': int(player.volume * 100),
                'paused': bool(player.voice_client.is_paused()) if player.voice_client else False,
            }
            resp['downloads'] = {
                q: int((datetime.now() - t).total_seconds())
                for q, t in downloads_in_progress.items()
            }
            resp['channels'] = {str(cid): name for cid, name in list_voice_channels().items()}
            resp['connected'] = player.voice_client.channel.name if player.voice_client else None
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header('Content-type','application/json')
            self.send_header('Content-length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(('0.0.0.0', HTTP_CONTROL_PORT), AuthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info(f"HTTP control (auth) on port {HTTP_CONTROL_PORT}")

# ---- Bot events & cleanup ----
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    periodic_cleanup.start()

@bot.event
async def on_application_command_error(interaction, error):
    log.error(f"Error in {interaction.command.name}: {error}")
    if isinstance(error, RuntimeError):
        await interaction.response.send_message(str(error), ephemeral=True)

@tasks.loop(hours=1)
async def periodic_cleanup():
    cutoff = datetime.now() - timedelta(hours=FILE_RETENTION_HOURS)
    removed = 0
    for fname in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, fname)
        if os.path.isfile(path) and datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
            try:
                os.remove(path)
                removed += 1
            except:
                pass
    if removed:
        log.info(f"Cleaned up {removed} old files")

# ---- Entrypoint ----
if __name__ == '__main__':
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    start_http_server()
    token = os.environ.get('DISCORD_TOKEN', 'discordtoken')
    if not token:
        log.error('DISCORD_TOKEN not set'); sys.exit(1)
    bot.run(token)
