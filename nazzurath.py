import discord
from discord.ext import commands
from discord import app_commands
import re
import json
import os
import random
from collections import defaultdict
from dotenv import load_dotenv

# --- ENV + TOKEN ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# --- CONFIG ---
AVRAE_USER_ID = 261302296103747584
FORWARD_CHANNEL_ID = 1360707370732486868
TRUSTED_ROLE_ID = 998391075905474630
ADMIN_ROLE_ID = 998390105217716296
QUIP_FILE = 'quips.json'

CRIT_SUCCESS_EMOJI = discord.PartialEmoji(name="criticalSuccess", id=1361065140031848479)
CRIT_FAIL_EMOJI = discord.PartialEmoji(name="criticalFailure", id=1361065894339543284)

nat20_pattern = re.compile(r'\(\**?20\**?\)')
nat1_pattern = re.compile(r'\(\**?1\**?\)')
emoji_success_pattern = re.compile(r':?criticalSuccess:?|<:criticalSuccess:\d+>')
emoji_fail_pattern = re.compile(r':?criticalFailure:?|<:criticalFailure:\d+>')

EMBED_COLORS = {
    'Warning': discord.Color.red(),
    'Update': discord.Color.blue(),
    'Announcement': discord.Color.green(),
    'Ideas': discord.Color.purple(),
    'Good News': discord.Color.gold(),
    'Greetings': discord.Color.teal()
}

reaction_tracker = defaultdict(lambda: {'success': set(), 'fail': set()})

# --- INTENTS + BOT ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

# --- QUIPS ---
def load_quips():
    if os.path.exists(QUIP_FILE):
        with open(QUIP_FILE, 'r') as f:
            return json.load(f)
    return []

def save_quips(quips):
    with open(QUIP_FILE, 'w') as f:
        json.dump(quips, f, indent=4)

# --- ROLE CHECK ---
async def has_admin_role(interaction: discord.Interaction):
    return any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)

# --- SLASH COMMANDS ---
@bot.tree.command(name="announce", description="Announce a message in a channel")
@app_commands.describe(
    channel="Channel to send the announcement",
    desc="Main description of the embed",
    title="Optional title for the announcement",
    color="Embed color type",
    add_update_prefix="Add 'Update: ' to the title",
    image="URL of the image to include",
    thumbnail="URL of the thumbnail image",
    footer="Footer text to include",
    timestamp="Add timestamp to the embed",
    field1_title="Title for Field 1", field1_value="Value for Field 1",
    field2_title="Title for Field 2", field2_value="Value for Field 2",
    field3_title="Title for Field 3", field3_value="Value for Field 3",
    field4_title="Title for Field 4", field4_value="Value for Field 4",
    field5_title="Title for Field 5", field5_value="Value for Field 5",
)
async def announce(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    desc: str,
    color: str = 'Announcement',
    add_update_prefix: bool = False,
    image: str = None,
    thumbnail: str = None,
    footer: str = None,
    timestamp: bool = False,
    field1_title: str = None, field1_value: str = None,
    field2_title: str = None, field2_value: str = None,
    field3_title: str = None, field3_value: str = None,
    field4_title: str = None, field4_value: str = None,
    field5_title: str = None, field5_value: str = None
):
    if not await has_admin_role(interaction):
        return await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)

    quips = load_quips()
    quip_text = f"\n\n{random.choice(quips)}" if quips else ""

    if add_update_prefix and title:
        title = f"Update: {title}"

    embed_color = EMBED_COLORS.get(color, discord.Color.green())
    embed = discord.Embed(title=title if title else discord.Embed.Empty, description=desc, color=embed_color)

    for i, (f_title, f_value) in enumerate([
        (field1_title, field1_value),
        (field2_title, field2_value),
        (field3_title, field3_value),
        (field4_title, field4_value),
        (field5_title, field5_value)
    ], start=1):
        if f_title and f_value:
            embed.add_field(name=f_title, value=f_value, inline=False)

    if quip_text:
        embed.add_field(name="Quip", value=quip_text.strip(), inline=False)

    if image:
        embed.set_image(url=image)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if footer:
        embed.set_footer(text=footer)
    if timestamp:
        embed.timestamp = discord.utils.utcnow()

    await channel.send(embed=embed)
    await interaction.response.send_message(f"📢 Sent to {channel.mention}", ephemeral=True)


@bot.tree.command(name="announce_quip", description="Add a quip to the collection")
async def announce_quip(interaction: discord.Interaction, quip: str):
    if not await has_admin_role(interaction):
        return await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)

    quips = load_quips()
    quips.append(quip)
    save_quips(quips)
    await interaction.response.send_message(f"💬 Quip added: {quip}", ephemeral=True)

# --- CRITICAL ROLL DETECTION ---
@bot.event
async def on_message(message):
    await bot.process_commands(message)  # ensure slash commands still work

    if message.author.id != AVRAE_USER_ID:
        return
    print(f"Author ID: {message.author.id} | Expected: {AVRAE_USER_ID}")
    print(f"Embeds: {message.embeds}")


    print(f"👀 Received Avrae message {message.id} from {message.author.name}")
    found_nat20 = False
    found_nat1 = False

    if not message.embeds:
        print("⚠️ No embeds found.")
        return

    for embed in message.embeds:
        text_blocks = []
        if embed.description:
            text_blocks.append(embed.description)
        for field in embed.fields:
            text_blocks.append(field.value)

        for text in text_blocks:
            print(f"🔍 Scanning text: {text}")
            if nat20_pattern.search(text) or emoji_success_pattern.search(text):
                found_nat20 = True
            if nat1_pattern.search(text) or emoji_fail_pattern.search(text):
                found_nat1 = True

        if found_nat20:
            await message.add_reaction(CRIT_SUCCESS_EMOJI)
        if found_nat1:
            await message.add_reaction(CRIT_FAIL_EMOJI)

        if found_nat20 or found_nat1:
            msg_type = (
                "both" if found_nat20 and found_nat1 else
                "success" if found_nat20 else
                "fail"
            )
            await forward_embed(message, embed, msg_type)
            return

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return

    if message.author.id != AVRAE_USER_ID:
        return

    guild = channel.guild
    try:
        member = await guild.fetch_member(payload.user_id)
    except discord.NotFound:
        return

    if member.bot:
        return

    if TRUSTED_ROLE_ID not in [role.id for role in member.roles]:
        print(f"🚫 User {member.name} lacks trusted role")
        return

    emoji = payload.emoji
    if emoji == CRIT_SUCCESS_EMOJI:
        reaction_tracker[message.id]['success'].add(member.id)
    elif emoji == CRIT_FAIL_EMOJI:
        reaction_tracker[message.id]['fail'].add(member.id)
    else:
        return

    print(f"✅ Trusted reaction by {member.name}: {emoji}")

    for key, label in [('success', 'success'), ('fail', 'fail')]:
        if len(reaction_tracker[message.id][key]) >= 1:
            embed = message.embeds[0] if message.embeds else None
            await forward_embed(message, embed, label)
            reaction_tracker[message.id][key].clear()

# --- FORWARD EMBED ---
async def forward_embed(original_message, embed, message_type):
    channel = bot.get_channel(FORWARD_CHANNEL_ID)
    if not channel:
        print("🚫 Forward channel not found.")
        return

    msg_text = {
        "success": f"{CRIT_SUCCESS_EMOJI} **Critical success detected!**",
        "fail": f"{CRIT_FAIL_EMOJI} **Critical failure detected!**",
        "both": f"{CRIT_SUCCESS_EMOJI} {CRIT_FAIL_EMOJI} **Critical success and failure detected!**"
    }.get(message_type, "**Roll detected!**")

    await channel.send(f"{msg_text}\n[Jump to message]({original_message.jump_url})", embed=embed)

# --- BOT READY ---
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user} — Slash commands synced!")

# --- RUN ---
if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ DISCORD_TOKEN missing in .env")
