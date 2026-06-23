import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import json
import os
import subprocess
import ctypes
import ctypes.util
import aiohttp
import time
import re
from typing import Optional

TOKEN = os.environ.get("DISCORD_TOKEN", "")
BOT_START_TIME = time.time()

ACCENT   = 0x9B59B6
SUCCESS  = 0x2ECC71
WARNING  = 0xE67E22
DANGER   = 0xE74C3C
INFO     = 0x3498DB

# ── Opus loader ──────────────────────────────────────────────────────────────
def load_opus():
    if discord.opus.is_loaded():
        return
    candidates = [
        ctypes.util.find_library("opus"),
        "libopus.so.0",
        "libopus.so",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/aarch64-linux-gnu/libopus.so.0",
        "/usr/lib/libopus.so.0",
        "/usr/local/lib/libopus.so.0",
    ]
    for path in candidates:
        if path:
            try:
                discord.opus.load_opus(path)
                print(f"Opus loaded from: {path}")
                return
            except Exception:
                pass
    print("Warning: Could not load opus. Voice may not work.")

load_opus()

# ── Helpers ──────────────────────────────────────────────────────────────────
def fmt_duration(seconds: int) -> str:
    if not seconds:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

def yt_thumbnail(webpage_url: str) -> Optional[str]:
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", webpage_url)
    if m:
        return f"https://img.youtube.com/vi/{m.group(1)}/hqdefault.jpg"
    return None

def rp(color: int, title: str = "", description: str = "") -> discord.Embed:
    """Base rich-presence-style embed: author row + footer using the bot's identity."""
    e = discord.Embed(color=color, description=description or None)
    if title:
        e.title = title
    e.set_author(name="Griffith Music", icon_url=bot.user.display_avatar.url)
    e.set_footer(text="Griffith Music", icon_url=bot.user.display_avatar.url)
    return e

def now_playing_embed(track: dict, queue_size: int = 0) -> discord.Embed:
    duration = fmt_duration(track.get("duration", 0))
    embed = rp(ACCENT)
    embed.set_author(name="▶  Now Playing", icon_url=bot.user.display_avatar.url)
    embed.description = f"### [{track['title']}]({track['webpage_url']})"
    embed.add_field(name="Duration", value=f"`{duration}`", inline=True)
    if queue_size:
        embed.add_field(name="Up Next", value=f"`{queue_size} track{'s' if queue_size != 1 else ''}`", inline=True)
    thumb = yt_thumbnail(track.get("webpage_url", ""))
    if thumb:
        embed.set_thumbnail(url=thumb)
    return embed

def queued_embed(track: dict, position: int) -> discord.Embed:
    duration = fmt_duration(track.get("duration", 0))
    embed = rp(INFO)
    embed.set_author(name="Added to Queue", icon_url=bot.user.display_avatar.url)
    embed.description = f"**[{track['title']}]({track['webpage_url']})**"
    embed.add_field(name="Duration", value=f"`{duration}`", inline=True)
    embed.add_field(name="Position", value=f"`#{position}`", inline=True)
    thumb = yt_thumbnail(track.get("webpage_url", ""))
    if thumb:
        embed.set_thumbnail(url=thumb)
    return embed

# ── Spotify helpers ───────────────────────────────────────────────────────────
SPOTIFY_TRACK_RE    = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]+)")
SPOTIFY_ALBUM_RE    = re.compile(r"open\.spotify\.com/album/([A-Za-z0-9]+)")
SPOTIFY_PLAYLIST_RE = re.compile(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)")

def parse_spotify_url(url: str) -> tuple[str, str]:
    m = SPOTIFY_TRACK_RE.search(url)
    if m: return "track", m.group(1)
    m = SPOTIFY_ALBUM_RE.search(url)
    if m: return "album", m.group(1)
    m = SPOTIFY_PLAYLIST_RE.search(url)
    if m: return "playlist", m.group(1)
    return "", ""

async def spotify_track_query(track_url: str) -> Optional[str]:
    try:
        oembed = f"https://open.spotify.com/oembed?url={track_url}"
        async with aiohttp.ClientSession() as session:
            async with session.get(oembed, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = data.get("title", "").strip()
                    if title:
                        return title
    except Exception as e:
        print(f"Spotify oEmbed error: {e}")
    return None

async def scrape_spotify_tracks(kind: str, sid: str) -> list[str]:
    embed_url = f"https://open.spotify.com/embed/{kind}/{sid}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(embed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    print(f"Spotify embed returned {resp.status}")
                    return []
                html = await resp.text()

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
        if not match:
            print("Spotify embed: __NEXT_DATA__ not found")
            return []

        data = json.loads(match.group(1))

        def find_key(obj, key, depth=0):
            if depth > 10: return None
            if isinstance(obj, dict):
                if key in obj: return obj[key]
                for v in obj.values():
                    r = find_key(v, key, depth + 1)
                    if r is not None: return r
            elif isinstance(obj, list):
                for item in obj:
                    r = find_key(item, key, depth + 1)
                    if r is not None: return r
            return None

        track_list = find_key(data, "trackList")
        if not track_list:
            print("Spotify embed: trackList not found in data")
            return []

        queries: list[str] = []
        for track in track_list[:50]:
            title  = track.get("title", "").strip()
            artist = track.get("subtitle", "").strip()
            if title:
                queries.append(f"{title} {artist}".strip())
        return queries

    except Exception as e:
        print(f"Spotify scrape error: {e}")
        return []

async def get_spotify_queries(url: str) -> tuple[str, list[str]]:
    kind, sid = parse_spotify_url(url)
    if not kind:
        return "", []
    if kind == "track":
        query = await spotify_track_query(url)
        return kind, [query] if query else []
    queries = await scrape_spotify_tracks(kind, sid)
    return kind, queries

# ── Per-guild music state ─────────────────────────────────────────────────────
class GuildMusic:
    def __init__(self):
        self.queue:      list[dict]     = []
        self.current:    Optional[dict] = None
        self.loop:       bool           = False
        self.stay_247:   bool           = False
        self.stay_channel_id: Optional[int] = None

guild_music: dict[int, GuildMusic] = {}

def get_state(guild_id: int) -> GuildMusic:
    if guild_id not in guild_music:
        guild_music[guild_id] = GuildMusic()
    return guild_music[guild_id]

# ── YT-DLP helpers ────────────────────────────────────────────────────────────
YDL_BASE_ARGS = [
    "yt-dlp",
    "--quiet",
    "--no-warnings",
]

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

YT_PLAYLIST_RE = re.compile(r"(?:youtube\.com|youtu\.be).*[?&]list=([A-Za-z0-9_-]+)")

async def fetch_info(query: str) -> Optional[dict]:
    """Resolve a search query or single video URL into a playable track dict."""
    loop = asyncio.get_event_loop()
    def _run():
        args = YDL_BASE_ARGS + [
            "--dump-json",
            "--no-playlist",
            "--format", "bestaudio/best",
            "--default-search", "ytsearch",
            query,
        ]
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[:300])
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        info = json.loads(lines[-1])
        return {
            "url":         info["url"],
            "title":       info.get("title", "Unknown"),
            "webpage_url": info.get("webpage_url", query),
            "duration":    info.get("duration", 0),
        }
    try:
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        print(f"yt-dlp error: {str(e)[:200]}")
        return None

async def fetch_yt_playlist(url: str) -> list[dict]:
    """
    Fast flat-extract of a YouTube playlist in order.
    Returns stub dicts (no audio URL yet — resolved lazily at play time).
    """
    loop = asyncio.get_event_loop()
    def _run():
        args = YDL_BASE_ARGS + [
            "--flat-playlist",
            "--dump-json",
            url,
        ]
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        tracks = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                vid_url = e.get("url") or e.get("webpage_url") or ""
                if not vid_url.startswith("http"):
                    vid_id = e.get("id", "")
                    vid_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""
                if not vid_url:
                    continue
                tracks.append({
                    "url":         "",
                    "title":       e.get("title", "Unknown"),
                    "webpage_url": vid_url,
                    "duration":    e.get("duration", 0),
                })
            except Exception:
                pass
        return tracks
    try:
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        print(f"yt-dlp playlist error: {str(e)[:200]}")
        return []

async def resolve_track(track: dict) -> Optional[dict]:
    """Fetch the real audio URL for a stub track (one that hasn't been resolved yet)."""
    if track.get("url"):
        return track
    return await fetch_info(track["webpage_url"])

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

async def update_presence(track: Optional[dict] = None):
    if track:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=track["title"],
            ),
        )
    else:
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="donn bloodline | armed",
            ),
        )

async def _play_next_async(guild_id: int, vc: discord.VoiceClient):
    state = get_state(guild_id)
    if not state.queue:
        state.current = None
        await update_presence(None)
        return

    track = state.queue.pop(0)

    # Lazy-resolve tracks that only have a webpage_url (e.g. from YouTube playlists)
    if not track.get("url"):
        resolved = await resolve_track(track)
        if not resolved:
            print(f"Skipping unresolvable track: {track.get('title')}")
            await _play_next_async(guild_id, vc)
            return
        track = resolved

    state.current = track
    source = discord.FFmpegPCMAudio(track["url"], **FFMPEG_OPTS)
    await update_presence(track)

    def after(err):
        if err:
            print(f"Player error: {err}")
        asyncio.run_coroutine_threadsafe(_play_next_async(guild_id, vc), bot.loop)

    vc.play(source, after=after)

def play_next(guild_id: int, vc: discord.VoiceClient):
    asyncio.run_coroutine_threadsafe(_play_next_async(guild_id, vc), bot.loop)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """If 24/7 mode is on, rejoin when the bot gets disconnected or left alone."""
    guild = member.guild
    state = get_state(guild.id)
    if not state.stay_247:
        return

    vc = guild.voice_client

    # Bot itself was disconnected — rejoin the saved channel
    if member.id == bot.user.id and before.channel and not after.channel:
        await asyncio.sleep(3)
        channel = guild.get_channel(state.stay_channel_id)
        if channel and isinstance(channel, discord.VoiceChannel):
            try:
                await channel.connect()
            except Exception as e:
                print(f"24/7 rejoin failed: {e}")
        return

    # Someone left and bot is now alone — stay, do nothing (don't auto-disconnect)
    if vc and before.channel and before.channel == vc.channel:
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans:
            pass  # 24/7 mode: stay in the channel even when empty

@bot.event
async def on_ready():
    for guild in bot.guilds:
        try:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"Cleared guild commands for: {guild.name}")
        except Exception as e:
            print(f"Guild clear failed for {guild.name}: {e}")
    try:
        cmds = await bot.tree.sync()
        print(f"Synced {len(cmds)} commands globally.")
    except Exception as e:
        print(f"Global sync failed: {e}")

    await update_presence(None)

    app_id = bot.user.id
    invite = f"https://discord.com/api/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot+applications.commands"
    print(f"\nLogged in as {bot.user}")
    print(f"Invite link: {invite}\n")

# ════════════════════════════════════════════════════════════════════════════
#  MUSIC COMMANDS
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="join", description="Join your voice channel")
async def cmd_join(interaction: discord.Interaction):
    if not interaction.user.voice:
        return await interaction.response.send_message(
            embed=rp(DANGER, description="You're not in a voice channel."), ephemeral=True)
    channel = interaction.user.voice.channel
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.move_to(channel)
    else:
        await channel.connect()
    embed = rp(SUCCESS)
    embed.set_author(name="Joined Voice Channel", icon_url=bot.user.display_avatar.url)
    embed.description = f"**{channel.name}**"
    await interaction.response.send_message(embed=embed)

@tree.command(name="leave", description="Leave the voice channel")
async def cmd_leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message(
            embed=rp(DANGER, description="I'm not in a voice channel."), ephemeral=True)
    await vc.disconnect()
    get_state(interaction.guild_id).queue.clear()
    await update_presence(None)
    embed = rp(WARNING)
    embed.set_author(name="Left Voice Channel", icon_url=bot.user.display_avatar.url)
    embed.description = "Disconnected and queue cleared."
    await interaction.response.send_message(embed=embed)

@tree.command(name="247", description="Toggle 24/7 mode — bot stays in VC even when everyone leaves")
async def cmd_247(interaction: discord.Interaction):
    if not interaction.user.voice:
        return await interaction.response.send_message(
            embed=rp(DANGER, description="Join a voice channel first."), ephemeral=True)

    state = get_state(interaction.guild_id)
    vc = interaction.guild.voice_client

    if state.stay_247:
        # Turn OFF
        state.stay_247 = False
        state.stay_channel_id = None
        embed = rp(WARNING)
        embed.set_author(name="24/7 Mode Off", icon_url=bot.user.display_avatar.url)
        embed.description = "I'll leave the VC when everyone's gone."
        return await interaction.response.send_message(embed=embed)

    # Turn ON — join channel if not already in one
    channel = interaction.user.voice.channel
    if not vc:
        vc = await channel.connect()
    elif vc.channel != channel:
        await vc.move_to(channel)

    state.stay_247 = True
    state.stay_channel_id = channel.id

    embed = rp(SUCCESS)
    embed.set_author(name="24/7 Mode On", icon_url=bot.user.display_avatar.url)
    embed.description = f"Locked into **{channel.name}** — I'll stay here and rejoin if kicked."
    embed.set_footer(text="Use /247 again to turn it off", icon_url=bot.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@tree.command(name="play", description="Play a song — YouTube, Spotify track/album/playlist, or search query")
@app_commands.describe(query="Song name, YouTube URL, or Spotify link")
async def cmd_play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        embed = discord.Embed(description="❌ Join a voice channel first.", color=DANGER)
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    await interaction.response.defer()

    vc = interaction.guild.voice_client
    if not vc:
        vc = await interaction.user.voice.channel.connect()
    elif interaction.user.voice.channel != vc.channel:
        await vc.move_to(interaction.user.voice.channel)

    state = get_state(interaction.guild_id)

    # ── Spotify ───────────────────────────────────────────────────────────
    kind, _ = parse_spotify_url(query)
    if kind:
        kind, spotify_queries = await get_spotify_queries(query)
        if not spotify_queries:
            return await interaction.followup.send(
                embed=rp(DANGER, description="Couldn't resolve that Spotify link."))

        label = {"track": "track", "album": "album", "playlist": "playlist"}.get(kind, kind)

        if kind == "track":
            # Single track — resolve now and play/queue immediately
            info = await fetch_info(spotify_queries[0])
            if not info:
                return await interaction.followup.send(
                    embed=rp(DANGER, description="Couldn't find that track on YouTube."))
            state.queue.append(info)
            if not vc.is_playing() and not vc.is_paused():
                play_next(interaction.guild_id, vc)
                await interaction.followup.send(embed=now_playing_embed(state.current or info, len(state.queue)))
            else:
                await interaction.followup.send(embed=queued_embed(info, len(state.queue)))
            return

        # Multi-track: add stubs in order right away, resolve lazily at play time
        stubs = [
            {"url": "", "title": sq, "webpage_url": sq, "duration": 0}
            for sq in spotify_queries
        ]
        was_idle = not vc.is_playing() and not vc.is_paused()
        state.queue.extend(stubs)

        embed = rp(SUCCESS)
        embed.set_author(name=f"Spotify {label.capitalize()} Queued", icon_url=bot.user.display_avatar.url)
        embed.description = f"Added **{len(stubs)}** track{'s' if len(stubs) != 1 else ''} to the queue."
        embed.add_field(name="Order", value="Tracks will play in playlist order.", inline=False)
        await interaction.followup.send(embed=embed)

        if was_idle:
            play_next(interaction.guild_id, vc)
        return

    # ── YouTube playlist ──────────────────────────────────────────────────
    if YT_PLAYLIST_RE.search(query):
        loading_embed = rp(ACCENT)
        loading_embed.set_author(name="Loading YouTube Playlist", icon_url=bot.user.display_avatar.url)
        loading_embed.description = "Fetching playlist order…"
        await interaction.followup.send(embed=loading_embed)

        tracks = await fetch_yt_playlist(query)
        if not tracks:
            return await interaction.channel.send(
                embed=rp(DANGER, description="Couldn't load that YouTube playlist."))

        was_idle = not vc.is_playing() and not vc.is_paused()
        state.queue.extend(tracks)

        done_embed = rp(SUCCESS)
        done_embed.set_author(name="YouTube Playlist Queued", icon_url=bot.user.display_avatar.url)
        done_embed.description = f"Added **{len(tracks)}** track{'s' if len(tracks) != 1 else ''} in playlist order."
        await interaction.channel.send(embed=done_embed)

        if was_idle:
            play_next(interaction.guild_id, vc)
        return

    # ── Single YouTube video or search query ──────────────────────────────
    info = await fetch_info(query)
    if not info:
        return await interaction.followup.send(
            embed=rp(DANGER, description="Couldn't find that song."))

    state.queue.append(info)

    if not vc.is_playing() and not vc.is_paused():
        play_next(interaction.guild_id, vc)
        await interaction.followup.send(embed=now_playing_embed(state.current or info, len(state.queue)))
    else:
        await interaction.followup.send(embed=queued_embed(info, len(state.queue)))

@tree.command(name="stop", description="Stop playback and clear the queue")
async def cmd_stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message(
            embed=rp(DANGER, description="Nothing is playing."), ephemeral=True)
    state = get_state(interaction.guild_id)
    state.queue.clear()
    state.current = None
    vc.stop()
    await update_presence(None)
    embed = rp(WARNING)
    embed.set_author(name="Stopped", icon_url=bot.user.display_avatar.url)
    embed.description = "Playback stopped and queue cleared."
    await interaction.response.send_message(embed=embed)

@tree.command(name="pause", description="Pause the current song")
async def cmd_pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not (vc and vc.is_playing()):
        return await interaction.response.send_message(
            embed=rp(DANGER, description="Nothing is playing."), ephemeral=True)
    vc.pause()
    state = get_state(interaction.guild_id)
    embed = rp(WARNING)
    embed.set_author(name="Paused", icon_url=bot.user.display_avatar.url)
    if state.current:
        embed.description = f"**[{state.current['title']}]({state.current['webpage_url']})**"
        thumb = yt_thumbnail(state.current.get("webpage_url", ""))
        if thumb:
            embed.set_thumbnail(url=thumb)
    await interaction.response.send_message(embed=embed)

@tree.command(name="resume", description="Resume the paused song")
async def cmd_resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not (vc and vc.is_paused()):
        return await interaction.response.send_message(
            embed=rp(DANGER, description="Nothing is paused."), ephemeral=True)
    vc.resume()
    state = get_state(interaction.guild_id)
    embed = rp(SUCCESS)
    embed.set_author(name="Resumed", icon_url=bot.user.display_avatar.url)
    if state.current:
        embed.description = f"**[{state.current['title']}]({state.current['webpage_url']})**"
        thumb = yt_thumbnail(state.current.get("webpage_url", ""))
        if thumb:
            embed.set_thumbnail(url=thumb)
    await interaction.response.send_message(embed=embed)

@tree.command(name="skip", description="Skip the current song")
async def cmd_skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        return await interaction.response.send_message(
            embed=rp(DANGER, description="Nothing to skip."), ephemeral=True)
    state = get_state(interaction.guild_id)
    embed = rp(INFO)
    embed.set_author(name="Skipped", icon_url=bot.user.display_avatar.url)
    if state.current:
        embed.description = f"**[{state.current['title']}]({state.current['webpage_url']})**"
        thumb = yt_thumbnail(state.current.get("webpage_url", ""))
        if thumb:
            embed.set_thumbnail(url=thumb)
    vc.stop()
    await interaction.response.send_message(embed=embed)

@tree.command(name="queue", description="Show the current queue")
async def cmd_queue(interaction: discord.Interaction):
    state = get_state(interaction.guild_id)
    if not state.current and not state.queue:
        embed = rp(INFO)
        embed.set_author(name="Queue is Empty", icon_url=bot.user.display_avatar.url)
        embed.description = "Use `/play` to add songs."
        return await interaction.response.send_message(embed=embed)

    embed = rp(ACCENT)
    embed.set_author(name="Queue", icon_url=bot.user.display_avatar.url)

    if state.current:
        duration = fmt_duration(state.current.get("duration", 0))
        embed.add_field(
            name="▶  Now Playing",
            value=f"**[{state.current['title']}]({state.current['webpage_url']})** `{duration}`",
            inline=False,
        )
        thumb = yt_thumbnail(state.current.get("webpage_url", ""))
        if thumb:
            embed.set_thumbnail(url=thumb)

    if state.queue:
        lines = []
        for i, s in enumerate(state.queue[:15]):
            dur = fmt_duration(s.get("duration", 0))
            lines.append(f"`{i+1}.` [{s['title']}]({s['webpage_url']}) `{dur}`")
        if len(state.queue) > 15:
            lines.append(f"*… and {len(state.queue) - 15} more*")
        embed.add_field(
            name=f"Up Next  —  {len(state.queue)} track{'s' if len(state.queue) != 1 else ''}",
            value="\n".join(lines),
            inline=False,
        )

    total_secs = sum(s.get("duration", 0) for s in state.queue)
    if total_secs:
        embed.add_field(name="Total Duration", value=f"`{fmt_duration(total_secs)}`", inline=True)

    await interaction.response.send_message(embed=embed)

# ════════════════════════════════════════════════════════════════════════════
#  HELP
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="help", description="Show all available commands")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        description=(
            "Music for your server — YouTube, Spotify tracks,\n"
            "albums & playlists, or just search by name."
        ),
        color=ACCENT,
    )
    embed.set_author(
        name="Griffith Music",
        icon_url=bot.user.display_avatar.url,
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)

    embed.add_field(
        name="▶  Playback",
        value=(
            "`/play` `<query>`\n"
            "`/pause`\n"
            "`/resume`\n"
            "`/skip`\n"
            "`/stop`"
        ),
        inline=True,
    )
    embed.add_field(
        name="🔊  Voice & Queue",
        value=(
            "`/queue`\n"
            "`/join`\n"
            "`/leave`\n"
            "`/247`\n"
            "`/logbot`\n"
            "`/log`"
        ),
        inline=True,
    )
    embed.add_field(
        name="🎵  Sources",
        value="YouTube · Spotify tracks\nSpotify albums · Spotify playlists",
        inline=False,
    )
    embed.set_footer(
        text=f"{bot.user.name}  •  type / to get started",
        icon_url=bot.user.display_avatar.url,
    )
    await interaction.response.send_message(embed=embed)

# ════════════════════════════════════════════════════════════════════════════
#  LOG / LOGBOT
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="log", description="Show all servers the bot is in")
async def cmd_log(interaction: discord.Interaction):
    try:
        guilds = bot.guilds
        if not guilds:
            embed = rp(INFO)
            embed.set_author(name="Servers", icon_url=bot.user.display_avatar.url)
            embed.description = "Not in any servers yet."
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        lines = []
        for i, g in enumerate(guilds, 1):
            count = g.member_count if g.member_count is not None else "?"
            lines.append(f"`{i}.` **{g.name}** — `{count}` members | `{g.id}`")

        embeds = []
        for page, i in enumerate(range(0, len(lines), 10)):
            embed = rp(INFO)
            embed.set_author(
                name=f"Servers  —  {len(guilds)} total" if page == 0 else "Servers (cont.)",
                icon_url=bot.user.display_avatar.url,
            )
            embed.description = "\n".join(lines[i:i+10])
            embeds.append(embed)

        await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)
    except Exception as e:
        print(f"/log error: {e}")
        err = rp(DANGER, description=str(e))
        try:
            await interaction.response.send_message(embed=err, ephemeral=True)
        except Exception:
            await interaction.followup.send(embed=err, ephemeral=True)

@tree.command(name="logbot", description="Show bot status and all servers it's in")
async def cmd_logbot(interaction: discord.Interaction):
    try:
        guilds        = bot.guilds
        latency_ms    = round(bot.latency * 1000)
        uptime_secs   = int(time.time() - BOT_START_TIME)
        h, rem        = divmod(uptime_secs, 3600)
        m, s          = divmod(rem, 60)
        uptime_str    = f"{h}h {m}m {s}s"
        total_members = sum(g.member_count or 0 for g in guilds)
        status_icon   = "🟢 Online" if latency_ms < 100 else "🟡 Slow" if latency_ms < 300 else "🔴 High Latency"

        embed = rp(ACCENT)
        embed.set_author(name="Bot Status", icon_url=bot.user.display_avatar.url)
        embed.set_thumbnail(url=bot.user.display_avatar.url)
        embed.add_field(name="Status",  value=status_icon,          inline=True)
        embed.add_field(name="Ping",    value=f"`{latency_ms}ms`",  inline=True)
        embed.add_field(name="Uptime",  value=f"`{uptime_str}`",    inline=True)
        embed.add_field(name="Servers", value=f"`{len(guilds)}`",   inline=True)
        embed.add_field(name="Members", value=f"`{total_members}`", inline=True)
        embed.add_field(name="Bot ID",  value=f"`{bot.user.id}`",   inline=True)

        if guilds:
            lines = [f"`{i}.` **{g.name}** — `{g.member_count or '?'}` members" for i, g in enumerate(guilds, 1)]
            for page_i, i in enumerate(range(0, len(lines), 10)):
                embed.add_field(
                    name="Servers" if page_i == 0 else "Servers (cont.)",
                    value="\n".join(lines[i:i+10]),
                    inline=False,
                )

        embed.set_footer(text=f"Griffith Music  •  discord.py {discord.__version__}", icon_url=bot.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    except Exception as e:
        print(f"/logbot error: {e}")
        err = rp(DANGER, description=str(e))
        try:
            await interaction.response.send_message(embed=err, ephemeral=True)
        except Exception:
            await interaction.followup.send(embed=err, ephemeral=True)

# ════════════════════════════════════════════════════════════════════════════
#  Run
# ════════════════════════════════════════════════════════════════════════════

if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable is not set.")
else:
    retry_delay = 5
    while True:
        try:
            print("Starting bot...")
            bot.run(TOKEN, reconnect=True, log_handler=None)
        except discord.errors.LoginFailure:
            print("Invalid token — check your DISCORD_TOKEN.")
            break
        except KeyboardInterrupt:
            print("Stopped.")
            break
        except Exception as e:
            print(f"Bot crashed: {e} — restarting in {retry_delay}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
            continue
        retry_delay = 5
