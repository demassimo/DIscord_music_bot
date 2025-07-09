import os
import uuid
import asyncio
import subprocess
import nextcord
from nextcord.ext import commands
import yt_dlp
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse
import threading

DOWNLOAD_DIR = 'downloads'

intents = nextcord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

class Song:
    def __init__(self, title: str, filepath: str):
        self.title = title
        self.filepath = filepath

class MusicPlayer:
    def __init__(self):
        self.queue = []
        self.history = []
        self.loop = False
        self.play_next_song = asyncio.Event()
        self.playlist_songs = []
        self.voice_client = None

    async def add_song(self, query: str) -> Song:
        if len(self.queue) >= 10:
            raise Exception('Queue limit reached')
        song = await download_audio(query)
        self.queue.append(song)
        return song

player = MusicPlayer()

async def download_audio(query: str) -> Song:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    if 'spotify.com' in query:
        outfile = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.%(ext)s")
        cmd = ['spotdl', query, '--output', outfile]
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.communicate()
        # spotdl names file without extension placeholder
        downloaded = None
        for f in os.listdir(DOWNLOAD_DIR):
            if outfile.split('%')[0] in f:
                downloaded = os.path.join(DOWNLOAD_DIR, f)
                break
        title = query
        return Song(title, downloaded)
    else:
        outfile = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.%(ext)s")
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': outfile,
            'quiet': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            downloaded = ydl.prepare_filename(info)
            title = info.get('title', 'Unknown')
        return Song(title, downloaded)

async def play_next(ctx):
    if not ctx.voice_client:
        return
    while True:
        player.play_next_song.clear()
        if not player.queue:
            await asyncio.sleep(1)
            continue
        song = player.queue.pop(0)
        player.history.append(song)
        source = nextcord.FFmpegPCMAudio(song.filepath)
        ctx.voice_client.play(source, after=lambda e: player.play_next_song.set())
        await ctx.send(f'Now playing: {song.title}')
        await player.play_next_song.wait()
        if not player.loop:
            if os.path.exists(song.filepath):
                os.remove(song.filepath)
            if song in player.playlist_songs:
                player.playlist_songs.remove(song)
        else:
            player.queue.insert(0, song)

@bot.slash_command(description='Join voice channel')
async def join(interaction: nextcord.Interaction):
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        vc = await channel.connect()
        player.voice_client = vc
        await interaction.response.send_message(f'Joined {channel}')
    else:
        await interaction.response.send_message('You are not in a voice channel', ephemeral=True)

@bot.slash_command(description='Play a song from URL or search query')
async def play(interaction: nextcord.Interaction, query: str):
    if not interaction.user.voice:
        await interaction.response.send_message('You must be in a voice channel', ephemeral=True)
        return
    if not interaction.guild.voice_client:
        vc = await interaction.user.voice.channel.connect()
        player.voice_client = vc
    try:
        song = await player.add_song(query)
        await interaction.response.send_message(f'Added {song.title} to queue')
    except Exception as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    if not interaction.guild.voice_client.is_playing():
        bot.loop.create_task(play_next(interaction))

@bot.slash_command(description='Skip current song')
async def skip(interaction: nextcord.Interaction):
    if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        interaction.guild.voice_client.stop()
        await interaction.response.send_message('Skipped')
    else:
        await interaction.response.send_message('Nothing playing', ephemeral=True)

@bot.slash_command(description='Toggle loop mode')
async def loop(interaction: nextcord.Interaction):
    player.loop = not player.loop
    await interaction.response.send_message(f'Loop is now {"on" if player.loop else "off"}')

@bot.slash_command(description='Go back to previous song')
async def back(interaction: nextcord.Interaction):
    if player.history:
        song = player.history.pop()
        player.queue.insert(0, song)
        if interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.stop()
        await interaction.response.send_message(f'Replaying: {song.title}')
    else:
        await interaction.response.send_message('No history', ephemeral=True)

@bot.slash_command(description='Show queue')
async def queue(interaction: nextcord.Interaction):
    if player.queue:
        titles = [s.title for s in player.queue]
        await interaction.response.send_message('\n'.join(titles))
    else:
        await interaction.response.send_message('Queue is empty')

@bot.slash_command(description='Add playlist URL')
async def playlist(interaction: nextcord.Interaction, url: str):
    if not interaction.user.voice:
        await interaction.response.send_message('You must be in a voice channel', ephemeral=True)
        return
    if not interaction.guild.voice_client:
        vc = await interaction.user.voice.channel.connect()
        player.voice_client = vc

    playlist_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4()))
    os.makedirs(playlist_dir, exist_ok=True)
    cmd = ['spotdl', url, '--output', os.path.join(playlist_dir, '%(title)s.%(ext)s')]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.communicate()

    added = 0
    for file in sorted(os.listdir(playlist_dir)):
        if file.endswith('.mp3') and len(player.queue) < 10:
            song = Song(file, os.path.join(playlist_dir, file))
            player.queue.append(song)
            player.playlist_songs.append(song)
            added += 1
    msg = f'Added {added} songs from playlist'
    if len(os.listdir(playlist_dir)) > added:
        msg += ' (queue limit reached)'
    await interaction.response.send_message(msg)
    if not interaction.guild.voice_client.is_playing():
        bot.loop.create_task(play_next(interaction))

@bot.slash_command(description='Remove playlist from queue')
async def remove_playlist(interaction: nextcord.Interaction):
    removed = 0
    for song in list(player.playlist_songs):
        if song in player.queue:
            player.queue.remove(song)
            removed += 1
        if os.path.exists(song.filepath):
            os.remove(song.filepath)
        player.playlist_songs.remove(song)
    await interaction.response.send_message(f'Removed {removed} songs from queue')

@bot.user_command(name='Show Queue')
async def show_queue_ctx(interaction: nextcord.Interaction, member: nextcord.Member):
    if member != bot.user:
        await interaction.response.send_message('Use this on the bot', ephemeral=True)
        return
    if player.queue:
        titles = [s.title for s in player.queue]
        await interaction.response.send_message('\n'.join(titles))
    else:
        await interaction.response.send_message('Queue is empty')

async def handle_command(cmd: str):
    vc = player.voice_client
    if not vc:
        return
    if cmd == 'skip' and vc.is_playing():
        vc.stop()
    elif cmd == 'clear':
        for song in list(player.queue):
            if os.path.exists(song.filepath):
                os.remove(song.filepath)
        player.queue.clear()
        player.playlist_songs.clear()

def start_http_server():
    port = int(os.environ.get('HTTP_CONTROL_PORT', '8080'))

    class ControlHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == '/command':
                params = urllib.parse.parse_qs(parsed.query)
                cmd = params.get('cmd', [None])[0]
                if cmd:
                    asyncio.run_coroutine_threadsafe(handle_command(cmd), bot.loop)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'OK')
            else:
                self.send_response(404)
                self.end_headers()

    server = HTTPServer(('0.0.0.0', port), ControlHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

start_http_server()
bot.run(os.environ.get('DISCORD_TOKEN'))
