import os
import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

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