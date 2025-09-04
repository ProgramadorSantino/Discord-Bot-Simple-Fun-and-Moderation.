# bot.py
import os
import re
import random
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from discord.ext.commands import BadArgument
from dotenv import load_dotenv

import aiohttp
from logic import gen_pass, eight_ball, coin_flip, roll_dice

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("julian-bot")

# ---------- Custom emojis ----------
VICTORY = "<:VICTORY:1408236937424273529>"
RUN = "<a:RUN:1408589572312535121>"

# number emoji list for polls: supports up to 10 options
NUM_EMOJIS = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]

# ---------- Simple in-memory stores ----------
# warnings[(guild_id, user_id)] = list of dicts: {'reason': str, 'by': int, 'at': datetime}
warnings_store = {}

# track scheduled unmutes so we don't duplicate (optional convenience)
scheduled_unmutes = {}  # key: (guild_id, user_id) -> asyncio.Task

# ---------- Env ----------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Put it in a .env file or your OS env vars.")

# Directory for images (you can override with IMAGES_DIR env var)
from pathlib import Path
BASE_DIR = Path(__file__).parent.resolve()
IMAGES_DIR = Path(os.getenv("IMAGES_DIR", BASE_DIR / "images")).resolve()
VALID_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_image_cache: list[Path] = []

def _refresh_image_cache() -> int:
    global _image_cache
    if not IMAGES_DIR.exists():
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    _image_cache = sorted([p for p in IMAGES_DIR.iterdir() if p.suffix.lower() in VALID_EXTS])
    log.info(f"[MEME] Loaded {len(_image_cache)} image(s) from {IMAGES_DIR}")
    return len(_image_cache)

def _pick_images(k: int = 1) -> list[Path]:
    if not _image_cache:
        return []
    k = max(1, min(k, 4))  # cap to 4 to avoid huge payloads
    if len(_image_cache) >= k:
        return random.sample(_image_cache, k=k)
    return [random.choice(_image_cache) for _ in range(k)]

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

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def parse_duration_to_seconds(text: str) -> int:
    """
    Parse duration strings like '15m', '2h', '1h30m', '3d', '1w2d', '45s'
    Returns total seconds (int). Raises ValueError if invalid.
    """
    text = text.strip().lower().replace(" ", "")
    if not text:
        raise ValueError("empty duration")

    pattern = r"(\d+)([smhdw])"
    units = {"s":1, "m":60, "h":3600, "d":86400, "w":604800}
    total = 0
    for amount, unit in re.findall(pattern, text):
        total += int(amount) * units[unit]
    if total == 0:
        raise ValueError("invalid duration")
    return total

async def ensure_muted_role(guild: discord.Guild) -> discord.Role:
    """
    Get or create a 'Muted' role with safe channel overwrites.
    """
    role = discord.utils.get(guild.roles, name="Muted")
    if role:
        return role

    # Create role with no special perms; channel overwrites will do the heavy lifting
    role = await guild.create_role(name="Muted", reason="Auto-created for mute command")
    # Apply channel overwrites
    overwrite = discord.PermissionOverwrite(send_messages=False, add_reactions=False, speak=False, connect=False)
    for channel in guild.channels:
        try:
            await channel.set_permissions(role, overwrite=overwrite)
        except Exception:
            pass
    return role

async def schedule_unmute(guild_id: int, user_id: int, seconds: int, reason: str = "Timed mute expired"):
    """
    Background scheduler to unmute after N seconds.
    """
    key = (guild_id, user_id)
    old = scheduled_unmutes.get(key)
    if old and not old.done():
        old.cancel()

    async def _task():
        try:
            await asyncio.sleep(seconds)
            guild = bot.get_guild(guild_id)
            if not guild:
                return
            member = guild.get_member(user_id)
            if not member:
                return
            role = discord.utils.get(guild.roles, name="Muted")
            if role and role in member.roles:
                await member.remove_roles(role, reason=reason)
                channel = guild.system_channel or discord.utils.get(guild.text_channels)
                if channel:
                    await channel.send(f"🔈 Auto-unmuted {member.mention} — mute expired. {VICTORY}")
        except asyncio.CancelledError:
            pass
        finally:
            scheduled_unmutes.pop(key, None)

    t = asyncio.create_task(_task())
    scheduled_unmutes[key] = t

# ---------- Dynamic cooldown (mods bypass) ----------
def _is_mod(ctx: commands.Context) -> bool:
    # Admins or users with common mod perms bypass image cooldowns
    perms = getattr(ctx.author, "guild_permissions", None)
    if not perms:
        return False
    return perms.administrator or perms.manage_messages or perms.kick_members or perms.ban_members

def image_dynamic_cooldown(ctx: commands.Context) -> commands.Cooldown:
    """
    5s per-user cooldown for image commands; mods bypass (0s).
    """
    if _is_mod(ctx):
        return commands.Cooldown(1, 0, commands.BucketType.user)  # no wait
    return commands.Cooldown(1, 5, commands.BucketType.user)

# ---------- DUCK fetcher (non-blocking wrapper) ----------
async def get_duck_image_url() -> str:
    """
    Calls https://random-d.uk/api/random using aiohttp (async).
    Returns the image URL or empty string on failure.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://random-d.uk/api/random", timeout=8) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get("url", "")
    except Exception as e:
        log.warning(f"[DUCK] fetch failed: {e}")
        return ""


# ---------- Events ----------
@bot.event
async def on_ready():
    log.info(f"✅ Logged in as {bot.user} (id: {bot.user.id})")
    count = _refresh_image_cache()  # load or reload images at startup
    log.info(f"📸 Meme image cache ready with {count} file(s) in {IMAGES_DIR}")
    activity = discord.Game(name="$help — now with ✨silliness✨")
    await bot.change_presence(activity=activity)

@bot.event
async def on_message(message: discord.Message):
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

    await bot.process_commands(message)

# ---------- Help ----------
@bot.command(name="help")
async def _help(ctx: commands.Context):
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
        "• `avatar [@user]` → show profile pic",
        "• `userinfo [@user]` → basic user info",
        "• `serverinfo` → server stats",
        "• `choose option1 | option2 | ...` → I pick one",
        "• `poll \"Question\" opt1 | opt2 | ...` → reaction poll (up to 10)",
        "• `remindme <time> <message>` → DM reminder, e.g., `remindme 15m drink water`",
        "• `dadjoke` → so bad it’s good 😅",
        "• `DUCK` → a random duck photo 🦆",
        "• `MEME [count]` → random image(s) from /images (1–4)",
        "• `memevs` → two random images to vote 1️⃣/2️⃣",
    ]
    if is_admin:
        lines += [
            "",
            "__Admin/Mod only__",
            "• `kick @user [reason]` / `ban @user [reason]`",
            "• `purge <count>` (1–100)",
            "• `giverole @user <role>` / `removerole @user <role>`",
            "• `joined <member>`",
            "• `warn @user [reason]` / `warnings @user` / `clearwarnings @user`",
            "• `slowmode <seconds>`",
            "• `lockdown [reason]` / `unlock` (current channel)",
            "• `nickname @user <new name>`",
            "• `mute @user [time]` / `unmute @user` (creates Muted role if missing)",
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
    await ctx.send(f"No u {VICTORY} {RUN}")

@bot.command(name="THEYTOOKMYFAMILY")
async def kidnapping(ctx: commands.Context):
    await ctx.send(f"HAHAHA NUB {RUN} {RUN}")

@bot.command(name="bye")
async def bye(ctx: commands.Context):
    await ctx.send(f"Ok fine, dramatic exit in 3…2…1…  {RUN} {VICTORY} {VICTORY} {VICTORY}")

@bot.command(name="pass")
@commands.cooldown(2, 5, commands.BucketType.user)
async def password(ctx: commands.Context, length: int = 10):
    await ctx.send(gen_pass(length))

@bot.command(name="8ball")
@commands.cooldown(2, 5, commands.BucketType.user)
async def eightball_cmd(ctx: commands.Context, *, question: str = ""):
    await ctx.send(f"🎱 {eight_ball()} {VICTORY} {RUN}")

@bot.command(name="flip")
@commands.cooldown(2, 5, commands.BucketType.user)
async def flip(ctx: commands.Context):
    await ctx.send(f"🪙 {coin_flip()}! {VICTORY} {RUN}")

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
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    flair = random.choice(["✨", "🌈", "🎉", "🦄", "🍭"])
    await ctx.send(f"{text} {flair}", allowed_mentions=discord.AllowedMentions.none())

@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.send(f"Pong! {round(bot.latency*1000)} ms")

# ---------- NEW: Image Commands (5s cooldown; mods bypass) ----------
@bot.command(name="meme", aliases=["MEME"])
@commands.dynamic_cooldown(image_dynamic_cooldown, commands.BucketType.user)
async def meme(ctx: commands.Context, count: int = 1):
    """
    $MEME [count]
    Sends 1–4 random images from the images folder. Case-insensitive.
    """
    if not _image_cache:
        await ctx.send(f"No images found in `{IMAGES_DIR}`. Add files and try again.")
        return
    count = max(1, min(int(count), 4))
    picks = _pick_images(count)
    files = []
    for p in picks:
        try:
            files.append(discord.File(fp=str(p), filename=p.name))
        except Exception as e:
            log.warning(f"[MEME] Could not attach {p}: {e}")
    if not files:
        return await ctx.send("Couldn’t attach any image files. Check permissions/paths.")
    await ctx.send(content=f"Here you go {ctx.author.mention}! {VICTORY}", files=files)

@bot.command(name="memevs")
@commands.dynamic_cooldown(image_dynamic_cooldown, commands.BucketType.user)
async def memevs(ctx: commands.Context):
    """
    $memevs
    Sends two random images in one message so people can vote 1️⃣ or 2️⃣.
    """
    if not _image_cache:
        await ctx.send(f"No images found in `{IMAGES_DIR}`. Add files and try again.")
        return
    picks = _pick_images(2)
    files = []
    for p in picks:
        try:
            files.append(discord.File(fp=str(p), filename=p.name))
        except Exception as e:
            log.warning(f"[MEME] Could not attach {p}: {e}")
    if not files:
        return await ctx.send("Couldn’t attach images. Check permissions/paths.")
    msg = await ctx.send(content=f"**Meme Battle!** React to vote: 1️⃣ or 2️⃣ {VICTORY}", files=files)
    try:
        await msg.add_reaction("1️⃣")
        await msg.add_reaction("2️⃣")
    except Exception:
        pass

@bot.command(name="DUCK", aliases=["duck"])
@commands.dynamic_cooldown(image_dynamic_cooldown, commands.BucketType.user)
async def duck(ctx: commands.Context):
    """
    $DUCK  → Fetches a random duck image (🦆) from random-d.uk
    Mods bypass cooldown; others 5s per-user.
    """
    url = await get_duck_image_url()
    if not url:
        return await ctx.send("Couldn’t fetch a duck right now. Try again in a moment.")
    # send as embed (nicer than plain link)
    embed = discord.Embed(title="🦆 Quack!", color=0x00ccff)
    embed.set_image(url=url)
    await ctx.send(embed=embed)

# ---------- Member Utilities ----------
@bot.command(name="avatar")
async def avatar(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    await ctx.send(member.display_avatar.url if member.display_avatar else "No avatar.")

@bot.command(name="userinfo")
async def userinfo(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    created = discord.utils.format_dt(member.created_at, style="F")
    joined = discord.utils.format_dt(member.joined_at, style="F") if member.joined_at else "Unknown"
    embed = discord.Embed(title=f"User Info — {member}", color=0x00ccff)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.add_field(name="Top Role", value=member.top_role.mention if member.top_role else "None", inline=True)
    embed.add_field(name="Account Created", value=created, inline=False)
    embed.add_field(name="Joined Server", value=joined, inline=False)
    await ctx.send(embed=embed)

@bot.command(name="serverinfo")
async def serverinfo(ctx: commands.Context):
    if not await _ensure_guild(ctx):
        return
    guild = ctx.guild
    embed = discord.Embed(title=f"{guild.name} — Server Info", color=0x00ffcc)
    embed.set_thumbnail(url=guild.icon.url if guild.icon else discord.Embed.Empty)
    embed.add_field(name="Owner", value=str(guild.owner), inline=False)
    embed.add_field(name="Members", value=guild.member_count, inline=True)
    embed.add_field(name="Boost Level", value=guild.premium_tier, inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, style="F"), inline=False)
    await ctx.send(embed=embed)

@bot.command(name="choose")
async def choose(ctx: commands.Context, *, options: str):
    parts = [p.strip() for p in options.split("|") if p.strip()]
    if len(parts) < 2:
        return await ctx.send("Give me at least two options, like: `$choose tacos | sushi | pizza`")
    pick = random.choice(parts)
    await ctx.send(f"I choose: **{pick}** {VICTORY}")

@bot.command(name="poll")
async def poll(ctx: commands.Context, *, text: str):
    m = re.match(r'"\s*(.+?)\s*"\s*(.+)', text)
    if not m:
        return await ctx.send('Format: `$poll "Question" option1 | option2 | option3`')
    question, options = m.groups()
    opts = [o.strip() for o in options.split("|") if o.strip()]
    if not (2 <= len(opts) <= 10):
        return await ctx.send("Give me between 2 and 10 options, separated by `|`.")
    desc = "\n".join(f"{NUM_EMOJIS[i]}  {opt}" for i, opt in enumerate(opts))
    embed = discord.Embed(title=f"📊 {question}", description=desc, color=0x7289DA)
    embed.set_footer(text=f"Requested by {ctx.author}")
    msg = await ctx.send(embed=embed)
    for i in range(len(opts)):
        try:
            await msg.add_reaction(NUM_EMOJIS[i])
        except Exception:
            pass

@bot.command(name="remindme")
async def remindme(ctx: commands.Context, time: str, *, message: str):
    try:
        seconds = parse_duration_to_seconds(time)
    except ValueError:
        return await ctx.send("Time format invalid. Try like `10m`, `2h30m`, `3d`.")
    await ctx.send(f"⏰ Reminder set for {time}: **{message}** {VICTORY}")
    async def _remind():
        await asyncio.sleep(seconds)
        try:
            await ctx.author.send(f"⏰ Reminder from **{ctx.guild.name if ctx.guild else 'DM'}**: {message}")
        except discord.Forbidden:
            await ctx.send(f"{ctx.author.mention} ⏰ Reminder: {message}")
    asyncio.create_task(_remind())

@bot.command(name="dadjoke")
async def dadjoke(ctx: commands.Context):
    jokes = [
        "I would tell you a construction joke, but I’m still working on it.",
        "Why did the scarecrow get promoted? He was outstanding in his field. {RUN}",
        "I used to hate facial hair… but then it grew on me.",
        "Why don’t eggs tell jokes? They’d crack each other up.",
    ]
    await ctx.send(random.choice(jokes) + f" {VICTORY}")

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

# ---- Warnings system ----
@bot.command(name="warn")
@commands.has_permissions(manage_messages=True)
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await ctx.send("You can’t warn that member due to role hierarchy.")
    key = (ctx.guild.id, member.id)
    warnings_store.setdefault(key, []).append({"reason": reason, "by": ctx.author.id, "at": _now_utc()})
    try:
        await member.send(f"⚠️ You’ve been warned in **{ctx.guild.name}**: {reason}")
    except discord.Forbidden:
        pass
    await ctx.send(f"⚠️ Warned {member.mention} — {reason}")

@bot.command(name="warnings")
@commands.has_permissions(manage_messages=True)
async def warnings_cmd(ctx: commands.Context, member: discord.Member):
    if not await _ensure_guild(ctx):
        return
    key = (ctx.guild.id, member.id)
    entries = warnings_store.get(key, [])
    if not entries:
        return await ctx.send(f"{member.mention} has no warnings. {VICTORY}")
    lines = [f"Warnings for **{member}**:"]
    for i, w in enumerate(entries, 1):
        when = discord.utils.format_dt(w["at"], style="R")
        mod = ctx.guild.get_member(w["by"])
        lines.append(f"{i}. {w['reason']} — by {mod.mention if mod else w['by']} ({when})")
    await ctx.send("\n".join(lines))

@bot.command(name="clearwarnings")
@commands.has_permissions(manage_messages=True)
async def clearwarnings(ctx: commands.Context, member: discord.Member):
    if not await _ensure_guild(ctx):
        return
    key = (ctx.guild.id, member.id)
    count = len(warnings_store.get(key, []))
    warnings_store.pop(key, None)
    await ctx.send(f"🧽 Cleared **{count}** warnings for {member.mention}.")

# ---- slowmode / lockdown / unlock / nickname / mute / unmute ----
@bot.command(name="slowmode")
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx: commands.Context, seconds: int):
    if not await _ensure_guild(ctx):
        return
    seconds = max(0, min(seconds, 21600))  # 6 hours cap
    await ctx.channel.edit(slowmode_delay=seconds, reason=f"By {ctx.author}")
    if seconds == 0:
        await ctx.send("⏱️ Slowmode disabled for this channel.")
    else:
        await ctx.send(f"⏱️ Slowmode set to **{seconds}s**.")

@bot.command(name="lockdown")
@commands.has_permissions(manage_channels=True)
async def lockdown(ctx: commands.Context, *, reason: str = "Lockdown"):
    if not await _ensure_guild(ctx):
        return
    everyone = ctx.guild.default_role
    overwrites = ctx.channel.overwrites_for(everyone)
    overwrites.send_messages = False
    try:
        await ctx.channel.set_permissions(everyone, overwrite=overwrites, reason=f"{reason} — by {ctx.author}")
        await ctx.send(f"🔒 Channel locked. Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("I don’t have permission to lock this channel.")

@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context):
    if not await _ensure_guild(ctx):
        return
    everyone = ctx.guild.default_role
    overwrites = ctx.channel.overwrites_for(everyone)
    overwrites.send_messages = None  # reset to default
    try:
        await ctx.channel.set_permissions(everyone, overwrite=overwrites, reason=f"Unlock by {ctx.author}")
        await ctx.send("🔓 Channel unlocked.")
    except discord.Forbidden:
        await ctx.send("I don’t have permission to unlock this channel.")

@bot.command(name="nickname")
@commands.has_permissions(manage_nicknames=True)
async def nickname(ctx: commands.Context, member: discord.Member, *, new_name: str):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await ctx.send("You can’t change that member’s nickname due to role hierarchy.")
    try:
        await member.edit(nick=new_name, reason=f"By {ctx.author}")
        await ctx.send(f"✏️ Nickname changed for {member.mention} → **{new_name}**")
    except discord.Forbidden:
        await ctx.send("I don’t have permission to change that nickname.")

@bot.command(name="mute")
@commands.has_permissions(moderate_members=True, manage_roles=True)
async def mute(ctx: commands.Context, member: discord.Member, duration: str = None, *, reason: str = "Muted"):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await ctx.send("You can’t mute that member due to role hierarchy.")
    role = await ensure_muted_role(ctx.guild)
    if role in member.roles:
        return await ctx.send("They’re already muted.")
    try:
        await member.add_roles(role, reason=f"{reason} — by {ctx.author}")
        await ctx.send(f"🔇 Muted {member.mention}. {('Duration: ' + duration) if duration else ''}")
    except discord.Forbidden:
        return await ctx.send("I don’t have permission to add the Muted role.")

    if duration:
        try:
            seconds = parse_duration_to_seconds(duration)
            await schedule_unmute(ctx.guild.id, member.id, seconds)
        except ValueError:
            await ctx.send("Duration format invalid. Use like `10m`, `2h30m`, `3d`.")

@bot.command(name="unmute")
@commands.has_permissions(moderate_members=True, manage_roles=True)
async def unmute(ctx: commands.Context, member: discord.Member):
    if not await _ensure_guild(ctx):
        return
    role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not role or role not in member.roles:
        return await ctx.send("That member is not muted.")
    try:
        await member.remove_roles(role, reason=f"Unmuted by {ctx.author}")
        key = (ctx.guild.id, member.id)
        task = scheduled_unmutes.pop(key, None)
        if task and not task.done():
            task.cancel()
        await ctx.send(f"🔈 Unmuted {member.mention}. {VICTORY}")
    except discord.Forbidden:
        await ctx.send("I don’t have permission to remove the Muted role.")

# ---------- Error Handler ----------
@bot.event
async def on_command_error(ctx: commands.Context, error):
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
