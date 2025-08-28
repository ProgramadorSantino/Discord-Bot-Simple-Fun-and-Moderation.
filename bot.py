# bot.py
import os
import random
import discord
from discord.ext import commands
from logic import gen_pass, eight_ball, coin_flip, roll_dice
from discord.ext.commands import BadArgument 
from dotenv import load_dotenv #KEEP BELOW OTHER IMPORTS

# Custom emojis
VICTORY = "<:VICTORY:1408236937424273529>"

load_dotenv()  # read variables from .env into the environment
TOKEN = os.getenv("DISCORD_TOKEN")  # look up the token by name

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Put it in a .env file or your OS env vars.")

# -------- Intents & Bot Setup --------
intents = discord.Intents.default()
intents.message_content = True          # needed for reading message text
intents.members = True                  # helps with ban/kick and member converters

# Choose a prefix you like; you can also add slash commands later
bot = commands.Bot(command_prefix="$", intents=intents, help_command=None)




# -------- Events --------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id: {bot.user.id})")
    activity = discord.Game(name="$help — now with ✨silliness✨")
    await bot.change_presence(activity=activity)

@bot.event
async def on_message(message: discord.Message):
    # ignore our own messages
    if message.author == bot.user:
        return

       # --- DMs: friendly chat behavior ---
    if isinstance(message.channel, discord.DMChannel):
        # only create text if it's actually a DM
        text = message.content.strip().lower()

        if text.startswith(("hi", "hello", "hola")):
            await message.channel.send(f"👋 ¡Hola! I’m alive in DMs too. Try `$help` for commands. {VICTORY}")
        elif text.startswith("$pass"):
            await message.channel.send(gen_pass(10) + f" {VICTORY}")
        else:
            # Slightly silly echo with random emoji OR custom VICTORY
            await message.channel.send(
                f"You whispered: **{message.content}** {random.choice(['😸','🦄','✨','🌀','🍀', VICTORY])}"
            )

        # Important: let commands still work in DMs
        await bot.process_commands(message)
        return



    # let commands process for guild messages
    await bot.process_commands(message)

# -------- Help (pretty & short) --------
# -------- Help (pretty & short) --------

@bot.command(name="help")
async def _help(ctx: commands.Context):
    perms = ctx.author.guild_permissions
    # Admins only (tighten from "or manage_roles" to just real admins)
    is_admin = perms.administrator

    lines = [
        "**Commands (prefix: `$`)**",
        "• `hello` → I say hi",
        "• `bye` → dramatic exit",
        "• `pass [len]` → generate a random symbol password (default 10)",
        "• `8ball [question]` → cosmic wisdom 🎱",
        "• `flip` → coin flip",
        "• `roll [NdM]` → roll dice, e.g. `roll 2d6`",
        "• `say <text>` → I repeat (with ✨ flair)",
    ]

    if is_admin:
        lines += [
            "",
            "__Admin only__",
            "• `kick @user [reason]`",
            "• `ban @user [reason]`",
            "• `purge <count>` → delete last N messages (max 100)",
            "• `giverole @user <role name>` → assign a role",
            "• `removerole @user <role name>` → remove a role",
            "• `joined <member>` → joined date + top role(s)",
        ]

    await ctx.send("\n".join(lines))



# -------- Fun / Silliness --------
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
async def password(ctx: commands.Context, length: int = 10):
    length = max(4, min(length, 64))  # simple safety bounds
    await ctx.send(gen_pass(length))

@bot.command(name="8ball")
async def eightball_cmd(ctx: commands.Context, *, question: str = ""):
    await ctx.send(f"🎱 {eight_ball()}{VICTORY} <a:RUN:1408589572312535121>")

@bot.command(name="flip")
async def flip(ctx: commands.Context):
    await ctx.send(f"🪙 {coin_flip()}!{VICTORY} <a:RUN:1408589572312535121>")

@bot.command(name="roll")
async def roll(ctx: commands.Context, dice: str = "1d6"):
    try:
        result, rolls = roll_dice(dice)
        await ctx.send(f"🎲 `{dice}` → {rolls} = **{result}**")
    except ValueError:
        await ctx.send("Usage: `$roll NdM` (e.g., `2d6`, `1d20`)")

@bot.command(name="say")
async def say(ctx: commands.Context, *, text: str):
    flair = random.choice(["✨", "🌈", "🎉", "🦄", "🍭"])
    await ctx.send(f"{text} {flair}")

# -------- Moderation --------
@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    try:
        await member.kick(reason=reason)
        await ctx.send(f"👢 Kicked **{member}** — {reason} NUB <a:RUN:1408589572312535121>")
    except discord.Forbidden:
        await ctx.send("I don’t have permission to kick that user.")
    except Exception as e:
        await ctx.send(f"Kick failed: `{e}`")

@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    try:
        await member.ban(reason=reason)
        await ctx.send(f"🔨 Banned **{member}** — {reason} NUB <a:RUN:1408589572312535121>")
    except discord.Forbidden:
        await ctx.send("I don’t have permission to ban that user.")
    except Exception as e:
        await ctx.send(f"Ban failed: `{e}`")

@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def purge(ctx: commands.Context, count: int):
    if not (1 <= count <= 100):
        await ctx.send("Please choose a number between 1 and 100.")
        return
    deleted = await ctx.channel.purge(limit=count + 1)  # +1 to include the command message
    await ctx.send(f"🧹 Deleted {len(deleted) - 1} messages.", delete_after=3)
# -------- Role Management (Admin only) --------
@bot.command(name="giverole")
@commands.has_permissions(administrator=True)
async def giverole(ctx: commands.Context, member: discord.Member, *, role_name: str):
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if role is None:
        await ctx.send(f"Role `{role_name}` not found.")
        return
    try:
        await member.add_roles(role, reason=f"By {ctx.author} via bot")
        await ctx.send(f"✅ Added role **{role_name}** to {member.mention}")
    except discord.Forbidden:
        await ctx.send("❌ I don’t have permission to manage that role. Move my role higher in the hierarchy.")
    except Exception as e:
        await ctx.send(f"⚠️ Couldn’t assign role: `{e}`")

@bot.command(name="removerole")
@commands.has_permissions(administrator=True)
async def removerole(ctx: commands.Context, member: discord.Member, *, role_name: str):
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if role is None:
        await ctx.send(f"Role `{role_name}` not found.")
        return
    try:
        await member.remove_roles(role, reason=f"By {ctx.author} via bot")
        await ctx.send(f"✅ Removed role **{role_name}** from {member.mention}")
    except discord.Forbidden:
        await ctx.send("❌ I don’t have permission to manage that role. Move my role higher in the hierarchy.")
    except Exception as e:
        await ctx.send(f"⚠️ Couldn’t remove role: `{e}`")

# -------- Admin: Joined Info (date + roles) --------
@bot.command(name="joined")
@commands.has_permissions(administrator=True)
async def joined(ctx: commands.Context, *, member: discord.Member):
    """
    Admin-only. Example: $joined RANDOM USER
    Shows when the member joined and their roles.
    Accepts mentions, IDs, usernames, nicknames, and names with spaces.
    """
    # Pretty joined time
    joined_abs = discord.utils.format_dt(member.joined_at, style="F")  # Full date
    joined_rel = discord.utils.format_dt(member.joined_at, style="R")  # Relative

    # Roles (exclude @everyone)
    roles = [r for r in member.roles if r != ctx.guild.default_role]
    if roles:
        # Show top role first, then the rest alphabetically
        top = member.top_role if member.top_role != ctx.guild.default_role else None
        other = sorted([r for r in roles if r != top], key=lambda x: x.position, reverse=True)
        role_line = ", ".join([top.mention] + [r.mention for r in other]) if top else ", ".join(r.mention for r in other)
    else:
        role_line = "No roles"

    await ctx.send(
        f"**{member}** joined {joined_abs} ({joined_rel})\n"
        f"**Roles:** {role_line}"
    )


# -------- Error Handler (nice messages) --------

@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("I’m brave, not lawless — you’re missing permissions for that. 🛡️")
    elif isinstance(error, commands.MemberNotFound) or isinstance(error, BadArgument):
        await ctx.send("I can’t find that member. Try mentioning them or use an exact server name.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`. Try `$help`.")
    else:
        await ctx.send(f"Uh oh, I tripped on a cable: `{error}` {VICTORY}")






# -------- Run --------
if TOKEN == "REPLACE_ME_WITH_ENV_VAR":
    raise SystemExit("Set DISCORD_TOKEN env var instead of hardcoding your token.")
bot.run(TOKEN)





