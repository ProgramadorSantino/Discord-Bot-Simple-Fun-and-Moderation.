# bot.py
import os
import random
import logging
import discord
from discord.ext import commands
from discord.ext.commands import BadArgument
from dotenv import load_dotenv

from logic import gen_pass, eight_ball, coin_flip, roll_dice

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("julian-bot")

# ---------- Custom emojis ----------
VICTORY = "<:VICTORY:1408236937424273529>"

# ---------- Env ----------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Put it in a .env file or your OS env vars.")

# ---------- Intents ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # needed for member converters & role mgmt

# ---------- Allowed mentions (no mass pings) ----------
allowed = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)

# ---------- Bot ----------
bot = commands.Bot(
    command_prefix="$",
    intents=intents,
    help_command=None,
    allowed_mentions=allowed,
)

# ---------- Small helpers ----------
def _can_act(invoker: discord.Member, target: discord.Member) -> bool:
    # Disallow acting on self or owner or equal/higher role
    if target == invoker or (invoker.guild and target == invoker.guild.owner):
        return False
    return invoker.top_role > target.top_role

def _role_height_ok(guild: discord.Guild, role: discord.Role) -> bool:
    return guild.me.top_role > role

async def _ensure_guild(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("This command only works in servers.")
        return False
    return True

# ---------- Events ----------
@bot.event
async def on_ready():
    log.info(f"✅ Logged in as {bot.user} (id: {bot.user.id})")
    activity = discord.Game(name="$help — now with ✨silliness✨")
    await bot.change_presence(activity=activity)

@bot.event
async def on_message(message: discord.Message):
    # ignore our own messages
    if message.author == bot.user:
        return

    # --- DMs: friendly chat behavior ---
    if isinstance(message.channel, discord.DMChannel):
        text = message.content.strip().lower()

        if text.startswith(("hi", "hello", "hola")):
            await message.channel.send(f"👋 ¡Hola! I’m alive in DMs too. Try `$help` for commands. {VICTORY}")
        elif text.startswith("$pass"):
            await message.channel.send(gen_pass(10) + f" {VICTORY}")
        else:
            await message.channel.send(
                f"You whispered: **{message.content}** {random.choice(['😸','🦄','✨','🌀','🍀', VICTORY])}"
            )
        await bot.process_commands(message)
        return

    # Let commands process for guild messages
    await bot.process_commands(message)

# ---------- Help ----------
@bot.command(name="help")
async def _help(ctx: commands.Context):
    # Only show admin section if the author is an admin in a guild text channel
    is_admin = False
    if isinstance(ctx.channel, discord.TextChannel):
        is_admin = ctx.author.guild_permissions.administrator

    lines = [
        "**Commands (prefix: `$`)**",
        "• `hello` → I say hi",
        "• `bye` → dramatic exit",
        "• `pass [len]` → strong password (8–64)",
        "• `8ball [question]` → cosmic wisdom 🎱",
        "• `flip` → coin flip",
        "• `roll [NdM]` → roll dice, e.g. `2d6`",
        "• `say <text>` → I repeat (pings disabled)",
        "• `ping` → latency",
    ]
    if is_admin:
        lines += [
            "",
            "__Admin only__",
            "• `kick @user [reason]`",
            "• `ban @user [reason]`",
            "• `purge <count>` (1–100)",
            "• `giverole @user <role>` / `removerole @user <role>`",
            "• `joined <member>`",
        ]
    lines += [
        "",
        f"Build: classic commands • Python {os.sys.version.split()[0]} • discord.py {discord.__version__}"
    ]
    await ctx.send("\n".join(lines))

# ---------- Fun / Silliness ----------
@bot.command(name="hello")
async def hello(ctx: commands.Context):
    await ctx.send(f"HELLOHS SIRMENS  {VICTORY}")

@bot.command(name="FUCKYOU")
async def insult(ctx: commands.Context):
    await ctx.send(f"No u {VICTORY}<a:RUN:1408589572312535121>")

@bot.command(name="bye")
async def bye(ctx: commands.Context):
    await ctx.send(f"Ok fine, dramatic exit in 3…2…1… <a:RUN:1408589572312535121> {VICTORY} {VICTORY} {VICTORY}")

@bot.command(name="pass")
@commands.cooldown(2, 5, commands.BucketType.user)
async def password(ctx: commands.Context, length: int = 10):
    await ctx.send(gen_pass(length))

@bot.command(name="8ball")
@commands.cooldown(2, 5, commands.BucketType.user)
async def eightball_cmd(ctx: commands.Context, *, question: str = ""):
    await ctx.send(f"🎱 {eight_ball()} {VICTORY} <a:RUN:1408589572312535121>")

@bot.command(name="flip")
@commands.cooldown(2, 5, commands.BucketType.user)
async def flip(ctx: commands.Context):
    await ctx.send(f"🪙 {coin_flip()}! {VICTORY} <a:RUN:1408589572312535121>")

@bot.command(name="roll")
@commands.cooldown(2, 5, commands.BucketType.user)
async def roll(ctx: commands.Context, dice: str = "1d6"):
    try:
        result, rolls = roll_dice(dice)
        await ctx.send(f"🎲 `{dice}` → {rolls} = **{result}**")
    except ValueError:
        await ctx.send("Usage: `$roll NdM` (e.g., `2d6`, `1d20`)")

@bot.command(name="say")
@commands.cooldown(2, 10, commands.BucketType.user)
async def say(ctx: commands.Context, *, text: str):
    # Stop mass pings
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    flair = random.choice(["✨", "🌈", "🎉", "🦄", "🍭"])
    await ctx.send(f"{text} {flair}", allowed_mentions=discord.AllowedMentions.none())

@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.send(f"Pong! {round(bot.latency*1000)} ms")

# ---------- Moderation ----------
@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await ctx.send("You can’t act on that member due to role hierarchy.")
    if ctx.guild.me.top_role <= member.top_role:
        return await ctx.send("My role is not high enough to kick that member. Move my role higher.")
    try:
        await member.kick(reason=reason)
        await ctx.send(f"👢 Kicked **{member}** — {reason}")
    except discord.Forbidden:
        await ctx.send("I don’t have permission to kick that user.")
    except Exception as e:
        await ctx.send(f"Kick failed: `{e}`")

@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await ctx.send("You can’t act on that member due to role hierarchy.")
    if ctx.guild.me.top_role <= member.top_role:
        return await ctx.send("My role is not high enough to ban that member. Move my role higher.")
    try:
        await member.ban(reason=reason)
        await ctx.send(f"🔨 Banned **{member}** — {reason}")
    except discord.Forbidden:
        await ctx.send("I don’t have permission to ban that user.")
    except Exception as e:
        await ctx.send(f"Ban failed: `{e}`")

@bot.command(name="purge")
@commands.max_concurrency(1, per=commands.BucketType.channel, wait=False)
@commands.has_permissions(manage_messages=True)
async def purge(ctx: commands.Context, count: int):
    if not await _ensure_guild(ctx):
        return
    if not (1 <= count <= 100):
        await ctx.send("Please choose a number between 1 and 100.")
        return
    deleted = await ctx.channel.purge(limit=count + 1)  # +1 includes the command message
    await ctx.send(f"🧹 Deleted {len(deleted) - 1} messages.", delete_after=3)

# ---------- Role Management ----------
@bot.command(name="giverole")
@commands.has_permissions(manage_roles=True)
async def giverole(ctx: commands.Context, member: discord.Member, *, role_name: str):
    if not await _ensure_guild(ctx):
        return
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if role is None:
        return await ctx.send(f"Role `{role_name}` not found.")
    if not _role_height_ok(ctx.guild, role):
        return await ctx.send("My role is not high enough to manage that role. Move my role higher.")
    try:
        await member.add_roles(role, reason=f"By {ctx.author} via bot")
        await ctx.send(f"✅ Added role **{role_name}** to {member.mention}")
    except discord.Forbidden:
        await ctx.send("❌ I don’t have permission to manage that role.")
    except Exception as e:
        await ctx.send(f"⚠️ Couldn’t assign role: `{e}`")

@bot.command(name="removerole")
@commands.has_permissions(manage_roles=True)
async def removerole(ctx: commands.Context, member: discord.Member, *, role_name: str):
    if not await _ensure_guild(ctx):
        return
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if role is None:
        return await ctx.send(f"Role `{role_name}` not found.")
    if not _role_height_ok(ctx.guild, role):
        return await ctx.send("My role is not high enough to manage that role. Move my role higher.")
    try:
        await member.remove_roles(role, reason=f"By {ctx.author} via bot")
        await ctx.send(f"✅ Removed role **{role_name}** from {member.mention}")
    except discord.Forbidden:
        await ctx.send("❌ I don’t have permission to manage that role.")
    except Exception as e:
        await ctx.send(f"⚠️ Couldn’t remove role: `{e}`")

# ---------- Admin: Joined Info ----------
@bot.command(name="joined")
@commands.has_permissions(administrator=True)
async def joined(ctx: commands.Context, *, member: discord.Member):
    if not await _ensure_guild(ctx):
        return

    joined_at = member.joined_at
    if joined_at:
        joined_abs = discord.utils.format_dt(joined_at, style="F")
        joined_rel = discord.utils.format_dt(joined_at, style="R")
    else:
        joined_abs = "Unknown"
        joined_rel = ""

    roles = [r for r in member.roles if r != ctx.guild.default_role]
    if roles:
        top = member.top_role if member.top_role != ctx.guild.default_role else None
        other = sorted([r for r in roles if r != top], key=lambda x: x.position, reverse=True)
        role_line = ", ".join([top.mention] + [r.mention for r in other]) if top else ", ".join(r.mention for r in other)
    else:
        role_line = "No roles"

    await ctx.send(
        f"**{member}** joined {joined_abs} ({joined_rel})\n"
        f"**Roles:** {role_line}"
    )

# ---------- Error Handler ----------
@bot.event
async def on_command_error(ctx: commands.Context, error):
    # Let command-local handlers run first
    if hasattr(ctx.command, 'on_error'):
        return

    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(f"Slow down! Try again in {error.retry_after:.1f}s.")
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("You’re missing permissions for that. 🛡️")
    if isinstance(error, commands.BotMissingPermissions):
        return await ctx.send("I’m missing permissions. Adjust my role or channel perms.")
    if isinstance(error, (commands.MemberNotFound, BadArgument)):
        return await ctx.send("I can’t find that member. Try mentioning them or use an exact name.")
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(f"Missing argument: `{error.param.name}`. Try `$help`.")

    log.exception("Unhandled command error", exc_info=error)
    await ctx.send(f"Unexpected error: `{error}` {VICTORY}")

# ---------- Run ----------
if TOKEN == "REPLACE_ME_WITH_ENV_VAR":
    raise SystemExit("Set DISCORD_TOKEN env var instead of hardcoding your token.")

bot.run(TOKEN)







