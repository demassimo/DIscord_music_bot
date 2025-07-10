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

player = MusicPlayer()

# ---- Audio configuration ----
def channel_bitrate() -> int:
    """Return the target bitrate in kbps for the connected voice channel."""
    vc = player.voice_client
    if vc and vc.channel and getattr(vc.channel, 'bitrate', None):
        try:
            # Discord allows up to 384 kb/s for boosted servers.
            # Convert the channel bitrate from bps to kbps and clamp it.
            return max(32, min(vc.channel.bitrate // 1000, 384))
        except Exception:
            pass
    # Default to a higher quality bitrate when the channel doesn't report one
    return 128

def ffmpeg_options() -> str:
    """Return common ffmpeg options with current volume."""
    opts = [
        '-application', 'audio',
        '-compression_level', '10',
        '-vbr', 'on',
        '-frame_duration', '20',
    ]
    if player.volume != 1.0:
        opts += ['-filter:a', f'volume={player.volume}']
    return ' '.join(opts)

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
    src = nextcord.FFmpegOpusAudio(
        tts_mp3,
        bitrate=bit,
        options=ffmpeg_options(),
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
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    downloads_in_progress[query] = datetime.now()
    try:
        # Spotify track
        if re.search(r'https?://(?:open\.)?spotify\.com/track/', query):
            template = f"{uuid.uuid4()}.%(ext)s"
            outfile = os.path.join(DOWNLOAD_DIR, template)
            proc = await asyncio.create_subprocess_exec(
                'spotdl', query, '--output', outfile,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError('Spotify download timed out')
            prefix = os.path.basename(outfile).split('%')[0]
            entries = [fn for fn in os.listdir(DOWNLOAD_DIR) if fn.startswith(prefix)]
            if not entries:
                raise RuntimeError("spotdl finished but no file found")
            entry = entries[0]
            path = os.path.join(DOWNLOAD_DIR, entry)
            if os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for f in files:
                        if f.lower().endswith(('.mp3','.m4a','.flac','.wav','.opus','.ogg')):
                            dur = get_audio_duration(os.path.join(root, f))
                            return Song(os.path.splitext(f)[0], os.path.join(root, f), query, dur)
                raise RuntimeError("spotdl created directory but no audio inside")
            return Song(os.path.splitext(entry)[0], path, query, get_audio_duration(path))

        # YouTube/other via yt-dlp CLI with timeout
        cmd = [
            'yt-dlp', '--print-json', '-f', 'bestaudio/best',
            '-o', os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'), query
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError('YouTube download timed out')
        try:
            info = json.loads(out.decode().splitlines()[-1])
        except Exception:
            raise RuntimeError('yt-dlp did not return JSON')
        file_id = info.get('id')
        ext = info.get('ext')
        title = info.get('title', 'Unknown')
        duration = info.get('duration') or 0
        if not file_id or not ext:
            raise RuntimeError('yt-dlp did not return valid id/ext')
        path = os.path.join(DOWNLOAD_DIR, f"{file_id}.{ext}")
        if not os.path.isfile(path):
            raise RuntimeError('yt-dlp finished but file not found on disk')
        if not duration:
            duration = get_audio_duration(path)
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
        vc.pause()
    elif cmd == 'resume' and vc.is_paused():
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

@bot.slash_command(description='Skip the current song')
async def skip(interaction: nextcord.Interaction):
    await handle_command('skip')
    await interaction.response.send_message(" Skipped")

@bot.slash_command(description='Stop playback')
async def stop(interaction: nextcord.Interaction):
    await handle_command('stop')
    await interaction.response.send_message(" Stopped playback")

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
    level = max(0, min(level, 100))
    player.volume = level / 100
    await interaction.response.send_message(f" Volume set to {level}%")
    if player.voice_client and player.current and player.voice_client.is_playing():
        player.seek_pos = time.time() - player.start_time
        player.voice_client.stop()

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
                src = nextcord.FFmpegOpusAudio(
                    song.filepath,
                    bitrate=bit,
                    options=ffmpeg_options(),
                )
                vc.play(src, after=lambda _: player.play_next.set())
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
                    src = nextcord.FFmpegOpusAudio(
                        song.filepath,
                        bitrate=bit,
                        before_options=f'-ss {pos}',
                        options=ffmpeg_options(),
                    )
                    player.play_next.clear()
                    vc.play(src, after=lambda _: player.play_next.set())
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
                    player.seek_pos = pos
                    player.start_time = time.time() - pos
                    if player.voice_client:
                        player.voice_client.stop()
                except Exception:
                    pass
            elif cmd == 'volume' and 'level' in params:
                try:
                    lvl = int(params['level'][0])
                    lvl = max(0, min(lvl, 100))
                    player.volume = lvl / 100
                    if player.voice_client and player.current and player.voice_client.is_playing():
                        player.seek_pos = time.time() - player.start_time
                        player.voice_client.stop()
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
                'position': time.time() - player.start_time if player.current else 0,
                'volume': int(player.volume * 100),
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
    token = os.environ.get('DISCORD_TOKEN', '')
    if not token:
        log.error('DISCORD_TOKEN not set'); sys.exit(1)
    bot.run(token)
