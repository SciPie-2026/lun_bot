import os
import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
RECONNECT_INTERVAL = int(os.getenv("RECONNECT_INTERVAL_SECONDS", "20"))
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set. Add it to your .env file.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("vc-keepalive")

intents = discord.Intents.default()
intents.message_content = True  # required to read "!lun_join"
intents.voice_states = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# Tracks which channel we're supposed to be keeping alive, per guild.
# { guild_id: voice_channel_id }
target_channels: dict[int, int] = {}

# ---------------------------------------------------------------------------
# Music (yt-dlp + ffmpeg)
# ---------------------------------------------------------------------------
COOKIES_ENV_VALUE = os.getenv("YTDLP_COOKIES")
COOKIES_FILE_PATH = "/tmp/youtube_cookies.txt"

if COOKIES_ENV_VALUE:
    with open(COOKIES_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(COOKIES_ENV_VALUE)
    log.info("Loaded YouTube cookies from YTDLP_COOKIES env var")

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",  # avoids IPv6 issues on some hosts
}

if COOKIES_ENV_VALUE:
    YTDL_OPTIONS["cookiefile"] = COOKIES_FILE_PATH

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


async def search_audio(query: str):
    """Runs yt-dlp's (blocking) extract_info in a thread and returns (stream_url, title)."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(
        None, lambda: ytdl.extract_info(f"ytsearch:{query}", download=False)
    )
    if "entries" in data:
        data = data["entries"][0]
    return data["url"], data.get("title", "Unknown title")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
async def join_voice_channel(guild: discord.Guild):
    """Attempt to join the tracked channel for this guild, if not already connected."""
    channel_id = target_channels.get(guild.id)
    if channel_id is None:
        return  # nothing to keep alive here yet

    channel = guild.get_channel(channel_id)
    if channel is None:
        log.warning("Tracked channel %s no longer exists in guild %s", channel_id, guild.name)
        target_channels.pop(guild.id, None)
        return

    vc = guild.voice_client
    if vc is not None and vc.is_connected():
        return  # already connected

    try:
        await channel.connect(reconnect=True, self_mute=False, self_deaf=False)
        log.info("Joined voice channel: %s (%s)", channel.name, guild.name)
    except Exception as e:
        log.error("Failed to join voice channel: %s", e)


async def keep_alive():
    """Background loop that keeps re-checking tracked voice connections."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            await join_voice_channel(guild)
        await asyncio.sleep(RECONNECT_INTERVAL)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@bot.command(name="lun_join")
async def lun_join(ctx: commands.Context):
    """Joins the voice channel the command author is currently in, and keeps it alive."""
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.send("You need to be in a voice channel first.")
        return

    channel = ctx.author.voice.channel
    target_channels[ctx.guild.id] = channel.id

    vc = ctx.guild.voice_client
    if vc is not None and vc.is_connected():
        if vc.channel.id == channel.id:
            await ctx.send(f"Already in **{channel.name}**.")
            return
        await vc.move_to(channel)
        await ctx.send(f"Moved to **{channel.name}**.")
        return

    await join_voice_channel(ctx.guild)
    await ctx.send(f"Joined **{channel.name}** and will stay connected.")


async def ensure_connected(channel: discord.VoiceChannel):
    """Returns a live, connected VoiceClient for this channel's guild —
    cleaning up any stale/dead connection object first if needed."""
    vc = channel.guild.voice_client
    if vc is not None and not vc.is_connected():
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        vc = None

    if vc is None:
        vc = await channel.connect(reconnect=True, self_mute=False, self_deaf=False)
        target_channels[channel.guild.id] = channel.id

    return vc


@bot.command(name="lun_play")
async def lun_play(ctx: commands.Context, *, query: str):
    """Searches YouTube for `query` and plays the audio in your voice channel."""
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.send("Join a voice channel first.")
        return
    channel = ctx.author.voice.channel

    vc = await ensure_connected(channel)

    async with ctx.typing():
        try:
            stream_url, title = await search_audio(query)

            # Re-check right before playing — the search above can take a few
            # seconds, enough for a flaky voice connection to have dropped.
            vc = await ensure_connected(channel)

            if vc.is_playing() or vc.is_paused():
                vc.stop()

            def after_playback(error):
                if error:
                    log.error("Playback error: %s", error)

            source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
            vc.play(source, after=after_playback)
        except Exception as e:
            log.error("lun_play failed: %s", e, exc_info=True)
            await ctx.send(f"Something went wrong trying to play that: `{e}`")
            return

    await ctx.send(f"Now playing: **{title}**")


@bot.command(name="lun_stop")
async def lun_stop(ctx: commands.Context):
    """Stops whatever is currently playing (bot stays in the voice channel)."""
    vc = ctx.guild.voice_client
    if vc is not None and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await ctx.send("Stopped playback.")
    else:
        await ctx.send("Nothing is playing.")


@bot.command(name="lun_leave")
async def lun_leave(ctx: commands.Context):
    """Leaves the voice channel and stops keeping it alive for this server."""
    target_channels.pop(ctx.guild.id, None)
    vc = ctx.guild.voice_client
    if vc is not None:
        await vc.disconnect()
        await ctx.send("Left the voice channel.")
    else:
        await ctx.send("Not currently in a voice channel.")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    bot.loop.create_task(keep_alive())
    await bot.change_presence(activity=discord.CustomActivity("Ajao Na.."))


@bot.event
async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id and after.channel is None:
        # We got disconnected — only rejoin if we're still tracking a channel for this guild
        if member.guild.id in target_channels:
            log.info("Disconnected from voice, rejoining...")
            await join_voice_channel(member.guild)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
