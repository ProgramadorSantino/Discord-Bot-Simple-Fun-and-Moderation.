# bot.py
import os
import re
import random
import logging
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

from PIL import Image
import pytesseract
import pyttsx3

import discord
from discord.ext import commands
from discord.ext.commands import BadArgument
from dotenv import load_dotenv

import aiohttp
import cv2
from logic import gen_pass, eight_ball, coin_flip, roll_dice




pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
FFMPEG_EXE = r"C:\Users\santi\Downloads\ffmpeg-8.1-essentials_build\ffmpeg-8.1-essentials_build\bin\ffmpeg.exe"

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("COOl BOT")

# ---------- Custom emojis ----------
VICTORY = "<:VICTORY:1408236937424273529>"
RUN = "<a:RUN:1408589572312535121>"
NUM_EMOJIS = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]

# ---------- In-memory stores ----------
warnings_store   = {}   # {(guild_id, user_id): [{'reason', 'by', 'at'}]}
scheduled_unmutes = {}  # {(guild_id, user_id): asyncio.Task}
scheduled_unbans  = {}  # {(guild_id, user_id): asyncio.Task}
afk_users         = {}  # {user_id: reason}
mod_log_store     = []  # list of action dicts

# ---------- Env ----------
load_dotenv()
TOKEN    = os.getenv("DISCORD_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")
HF_API_URL = "https://api-inference.huggingface.co/models/google/vit-base-patch16-224"

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Put it in a .env file or your OS env vars.")

BASE_DIR   = Path(__file__).parent.resolve()
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
    k = max(1, min(k, 4))
    if len(_image_cache) >= k:
        return random.sample(_image_cache, k=k)
    return [random.choice(_image_cache) for _ in range(k)]

async def fact_extractor()-> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://uselessfacts.jsph.pl/random.json?language=en", timeout=8) as resp:
                resp.raise_for_status()
                return (await resp.json()).get("text", "No fact found.")
    except Exception as e:
        log.warning(f"[FACT] fetch failed: {e}")
        return "Couldn't fetch a fact right now."

# ---------- Intents ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

allowed = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)

bot = commands.Bot(
    command_prefix="$",
    intents=intents,
    help_command=None,
    allowed_mentions=allowed,
)

# ---------- Small helpers ----------
def _can_act(invoker: discord.Member, target: discord.Member) -> bool:
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

def _log_mod_action(guild_id: int, mod_id: int, action: str, target: str, reason: str):
    mod_log_store.append({
        "action": action, "mod_id": mod_id, "target": target,
        "reason": reason, "at": _now_utc(), "guild_id": guild_id,
    })
    if len(mod_log_store) > 500:
        mod_log_store.pop(0)

async def _try_delete(ctx: commands.Context):
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass

async def _private_reply(ctx: commands.Context, content: str = None, embed: discord.Embed = None, **kwargs):
    """Delete command message and DM the result. Falls back to channel with delete_after."""
    await _try_delete(ctx)
    try:
        await ctx.author.send(content=content, embed=embed, **kwargs)
    except discord.Forbidden:
        await ctx.send(content=content, embed=embed, delete_after=15, **kwargs)

async def _mod_reply(ctx: commands.Context, content: str = None, embed: discord.Embed = None, **kwargs):
    """Delete command message (hides mod's command) and post result publicly."""
    await _try_delete(ctx)
    await ctx.send(content=content, embed=embed, **kwargs)

def parse_duration_to_seconds(text: str) -> int:
    text = text.strip().lower().replace(" ", "")
    if not text:
        raise ValueError("empty duration")
    pattern = r"(\d+)([smhdw])"
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    total = sum(int(a) * units[u] for a, u in re.findall(pattern, text))
    if total == 0:
        raise ValueError("invalid duration")
    return total

async def ensure_muted_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name="Muted")
    if role:
        return role
    role = await guild.create_role(name="Muted", reason="Auto-created for mute command")
    overwrite = discord.PermissionOverwrite(send_messages=False, add_reactions=False, speak=False, connect=False)
    for channel in guild.channels:
        try:
            await channel.set_permissions(role, overwrite=overwrite)
        except Exception:
            pass
    return role

def image_has_face(path: Path) -> bool:
    face = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    img = cv2.imread(str(path))
    if img is None:
        return False
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return len(face.detectMultiScale(gray, 1.3, 5)) > 0

async def schedule_unmute(guild_id: int, user_id: int, seconds: int, reason: str = "Timed mute expired"):
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

    scheduled_unmutes[key] = asyncio.create_task(_task())

async def schedule_unban(guild_id: int, user_id: int, seconds: int):
    key = (guild_id, user_id)
    old = scheduled_unbans.get(key)
    if old and not old.done():
        old.cancel()

    async def _task():
        try:
            await asyncio.sleep(seconds)
            guild = bot.get_guild(guild_id)
            if not guild:
                return
            try:
                await guild.unban(discord.Object(id=user_id), reason="Temp ban expired")
                channel = guild.system_channel or discord.utils.get(guild.text_channels)
                if channel:
                    await channel.send(f"🔓 Auto-unbanned user ID `{user_id}` — temp ban expired.")
            except discord.NotFound:
                pass
        except asyncio.CancelledError:
            pass
        finally:
            scheduled_unbans.pop(key, None)

    scheduled_unbans[key] = asyncio.create_task(_task())

# ---------- Image Moderation ----------
BANNED_WORDS = {"Nigga", "NIGGA", "CP", "cheesepiza", "slut", "SLUT"}

def extract_text_from_image(path: Path) -> str:
    try:
        return pytesseract.image_to_string(Image.open(path)).lower()
    except Exception as e:
        log.warning(f"[OCR] Failed: {e}")
        return ""

def contains_banned_text(text: str) -> bool:
    return any(bad in text for bad in BANNED_WORDS)

def looks_like_meme(text: str) -> bool:
    t = text.strip()
    return len(t) >= 6 and " " in t

def _is_mod(ctx: commands.Context) -> bool:
    perms = getattr(ctx.author, "guild_permissions", None)
    if not perms:
        return False
    return perms.administrator or perms.manage_messages or perms.kick_members or perms.ban_members

def image_dynamic_cooldown(ctx: commands.Context) -> commands.Cooldown:
    if _is_mod(ctx):
        return commands.Cooldown(1, 0)
    return commands.Cooldown(1, 5)

async def is_meme_image(path: Path) -> bool:
    if not HF_TOKEN:
        log.warning("[MEME] HF_TOKEN missing — using OCR heuristic fallback")
        return looks_like_meme(extract_text_from_image(path))

    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            with open(path, "rb") as f:
                data = f.read()
            async with session.post(HF_API_URL, headers=headers, data=data, timeout=20) as resp:
                if resp.status != 200:
                    return looks_like_meme(extract_text_from_image(path))
                result = await resp.json()

        if not isinstance(result, list) or not result:
            return looks_like_meme(extract_text_from_image(path))

        best = max(result, key=lambda x: x["score"])
        label, score = best["label"].lower(), float(best["score"])
        log.info(f"[MEME MODEL] {label} {score:.2f}")

        memeish_keywords = (
            "comic","cartoon","website","poster","collage","screen",
            "monitor","scoreboard","menu","face","portrait","selfie",
            "mfs","skibidi","brainrot"
        )
        if any(k in label for k in memeish_keywords) or score >= 0.70:
            return True
        return looks_like_meme(extract_text_from_image(path))
    except Exception as e:
        log.warning(f"[HF meme check failed] {e}")
        return looks_like_meme(extract_text_from_image(path))

# ---------- Duck fetcher ----------
async def get_duck_image_url() -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://random-d.uk/api/random", timeout=8) as resp:
                resp.raise_for_status()
                return (await resp.json()).get("url", "")
    except Exception as e:
        log.warning(f"[DUCK] fetch failed: {e}")
        return ""

# ---------- TTS helper ----------
def _tts_to_file(text: str, filepath: str) -> None:
    """Synchronous — run in executor."""
    engine = pyttsx3.init()
    engine.setProperty("rate", 160)
    engine.setProperty("volume", 1.0)
    voices = engine.getProperty("voices")
    if voices:
        engine.setProperty("voice", voices[0].id)
    engine.save_to_file(text, filepath)
    engine.runAndWait()
    engine.stop()

# ---------- Events ----------
@bot.event
async def on_ready():
    log.info(f"✅ Logged in as {bot.user} (id: {bot.user.id})")
    count = _refresh_image_cache()
    log.info(f"📸 Meme cache: {count} file(s) in {IMAGES_DIR}")
    await bot.change_presence(activity=discord.Game(name="$help | now with voice 🔊"))

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # AFK: clear status when the AFK user speaks
    if message.author.id in afk_users and not message.content.startswith("$"):
        afk_users.pop(message.author.id)
        try:
            await message.channel.send(
                f"👋 Welcome back {message.author.mention}! AFK removed.", delete_after=5
            )
        except Exception:
            pass

    # AFK: notify if someone pings an AFK user
    for mentioned in message.mentions:
        if mentioned.id in afk_users and mentioned.id != message.author.id:
            try:
                await message.channel.send(
                    f"💤 {mentioned.mention} is AFK: **{afk_users[mentioned.id]}**",
                    delete_after=10,
                )
            except Exception:
                pass

    # DM handling
    if isinstance(message.channel, discord.DMChannel):
        text = message.content.strip().lower()
        if text.startswith(("hi", "hello", "hola")):
            await message.channel.send(f"👋 ¡Hola! Try `$help` for commands. {VICTORY}")
        elif text.startswith("$pass"):
            await message.channel.send(gen_pass(10) + f" {VICTORY}")
        else:
            await message.channel.send(
                f"You whispered: **{message.content}** {random.choice(['😸','🦄','✨','🌀','🍀',VICTORY])}"
            )
        await bot.process_commands(message)
        return

    await bot.process_commands(message)

# ---------- Help ----------
@bot.command(name="help")
async def _help(ctx: commands.Context):
    is_mod = False
    if isinstance(ctx.channel, discord.TextChannel):
        p = ctx.author.guild_permissions
        is_mod = p.administrator or p.manage_messages or p.kick_members or p.ban_members

    lines = [
        "**Commands (prefix: `$`)**",
        "• `hello` / `bye` → greetings",
        "• `pass [len]` → strong password *(DM'd)*",
        "• `8ball [q]` → cosmic wisdom 🎱",
        "• `flip` → coin flip | `roll [NdM]` → dice",
        "• `say <text>` → I repeat (pings disabled)",
        "• `ping` → latency",
        "• `avatar [@user]` → profile pic *(DM'd)*",
        "• `userinfo [@user]` → user info *(DM'd)*",
        "• `serverinfo` → server stats",
        "• `roleinfo <role>` → role details",
        "• `choose a | b | ...` → I pick one",
        "• `poll \"Q\" a | b | ...` → reaction poll",
        "• `remindme <time> <msg>` → DM reminder",
        "• `dadjoke` → groan 😅",
        "• `afk [reason]` → set AFK status",
        "• `color #hex` → preview a color",
        "• `weather <city>` → current weather + 3-day forecast 🌤️",
        "• `DUCK` 🦆 | `MEME [n]` | `memevs` | `uploadimage`",
        "• `randomfact` → get a random fact from the internet",
        "",
       
       
    ]
    if is_mod:
        lines += [
            "",
            "__Mod / Admin__",
            "• `kick @u [r]` / `ban @u [r]` / `unban <id> [r]`",
            "• `softban @u [r]` → ban+unban (clears msgs)",
            "• `tempban @u <dur> [r]` → timed ban",
            "• `purge <n>` (1–100)",
            "• `warn @u [r]` / `warnings @u` / `clearwarnings @u`",
            "• `mute @u [dur]` / `unmute @u`",
            "• `deafen @u` / `undeafen @u`",
            "• `movevc @u <channel>` → move to VC",
            "• `slowmode <s>` / `lockdown [r]` / `unlock`",
            "• `nickname @u <name>` / `giverole` / `removerole`",
            "• `modlog [n]` → recent mod actions",
            "• `joined <member>`",
            "**🔊 Voice** *(mod only — prevents spam)*",
            "• `join` → join your VC",
            "• `leave` → leave VC",
            "• `speak <text>` → TTS via pyttsx3",
            "• `speakweather <city>` → read weather aloud (`$sw`)",
            "• `speakrandomfact` → read a random fact aloud",
            "⚡ *Auto-mute triggers at 3 warnings (10 min)*",
        ]
    lines.append(f"\nPython {os.sys.version.split()[0]} • discord.py {discord.__version__}")

    embed = discord.Embed(title="📖 Bot Super Cool Help", description="\n".join(lines), color=0x5865F2)
    await _private_reply(ctx, embed=embed)

# ---------- Fun ----------
@bot.command(name="hello")
async def hello(ctx: commands.Context):
    await _try_delete(ctx)
    await ctx.send(f"HELLOHS SIRMENS {VICTORY}")

@bot.command(name="FUCKYOU")
async def insult(ctx: commands.Context):
    await _try_delete(ctx)
    await ctx.send(f"No u {VICTORY} {RUN}")

@bot.command(name="THEYTOOKMYFAMILY")
async def kidnapping(ctx: commands.Context):
    await _try_delete(ctx)
    await ctx.send(f"HAHAHA NUB {RUN} {RUN}")

@bot.command(name="bye")
async def bye(ctx: commands.Context):
    await _try_delete(ctx)
    await ctx.send(f"Ok fine, dramatic exit in 3…2…1… {RUN} {VICTORY} {VICTORY} {VICTORY}")

@bot.command(name="pass")
@commands.cooldown(2, 5, commands.BucketType.user)
async def password(ctx: commands.Context, length: int = 10):
    await _private_reply(ctx, f"🔑 Your password: `{gen_pass(length)}`\n*(DM'd — only you can see this)*")

@bot.command(name="8ball")
@commands.cooldown(2, 5, commands.BucketType.user)
async def eightball_cmd(ctx: commands.Context, *, question: str = ""):
    await _try_delete(ctx)
    await ctx.send(f"🎱 {eight_ball()} {VICTORY} {RUN}")

@bot.command(name="flip")
@commands.cooldown(2, 5, commands.BucketType.user)
async def flip(ctx: commands.Context):
    await _try_delete(ctx)
    await ctx.send(f"🪙 {coin_flip()}! {VICTORY} {RUN}")

@bot.command(name="roll")
@commands.cooldown(2, 5, commands.BucketType.user)
async def roll(ctx: commands.Context, dice: str = "1d6"):
    await _try_delete(ctx)
    try:
        result, rolls = roll_dice(dice)
        await ctx.send(f"🎲 `{dice}` → {rolls} = **{result}**")
    except ValueError:
        await ctx.send("Usage: `$roll NdM` (e.g., `2d6`, `1d20`)", delete_after=8)

@bot.command(name="say")
@commands.cooldown(2, 10, commands.BucketType.user)
async def say(ctx: commands.Context, *, text: str):
    await _try_delete(ctx)
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    flair = random.choice(["✨", "🌈", "🎉", "🦄", "🍭"])
    await ctx.send(f"{text} {flair}", allowed_mentions=discord.AllowedMentions.none())

@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await _try_delete(ctx)
    await ctx.send(f"🏓 Pong! **{round(bot.latency*1000)} ms**", delete_after=10)

@bot.command(name="dadjoke")
async def dadjoke(ctx: commands.Context):
    await _try_delete(ctx)
    jokes = [
        "I would tell you a construction joke, but I'm still working on it.",
        "Why did the scarecrow get promoted? He was outstanding in his field.",
        "I used to hate facial hair… but then it grew on me.",
        "Why don't eggs tell jokes? They'd crack each other up.",
        "I'm reading a book about anti-gravity. It's impossible to put down.",
        "Why can't you give Elsa a balloon? Because she'll let it go.",
        "I asked my dog what 2 minus 2 is. He said nothing.",
    ]
    await ctx.send(random.choice(jokes) + f" {VICTORY}")

@bot.command(name="afk")
async def afk(ctx: commands.Context, *, reason: str = "AFK"):
    await _try_delete(ctx)
    afk_users[ctx.author.id] = reason
    await ctx.send(f"💤 {ctx.author.mention} is now AFK: **{reason}**", delete_after=5)

@bot.command(name="color")
async def color_cmd(ctx: commands.Context, hex_color: str):
    await _try_delete(ctx)
    hex_color = hex_color.lstrip("#")
    try:
        value = int(hex_color, 16)
    except ValueError:
        return await ctx.send("Invalid hex. Use like `$color #FF5733`", delete_after=8)
    embed = discord.Embed(title=f"Color: #{hex_color.upper()}", color=value)
    embed.set_footer(text=f"Requested by {ctx.author}")
    await ctx.send(embed=embed)
    
@bot.command(name="randomfact")
async def random_fact(ctx: commands.Context):
    await _try_delete(ctx)
    fact = await fact_extractor()
    await ctx.send(f"📚 {fact}")


# ---------- Member utilities ----------
@bot.command(name="avatar")
async def avatar(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    if not member.display_avatar:
        return await _private_reply(ctx, "No avatar found.")
    embed = discord.Embed(title=f"{member}'s Avatar", color=0x00ccff)
    embed.set_image(url=member.display_avatar.url)
    await _private_reply(ctx, embed=embed)

@bot.command(name="userinfo")
async def userinfo(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    created = discord.utils.format_dt(member.created_at, style="F")
    joined  = discord.utils.format_dt(member.joined_at, style="F") if member.joined_at else "Unknown"
    roles   = [r.mention for r in reversed(member.roles) if ctx.guild and r != ctx.guild.default_role]
    embed = discord.Embed(title=f"User Info — {member}", color=0x00ccff)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.add_field(name="Top Role", value=member.top_role.mention if ctx.guild and member.top_role else "None", inline=True)
    embed.add_field(name="Account Created", value=created, inline=False)
    embed.add_field(name="Joined Server", value=joined, inline=False)
    if roles:
        embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles[:15]) + (" …" if len(roles) > 15 else ""), inline=False)
    await _private_reply(ctx, embed=embed)

@bot.command(name="serverinfo")
async def serverinfo(ctx: commands.Context):
    if not await _ensure_guild(ctx):
        return
    await _try_delete(ctx)
    guild = ctx.guild
    embed = discord.Embed(title=f"{guild.name} — Server Info", color=0x00ffcc)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=str(guild.owner), inline=False)
    embed.add_field(name="Members", value=guild.member_count, inline=True)
    embed.add_field(name="Channels", value=len(guild.channels), inline=True)
    embed.add_field(name="Roles", value=len(guild.roles), inline=True)
    embed.add_field(name="Boost Level", value=guild.premium_tier, inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, style="F"), inline=False)
    await ctx.send(embed=embed)

@bot.command(name="roleinfo")
async def roleinfo(ctx: commands.Context, *, role_name: str):
    if not await _ensure_guild(ctx):
        return
    await _try_delete(ctx)
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        return await ctx.send(f"Role `{role_name}` not found.", delete_after=8)
    embed = discord.Embed(title=f"Role: {role.name}", color=role.color)
    embed.add_field(name="ID", value=role.id, inline=True)
    embed.add_field(name="Members", value=len(role.members), inline=True)
    embed.add_field(name="Position", value=role.position, inline=True)
    embed.add_field(name="Mentionable", value=role.mentionable, inline=True)
    embed.add_field(name="Hoisted", value=role.hoist, inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(role.created_at, style="F"), inline=False)
    await ctx.send(embed=embed)

@bot.command(name="choose")
async def choose(ctx: commands.Context, *, options: str):
    await _try_delete(ctx)
    parts = [p.strip() for p in options.split("|") if p.strip()]
    if len(parts) < 2:
        return await ctx.send("Give at least two options: `$choose tacos | sushi | pizza`", delete_after=8)
    await ctx.send(f"I choose: **{random.choice(parts)}** {VICTORY}")

@bot.command(name="poll")
async def poll(ctx: commands.Context, *, text: str):
    await _try_delete(ctx)
    m = re.match(r'"\s*(.+?)\s*"\s*(.+)', text)
    if not m:
        return await ctx.send('Format: `$poll "Question" opt1 | opt2 | opt3`', delete_after=8)
    question, options = m.groups()
    opts = [o.strip() for o in options.split("|") if o.strip()]
    if not (2 <= len(opts) <= 10):
        return await ctx.send("Need 2–10 options separated by `|`.", delete_after=8)
    desc = "\n".join(f"{NUM_EMOJIS[i]}  {opt}" for i, opt in enumerate(opts))
    embed = discord.Embed(title=f"📊 {question}", description=desc, color=0x7289DA)
    embed.set_footer(text=f"Poll by {ctx.author}")
    msg = await ctx.send(embed=embed)
    for i in range(len(opts)):
        try:
            await msg.add_reaction(NUM_EMOJIS[i])
        except Exception:
            pass

@bot.command(name="remindme")
async def remindme(ctx: commands.Context, time: str, *, message: str):
    await _try_delete(ctx)
    try:
        seconds = parse_duration_to_seconds(time)
    except ValueError:
        return await ctx.send("Invalid time. Try `10m`, `2h30m`, `3d`.", delete_after=8)
    try:
        await ctx.author.send(f"⏰ Reminder set for **{time}**: {message}")
    except discord.Forbidden:
        await ctx.send(f"⏰ {ctx.author.mention} Reminder set for {time}.", delete_after=8)

    async def _remind():
        await asyncio.sleep(seconds)
        try:
            await ctx.author.send(f"⏰ Reminder from **{ctx.guild.name if ctx.guild else 'DM'}**: {message}")
        except discord.Forbidden:
            await ctx.send(f"{ctx.author.mention} ⏰ Reminder: {message}")
    asyncio.create_task(_remind())

# ---------- Images ----------
@bot.command(name="meme", aliases=["MEME"])
@commands.dynamic_cooldown(image_dynamic_cooldown, commands.BucketType.user)
async def meme(ctx: commands.Context, count: int = 1):
    await _try_delete(ctx)
    if not _image_cache:
        return await ctx.send(f"No images in `{IMAGES_DIR}`.", delete_after=8)
    count = max(1, min(int(count), 4))
    files = []
    for p in _pick_images(count):
        try:
            files.append(discord.File(fp=str(p), filename=p.name))
        except Exception as e:
            log.warning(f"[MEME] Could not attach {p}: {e}")
    if not files:
        return await ctx.send("Couldn't attach any image files.")
    await ctx.send(content=f"Here you go {ctx.author.mention}! {VICTORY}", files=files)

@bot.command(name="memevs")
@commands.dynamic_cooldown(image_dynamic_cooldown, commands.BucketType.user)
async def memevs(ctx: commands.Context):
    await _try_delete(ctx)
    if not _image_cache:
        return await ctx.send(f"No images in `{IMAGES_DIR}`.", delete_after=8)
    files = []
    for p in _pick_images(2):
        try:
            files.append(discord.File(fp=str(p), filename=p.name))
        except Exception as e:
            log.warning(f"[MEME] Could not attach {p}: {e}")
    if not files:
        return await ctx.send("Couldn't attach images.")
    msg = await ctx.send(content=f"**Meme Battle!** Vote: 1️⃣ or 2️⃣ {VICTORY}", files=files)
    try:
        await msg.add_reaction("1️⃣")
        await msg.add_reaction("2️⃣")
    except Exception:
        pass

@bot.command(name="DUCK", aliases=["duck"])
@commands.dynamic_cooldown(image_dynamic_cooldown, commands.BucketType.user)
async def duck(ctx: commands.Context):
    await _try_delete(ctx)
    url = await get_duck_image_url()
    if not url:
        return await ctx.send("Couldn't fetch a duck right now.", delete_after=8)
    embed = discord.Embed(title="🦆 Quack!", color=0x00ccff)
    embed.set_image(url=url)
    await ctx.send(embed=embed)

# ---------- Voice ----------
# NOTE ON 4017 ERRORS: WebSocket 4017 = Discord voice UDP blocked on your network.
# The TCP handshake succeeds but the audio stream can't get through.
# Fix: allow outbound UDP in Windows Firewall to *.discord.media on ports 50000-65535.
# reconnect=False prevents the infinite retry loop while the network issue persists.

async def _safe_connect(ctx: commands.Context, channel: discord.VoiceChannel):
    """Connect or move, failing fast instead of looping forever on 4017."""
    vc = ctx.voice_client

    # Kill any dead/stale client first
    if vc is not None and not vc.is_connected():
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        vc = None

    try:
        if vc:
            await vc.move_to(channel)
            return vc
        # reconnect=False → raises immediately on 4017 instead of retrying forever
        return await asyncio.wait_for(
            channel.connect(reconnect=False, self_deaf=True), timeout=20
        )
    except (TimeoutError, asyncio.TimeoutError):
        raise RuntimeError(
            "Timed out joining voice (>20s). "
            "Check that outbound UDP is allowed in Windows Firewall "
            "(ports 50000-65535 to *.discord.media)."
        )
    except discord.errors.ConnectionClosed as e:
        raise RuntimeError(
            f"Voice WebSocket closed (code {e.code}). "
            "This usually means UDP is blocked — allow it in Windows Firewall."
        )

async def _play_tts(vc: discord.VoiceClient, text: str) -> None:
    """Generate WAV via pyttsx3 and play it; cleans up the temp file after."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as _f:
        tmp_path = _f.name
    try:
        await asyncio.get_event_loop().run_in_executor(None, _tts_to_file, text, tmp_path)

        def _cleanup(err):
            if err:
                log.warning(f"[TTS] Playback error: {err}")
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        vc.play(discord.FFmpegPCMAudio(tmp_path, executable=FFMPEG_EXE), after=_cleanup)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise

@bot.command(name="join")
@commands.has_permissions(manage_messages=True)
async def join_vc(ctx: commands.Context):
    if not await _ensure_guild(ctx):
        return
    await _try_delete(ctx)
    if ctx.author.voice is None:
        return await ctx.send("You need to be in a voice channel first. 🎙️", delete_after=8)
    try:
        vc = await _safe_connect(ctx, ctx.author.voice.channel)
        await ctx.send(f"🔊 Joined **{vc.channel.name}**!", delete_after=5)
    except RuntimeError as e:
        await ctx.send(f"❌ {e}", delete_after=15)

@bot.command(name="leave")
@commands.has_permissions(manage_messages=True)
async def leave_vc(ctx: commands.Context):
    if not await _ensure_guild(ctx):
        return
    await _try_delete(ctx)
    if ctx.voice_client is None:
        return await ctx.send("I'm not in a voice channel.", delete_after=8)
    name = ctx.voice_client.channel.name
    await ctx.voice_client.disconnect(force=True)
    await ctx.send(f"👋 Left **{name}**.", delete_after=5)

@bot.command(name="speak")
@commands.has_permissions(manage_messages=True)
@commands.cooldown(1, 5, commands.BucketType.guild)
async def speak(ctx: commands.Context, *, text: str):
    if not await _ensure_guild(ctx):
        return
    await _try_delete(ctx)
    if ctx.author.voice is None:
        return await ctx.send("Join a voice channel first, then use `$speak`.", delete_after=8)

    try:
        vc = await _safe_connect(ctx, ctx.author.voice.channel)
    except RuntimeError as e:
        return await ctx.send(f"❌ {e}", delete_after=15)

    if vc.is_playing():
        return await ctx.send("Already speaking! Wait a moment.", delete_after=5)

    try:
        await _play_tts(vc, text)
        preview = text[:60] + ("…" if len(text) > 60 else "")
        await ctx.send(f"🔊 Speaking: *{preview}*", delete_after=10)
    except Exception as e:
        log.warning(f"[TTS] Error: {e}")
        await ctx.send(f"TTS failed: `{e}`", delete_after=10)

@bot.command(name="speakweather", aliases=["sw"])
@commands.has_permissions(manage_messages=True)
@commands.cooldown(1, 10, commands.BucketType.guild)
async def speakweather(ctx: commands.Context, *, city: str):
    """Fetch weather and read it aloud in your voice channel."""
    if not await _ensure_guild(ctx):
        return
    await _try_delete(ctx)
    if ctx.author.voice is None:
        return await ctx.send("Join a voice channel first.", delete_after=8)

    async with ctx.typing():
        data = await _fetch_weather(city)

    if not data:
        return await ctx.send(f"❌ Couldn't fetch weather for **{city}**.", delete_after=10)

    try:
        cur      = data["current_condition"][0]
        area     = data["nearest_area"][0]
        location = area["areaName"][0]["value"]
        country  = area["country"][0]["value"]
        desc     = cur["weatherDesc"][0]["value"]
        temp_c   = cur["temp_C"]
        feels_c  = cur["FeelsLikeC"]
        humidity = cur["humidity"]
        wind_kmh = cur["windspeedKmph"]
        wind_dir = cur["winddir16Point"]
        uv       = cur["uvIndex"]
    except (KeyError, IndexError) as e:
        return await ctx.send(f"❌ Unexpected weather data format: `{e}`", delete_after=10)

    speech = (
        f"Current weather in {location}, {country}. "
        f"{desc}. "
        f"Temperature: {temp_c} degrees Celsius, feels like {feels_c}. "
        f"Humidity: {humidity} percent. "
        f"Wind: {wind_kmh} kilometres per hour, {wind_dir}. "
        f"UV index: {uv}."
    )

    try:
        vc = await _safe_connect(ctx, ctx.author.voice.channel)
    except RuntimeError as e:
        return await ctx.send(f"❌ {e}", delete_after=15)

    if vc.is_playing():
        return await ctx.send("Already speaking! Wait a moment.", delete_after=5)

    try:
        await _play_tts(vc, speech)
        await ctx.send(
            f"🔊 Reading weather for **{location}, {country}**.", delete_after=10
        )
    except Exception as e:
        log.warning(f"[TTS] speakweather error: {e}")
        await ctx.send(f"TTS failed: `{e}`", delete_after=10)
        
@bot.command(name="speakrandomfact", aliases=["speakfact"])
@commands.has_permissions(manage_messages=True)
@commands.cooldown(1, 10, commands.BucketType.guild)
async def speak_random_fact(ctx: commands.Context):
    """Fetch a random fact and read it aloud in your voice channel."""
    if not await _ensure_guild(ctx):
        return
    await _try_delete(ctx)
    if ctx.author.voice is None:
        return await ctx.send("Join a voice channel first.", delete_after=8)

    async with ctx.typing():
        fact = await fact_extractor()

    if not fact:
        return await ctx.send(f"❌ Couldn't fetch a random fact right now.", delete_after=10)

    speech = f"Here's a random fact: {fact}"

    try:
        vc = await _safe_connect(ctx, ctx.author.voice.channel)
    except RuntimeError as e:
        return await ctx.send(f"❌ {e}", delete_after=15)

    if vc.is_playing():
        return await ctx.send("Already speaking! Wait a moment.", delete_after=5)

    try:
        await _play_tts(vc, speech)
        await ctx.send(f"🔊 Speaking a random fact for you!", delete_after=10)
    except Exception as e:
        log.warning(f"[TTS] speakrandomfact error: {e}")
        await ctx.send(f"TTS failed: `{e}`", delete_after=10)

# ---------- Weather ----------
WEATHER_EMOJI = {
    "sunny": "☀️", "clear": "☀️", "partly cloudy": "⛅", "cloudy": "☁️",
    "overcast": "☁️", "mist": "🌫️", "fog": "🌫️", "rain": "🌧️",
    "drizzle": "🌦️", "shower": "🌧️", "snow": "❄️", "sleet": "🌨️",
    "blizzard": "❄️", "thunder": "⛈️", "storm": "⛈️", "ice": "🧊",
    "freezing": "🧊", "wind": "💨", "haze": "🌫️",
}

def _weather_emoji(desc: str) -> str:
    d = desc.lower()
    for key, emoji in WEATHER_EMOJI.items():
        if key in d:
            return emoji
    return "🌡️"

async def _fetch_weather(city: str) -> dict | None:
    url = f"https://wttr.in/{city}?format=j1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10, headers={"Accept": "application/json"}) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        log.warning(f"[WEATHER] fetch failed: {e}")
        return None

@bot.command(name="weather", aliases=["w"])
@commands.cooldown(2, 10, commands.BucketType.user)
async def weather(ctx: commands.Context, *, city: str):
    await _try_delete(ctx)
    async with ctx.typing():
        data = await _fetch_weather(city)

    if not data:
        return await ctx.send(
            f"❌ Couldn't fetch weather for **{city}**. Check the name and try again.", delete_after=10
        )

    try:
        cur  = data["current_condition"][0]
        area = data["nearest_area"][0]

        location  = area["areaName"][0]["value"]
        country   = area["country"][0]["value"]
        desc      = cur["weatherDesc"][0]["value"]
        temp_c    = cur["temp_C"]
        temp_f    = cur["temp_F"]
        feels_c   = cur["FeelsLikeC"]
        feels_f   = cur["FeelsLikeF"]
        humidity  = cur["humidity"]
        wind_kmh  = cur["windspeedKmph"]
        wind_dir  = cur["winddir16Point"]
        vis_km    = cur["visibility"]
        pressure  = cur["pressure"]
        uv        = cur["uvIndex"]
        emoji     = _weather_emoji(desc)

        embed = discord.Embed(
            title=f"{emoji} Weather — {location}, {country}",
            description=f"**{desc}**",
            color=0x56B4D3,
        )
        embed.add_field(name="🌡️ Temp",     value=f"{temp_c}°C / {temp_f}°F",           inline=True)
        embed.add_field(name="🤔 Feels Like", value=f"{feels_c}°C / {feels_f}°F",         inline=True)
        embed.add_field(name="💧 Humidity",  value=f"{humidity}%",                        inline=True)
        embed.add_field(name="💨 Wind",      value=f"{wind_kmh} km/h {wind_dir}",         inline=True)
        embed.add_field(name="👁️ Visibility", value=f"{vis_km} km",                       inline=True)
        embed.add_field(name="🔵 Pressure",  value=f"{pressure} hPa",                    inline=True)
        embed.add_field(name="🌞 UV Index",  value=uv,                                    inline=True)

        # 3-day forecast
        forecast_lines = []
        for day in data.get("weather", [])[:3]:
            date     = day["date"]
            max_c    = day["maxtempC"]
            min_c    = day["mintempC"]
            max_f    = day["maxtempF"]
            min_f    = day["mintempF"]
            day_desc = day["hourly"][4]["weatherDesc"][0]["value"]  # midday ~12:00
            day_emo  = _weather_emoji(day_desc)
            forecast_lines.append(f"`{date}` {day_emo} {day_desc} — {min_c}–{max_c}°C / {min_f}–{max_f}°F")

        if forecast_lines:
            embed.add_field(name="📅 3-Day Forecast", value="\n".join(forecast_lines), inline=False)

        embed.set_footer(text="Powered by wttr.in")
        await ctx.send(embed=embed)

    except (KeyError, IndexError) as e:
        log.warning(f"[WEATHER] parse error: {e}")
        await ctx.send(f"❌ Unexpected data format for **{city}**.", delete_after=10)

# ---------- Moderation ----------
@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await _mod_reply(ctx, "You can't act on that member due to role hierarchy.")
    if ctx.guild.me.top_role <= member.top_role:
        return await _mod_reply(ctx, "My role isn't high enough to kick that member.")
    try:
        await member.kick(reason=reason)
        _log_mod_action(ctx.guild.id, ctx.author.id, "KICK", str(member), reason)
        await _mod_reply(ctx, f"👢 Kicked **{member}** — {reason}")
    except discord.Forbidden:
        await _mod_reply(ctx, "I don't have permission to kick that user.")
    except Exception as e:
        await _mod_reply(ctx, f"Kick failed: `{e}`")

@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await _mod_reply(ctx, "You can't act on that member due to role hierarchy.")
    if ctx.guild.me.top_role <= member.top_role:
        return await _mod_reply(ctx, "My role isn't high enough to ban that member.")
    try:
        await member.ban(reason=reason)
        _log_mod_action(ctx.guild.id, ctx.author.id, "BAN", str(member), reason)
        await _mod_reply(ctx, f"🔨 Banned **{member}** — {reason}")
    except discord.Forbidden:
        await _mod_reply(ctx, "I don't have permission to ban that user.")
    except Exception as e:
        await _mod_reply(ctx, f"Ban failed: `{e}`")

@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban(ctx: commands.Context, user_id: int, *, reason: str = "No reason provided"):
    if not await _ensure_guild(ctx):
        return
    await _try_delete(ctx)
    try:
        await ctx.guild.unban(discord.Object(id=user_id), reason=reason)
        _log_mod_action(ctx.guild.id, ctx.author.id, "UNBAN", str(user_id), reason)
        await ctx.send(f"🔓 Unbanned user ID `{user_id}` — {reason}")
    except discord.NotFound:
        await ctx.send(f"No ban found for ID `{user_id}`.", delete_after=8)
    except discord.Forbidden:
        await ctx.send("I don't have permission to unban.", delete_after=8)

@bot.command(name="softban")
@commands.has_permissions(ban_members=True)
async def softban(ctx: commands.Context, member: discord.Member, *, reason: str = "Softban"):
    """Ban then immediately unban — purges recent messages."""
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await _mod_reply(ctx, "You can't act on that member due to role hierarchy.")
    try:
        await member.ban(reason=f"[SOFTBAN] {reason}", delete_message_days=1)
        await ctx.guild.unban(member, reason="Softban unban")
        _log_mod_action(ctx.guild.id, ctx.author.id, "SOFTBAN", str(member), reason)
        await _mod_reply(ctx, f"🔨🔓 Softbanned **{member}** (cleared recent messages) — {reason}")
    except discord.Forbidden:
        await _mod_reply(ctx, "I don't have permission.")
    except Exception as e:
        await _mod_reply(ctx, f"Softban failed: `{e}`")

@bot.command(name="tempban")
@commands.has_permissions(ban_members=True)
async def tempban(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "Temp ban"):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await _mod_reply(ctx, "You can't act on that member due to role hierarchy.")
    try:
        seconds = parse_duration_to_seconds(duration)
    except ValueError:
        return await _mod_reply(ctx, "Invalid duration. Use `10m`, `2h`, `3d`, etc.")
    try:
        await member.ban(reason=f"[TEMPBAN {duration}] {reason}")
        _log_mod_action(ctx.guild.id, ctx.author.id, f"TEMPBAN({duration})", str(member), reason)
        await schedule_unban(ctx.guild.id, member.id, seconds)
        await _mod_reply(ctx, f"⏳🔨 Temp banned **{member}** for **{duration}** — {reason}")
    except discord.Forbidden:
        await _mod_reply(ctx, "I don't have permission to ban.")
    except Exception as e:
        await _mod_reply(ctx, f"Tempban failed: `{e}`")

@bot.command(name="purge")
@commands.max_concurrency(1, per=commands.BucketType.channel, wait=False)
@commands.has_permissions(manage_messages=True)
async def purge(ctx: commands.Context, count: int):
    if not await _ensure_guild(ctx):
        return
    if not (1 <= count <= 100):
        return await ctx.send("Choose a number between 1 and 100.", delete_after=5)
    await _try_delete(ctx)
    deleted = await ctx.channel.purge(limit=count)
    await ctx.send(f"🧹 Deleted **{len(deleted)}** messages.", delete_after=3)

@bot.command(name="giverole")
@commands.has_permissions(manage_roles=True)
async def giverole(ctx: commands.Context, member: discord.Member, *, role_name: str):
    if not await _ensure_guild(ctx):
        return
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if role is None:
        return await _mod_reply(ctx, f"Role `{role_name}` not found.")
    if not _role_height_ok(ctx.guild, role):
        return await _mod_reply(ctx, "My role isn't high enough to manage that role.")
    try:
        await member.add_roles(role, reason=f"By {ctx.author} via bot")
        _log_mod_action(ctx.guild.id, ctx.author.id, "GIVEROLE", str(member), role_name)
        await _mod_reply(ctx, f"✅ Added **{role_name}** to {member.mention}")
    except discord.Forbidden:
        await _mod_reply(ctx, "❌ I don't have permission to manage that role.")
    except Exception as e:
        await _mod_reply(ctx, f"⚠️ Couldn't assign role: `{e}`")

@bot.command(name="removerole")
@commands.has_permissions(manage_roles=True)
async def removerole(ctx: commands.Context, member: discord.Member, *, role_name: str):
    if not await _ensure_guild(ctx):
        return
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if role is None:
        return await _mod_reply(ctx, f"Role `{role_name}` not found.")
    if not _role_height_ok(ctx.guild, role):
        return await _mod_reply(ctx, "My role isn't high enough to manage that role.")
    try:
        await member.remove_roles(role, reason=f"By {ctx.author} via bot")
        _log_mod_action(ctx.guild.id, ctx.author.id, "REMOVEROLE", str(member), role_name)
        await _mod_reply(ctx, f"✅ Removed **{role_name}** from {member.mention}")
    except discord.Forbidden:
        await _mod_reply(ctx, "❌ I don't have permission to manage that role.")
    except Exception as e:
        await _mod_reply(ctx, f"⚠️ Couldn't remove role: `{e}`")

@bot.command(name="joined")
@commands.has_permissions(administrator=True)
async def joined(ctx: commands.Context, *, member: discord.Member):
    if not await _ensure_guild(ctx):
        return
    await _try_delete(ctx)
    joined_at = member.joined_at
    joined_abs = discord.utils.format_dt(joined_at, style="F") if joined_at else "Unknown"
    joined_rel = discord.utils.format_dt(joined_at, style="R") if joined_at else ""
    roles = [r for r in member.roles if r != ctx.guild.default_role]
    if roles:
        top   = member.top_role if member.top_role != ctx.guild.default_role else None
        other = sorted([r for r in roles if r != top], key=lambda x: x.position, reverse=True)
        role_line = ", ".join(([top.mention] if top else []) + [r.mention for r in other])
    else:
        role_line = "No roles"
    await ctx.send(f"**{member}** joined {joined_abs} ({joined_rel})\n**Roles:** {role_line}")

# ---- Warnings ----
@bot.command(name="warn")
@commands.has_permissions(manage_messages=True)
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await _mod_reply(ctx, "You can't warn that member due to role hierarchy.")
    key = (ctx.guild.id, member.id)
    warnings_store.setdefault(key, []).append({"reason": reason, "by": ctx.author.id, "at": _now_utc()})
    count = len(warnings_store[key])
    try:
        await member.send(f"⚠️ You've been warned in **{ctx.guild.name}**: {reason}")
    except discord.Forbidden:
        pass
    _log_mod_action(ctx.guild.id, ctx.author.id, f"WARN({count})", str(member), reason)
    await _mod_reply(ctx, f"⚠️ Warned {member.mention} — {reason} *(Warning #{count})*")

    # Auto-escalation: 3+ warnings → auto-mute 10 min
    if count >= 3:
        role = await ensure_muted_role(ctx.guild)
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Auto-mute: 3+ warnings")
                await schedule_unmute(ctx.guild.id, member.id, 600)
                await ctx.channel.send(
                    f"🔇 {member.mention} auto-muted for **10 minutes** after reaching **{count} warnings**.",
                    delete_after=15,
                )
            except Exception:
                pass

@bot.command(name="warnings")
@commands.has_permissions(manage_messages=True)
async def warnings_cmd(ctx: commands.Context, member: discord.Member):
    if not await _ensure_guild(ctx):
        return
    await _try_delete(ctx)
    key = (ctx.guild.id, member.id)
    entries = warnings_store.get(key, [])
    if not entries:
        return await ctx.send(f"{member.mention} has no warnings. {VICTORY}", delete_after=8)
    lines = [f"Warnings for **{member}** ({len(entries)} total):"]
    for i, w in enumerate(entries, 1):
        when = discord.utils.format_dt(w["at"], style="R")
        mod  = ctx.guild.get_member(w["by"])
        lines.append(f"{i}. {w['reason']} — by {mod.mention if mod else w['by']} ({when})")
    await ctx.send("\n".join(lines))

@bot.command(name="clearwarnings")
@commands.has_permissions(manage_messages=True)
async def clearwarnings(ctx: commands.Context, member: discord.Member):
    if not await _ensure_guild(ctx):
        return
    key   = (ctx.guild.id, member.id)
    count = len(warnings_store.get(key, []))
    warnings_store.pop(key, None)
    _log_mod_action(ctx.guild.id, ctx.author.id, "CLEARWARNINGS", str(member), f"Cleared {count} warnings")
    await _mod_reply(ctx, f"🧽 Cleared **{count}** warnings for {member.mention}.")

# ---- Channel control ----
@bot.command(name="slowmode")
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx: commands.Context, seconds: int):
    if not await _ensure_guild(ctx):
        return
    seconds = max(0, min(seconds, 21600))
    await ctx.channel.edit(slowmode_delay=seconds, reason=f"By {ctx.author}")
    _log_mod_action(ctx.guild.id, ctx.author.id, "SLOWMODE", ctx.channel.name, f"{seconds}s")
    msg = "⏱️ Slowmode disabled." if seconds == 0 else f"⏱️ Slowmode set to **{seconds}s**."
    await _mod_reply(ctx, msg)

@bot.command(name="lockdown")
@commands.has_permissions(manage_channels=True)
async def lockdown(ctx: commands.Context, *, reason: str = "Lockdown"):
    if not await _ensure_guild(ctx):
        return
    everyone   = ctx.guild.default_role
    overwrites = ctx.channel.overwrites_for(everyone)
    overwrites.send_messages = False
    try:
        await ctx.channel.set_permissions(everyone, overwrite=overwrites, reason=f"{reason} — by {ctx.author}")
        _log_mod_action(ctx.guild.id, ctx.author.id, "LOCKDOWN", ctx.channel.name, reason)
        await _mod_reply(ctx, f"🔒 Channel locked. Reason: {reason}")
    except discord.Forbidden:
        await _mod_reply(ctx, "I don't have permission to lock this channel.")

@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context):
    if not await _ensure_guild(ctx):
        return
    everyone   = ctx.guild.default_role
    overwrites = ctx.channel.overwrites_for(everyone)
    overwrites.send_messages = None
    try:
        await ctx.channel.set_permissions(everyone, overwrite=overwrites, reason=f"Unlock by {ctx.author}")
        _log_mod_action(ctx.guild.id, ctx.author.id, "UNLOCK", ctx.channel.name, "")
        await _mod_reply(ctx, "🔓 Channel unlocked.")
    except discord.Forbidden:
        await _mod_reply(ctx, "I don't have permission to unlock this channel.")

@bot.command(name="nickname")
@commands.has_permissions(manage_nicknames=True)
async def nickname(ctx: commands.Context, member: discord.Member, *, new_name: str):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await _mod_reply(ctx, "You can't change that member's nickname due to role hierarchy.")
    try:
        await member.edit(nick=new_name, reason=f"By {ctx.author}")
        _log_mod_action(ctx.guild.id, ctx.author.id, "NICKNAME", str(member), new_name)
        await _mod_reply(ctx, f"✏️ Nickname for {member.mention} → **{new_name}**")
    except discord.Forbidden:
        await _mod_reply(ctx, "I don't have permission to change that nickname.")

@bot.command(name="mute")
@commands.has_permissions(moderate_members=True, manage_roles=True)
async def mute(ctx: commands.Context, member: discord.Member, duration: str = None, *, reason: str = "Muted"):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await _mod_reply(ctx, "You can't mute that member due to role hierarchy.")
    role = await ensure_muted_role(ctx.guild)
    if role in member.roles:
        return await _mod_reply(ctx, "They're already muted.")
    try:
        await member.add_roles(role, reason=f"{reason} — by {ctx.author}")
        dur_text = f" for **{duration}**" if duration else ""
        _log_mod_action(ctx.guild.id, ctx.author.id, f"MUTE{dur_text}", str(member), reason)
        await _mod_reply(ctx, f"🔇 Muted {member.mention}{dur_text} — {reason}")
    except discord.Forbidden:
        return await _mod_reply(ctx, "I don't have permission to add the Muted role.")
    if duration:
        try:
            await schedule_unmute(ctx.guild.id, member.id, parse_duration_to_seconds(duration))
        except ValueError:
            await ctx.send("Duration format invalid. Use like `10m`, `2h30m`, `3d`.", delete_after=8)

@bot.command(name="unmute")
@commands.has_permissions(moderate_members=True, manage_roles=True)
async def unmute(ctx: commands.Context, member: discord.Member):
    if not await _ensure_guild(ctx):
        return
    role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not role or role not in member.roles:
        return await _mod_reply(ctx, "That member is not muted.")
    try:
        await member.remove_roles(role, reason=f"Unmuted by {ctx.author}")
        task = scheduled_unmutes.pop((ctx.guild.id, member.id), None)
        if task and not task.done():
            task.cancel()
        _log_mod_action(ctx.guild.id, ctx.author.id, "UNMUTE", str(member), "Manual unmute")
        await _mod_reply(ctx, f"🔈 Unmuted {member.mention}. {VICTORY}")
    except discord.Forbidden:
        await _mod_reply(ctx, "I don't have permission to remove the Muted role.")

@bot.command(name="deafen")
@commands.has_permissions(deafen_members=True)
async def deafen(ctx: commands.Context, member: discord.Member, *, reason: str = "Deafened"):
    if not await _ensure_guild(ctx):
        return
    if not _can_act(ctx.author, member):
        return await _mod_reply(ctx, "You can't deafen that member due to role hierarchy.")
    if member.voice is None:
        return await _mod_reply(ctx, f"{member.mention} is not in a voice channel.")
    try:
        await member.edit(deafen=True, reason=f"{reason} — by {ctx.author}")
        _log_mod_action(ctx.guild.id, ctx.author.id, "DEAFEN", str(member), reason)
        await _mod_reply(ctx, f"🔕 Deafened {member.mention} — {reason}")
    except discord.Forbidden:
        await _mod_reply(ctx, "I don't have permission to deafen members.")

@bot.command(name="undeafen")
@commands.has_permissions(deafen_members=True)
async def undeafen(ctx: commands.Context, member: discord.Member):
    if not await _ensure_guild(ctx):
        return
    if member.voice is None:
        return await _mod_reply(ctx, f"{member.mention} is not in a voice channel.")
    try:
        await member.edit(deafen=False, reason=f"Undeafened by {ctx.author}")
        _log_mod_action(ctx.guild.id, ctx.author.id, "UNDEAFEN", str(member), "Manual undeafen")
        await _mod_reply(ctx, f"🔔 Undeafened {member.mention}. {VICTORY}")
    except discord.Forbidden:
        await _mod_reply(ctx, "I don't have permission to undeafen members.")

@bot.command(name="movevc")
@commands.has_permissions(move_members=True)
async def movevc(ctx: commands.Context, member: discord.Member, *, channel_name: str):
    if not await _ensure_guild(ctx):
        return
    if member.voice is None:
        return await _mod_reply(ctx, f"{member.mention} is not in a voice channel.")
    target = discord.utils.get(ctx.guild.voice_channels, name=channel_name)
    if target is None:
        return await _mod_reply(ctx, f"Voice channel `{channel_name}` not found.")
    try:
        await member.move_to(target, reason=f"Moved by {ctx.author}")
        _log_mod_action(ctx.guild.id, ctx.author.id, "MOVEVC", str(member), channel_name)
        await _mod_reply(ctx, f"🚀 Moved {member.mention} to **{channel_name}**.")
    except discord.Forbidden:
        await _mod_reply(ctx, "I don't have permission to move members.")

@bot.command(name="modlog")
@commands.has_permissions(manage_messages=True)
async def modlog(ctx: commands.Context, count: int = 10):
    if not await _ensure_guild(ctx):
        return
    await _try_delete(ctx)
    count = max(1, min(count, 25))
    guild_logs = [e for e in mod_log_store if e["guild_id"] == ctx.guild.id]
    if not guild_logs:
        return await ctx.send("No mod actions logged yet.", delete_after=8)
    recent = guild_logs[-count:][::-1]
    lines = [f"**Recent Mod Log ({len(recent)} entries)**"]
    for e in recent:
        when    = discord.utils.format_dt(e["at"], style="R")
        mod     = ctx.guild.get_member(e["mod_id"])
        mod_str = mod.display_name if mod else str(e["mod_id"])
        lines.append(f"`{e['action']}` on **{e['target']}** by {mod_str} ({when}) — {e['reason'] or '—'}")
    embed = discord.Embed(description="\n".join(lines), color=0xFF6B6B)
    await ctx.send(embed=embed, delete_after=30)

# ---------- Moderated Upload ----------
@bot.command(name="uploadimage", aliases=["upload"])
@commands.cooldown(1, 10, commands.BucketType.user)
async def upload_image(ctx: commands.Context):
    if not ctx.message.attachments:
        return await ctx.send("❌ Attach an image file.", delete_after=8)
    await _try_delete(ctx)

    accepted, rejected = [], []

    for attachment in ctx.message.attachments:
        name = attachment.filename.lower()
        if not name.endswith(VALID_EXTS):
            rejected.append(name + " (not image)")
            continue

        unique = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{name}"
        path   = IMAGES_DIR / unique
        try:
            await attachment.save(path)
        except Exception as e:
            log.warning(f"[UPLOAD] Save failed {name}: {e}")
            rejected.append(name + " (save error)")
            continue

        text = extract_text_from_image(path)
        if contains_banned_text(text):
            path.unlink(missing_ok=True)
            rejected.append(name + " (banned text)")
            continue

        if not (await is_meme_image(path) or looks_like_meme(text) or image_has_face(path)):
            path.unlink(missing_ok=True)
            rejected.append(name + " (not meme-like)")
            continue

        accepted.append(unique)

    if accepted:
        _refresh_image_cache()

    msg = []
    if accepted:
        msg.append(f"✅ Accepted {len(accepted)} image(s)")
    if rejected:
        msg.append("❌ Rejected: " + ", ".join(rejected))
    await ctx.send("\n".join(msg), delete_after=10)

# ---------- Error Handler ----------
@bot.event
async def on_command_error(ctx: commands.Context, error):
    if hasattr(ctx.command, "on_error"):
        return
    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(f"⏳ Slow down! Try again in **{error.retry_after:.1f}s**.", delete_after=5)
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("🛡️ You're missing permissions for that.", delete_after=8)
    if isinstance(error, commands.BotMissingPermissions):
        return await ctx.send("⚠️ I'm missing permissions. Adjust my role or channel perms.", delete_after=8)
    if isinstance(error, (commands.MemberNotFound, BadArgument)):
        return await ctx.send("❓ Can't find that member. Try mentioning them or use an exact name.", delete_after=8)
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(f"❌ Missing: `{error.param.name}`. Try `$help`.", delete_after=8)

    log.exception("Unhandled command error", exc_info=error)
    await ctx.send(f"Unexpected error: `{error}`", delete_after=10)

# ---------- Run ----------
if TOKEN == "REPLACE_ME_WITH_ENV_VAR":
    raise SystemExit("Set DISCORD_TOKEN env var instead of hardcoding your token.")

bot.run(TOKEN)
