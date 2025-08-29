# logic.py
import random
import re
import string
import secrets

def gen_pass(pass_length: int) -> str:
    # Stronger: ensure at least one of each class; clamp to 8–64
    pass_length = max(8, min(pass_length, 64))
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits, "+-/*!&$#?=@<>"]
    # ensure one of each
    base = [secrets.choice(p) for p in pools]
    # fill the rest
    allchars = "".join(pools)
    base += [secrets.choice(allchars) for _ in range(pass_length - len(base))]
    random.shuffle(base)
    return "".join(base)

def eight_ball() -> str:
    replies = [
        "It is certain.", "Without a doubt.", "You may rely on it.", "Yes — definitely.",
        "As I see it, yes.", "Most likely.", "Signs point to yes.",
        "Reply hazy, try again.", "Ask again later.", "Better not tell you now.",
        "Don’t count on it.", "My reply is no.", "Very doubtful."
    ]
    return random.choice(replies)

def coin_flip() -> str:
    return random.choice(["Heads", "Tails"])

def roll_dice(dice_text: str):
    """
    Parse NdM like '2d6' -> roll two six-sided dice.
    Returns (total, [individual rolls]).
    """
    m = re.fullmatch(r"\s*(\d{1,3})d(\d{1,3})\s*", dice_text)
    if not m:
        raise ValueError("Bad dice format")
    n = max(1, min(int(m.group(1)), 100))
    sides = max(2, min(int(m.group(2)), 1000))
    rolls = [random.randint(1, sides) for _ in range(n)]
    return sum(rolls), rolls
