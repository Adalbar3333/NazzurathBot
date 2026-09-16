from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from collections import defaultdict
from datetime import datetime, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from github_service import GitHubError, GitHubService, Proposal
from scheduling_service import (
    MAX_NOTIFICATION_ATTEMPTS,
    GuildMemberRecord,
    SchedulingNotification,
    SchedulingService,
    render_notification,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nazzurath")


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value else default
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a Discord numeric ID") from exc


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


TOKEN = os.getenv("DISCORD_TOKEN", "")
HOMEBREW_CHANNEL_ID = env_int("HOMEBREW_CHANNEL_ID", 1548461625865015466)
HOMEBREW_ROLE_ID = env_int("HOMEBREW_ROLE_ID", 0)
PORT = env_int("PORT", 8080)

AVRAE_USER_ID = env_int("AVRAE_USER_ID", 261302296103747584)
FORWARD_CHANNEL_ID = env_int("FORWARD_CHANNEL_ID", 1360707370732486868)
TRUSTED_ROLE_ID = env_int("TRUSTED_ROLE_ID", 998391075905474630)
ADMIN_ROLE_ID = env_int("ADMIN_ROLE_ID", 998390105217716296)
DISCORD_GUILD_ID = env_int("DISCORD_GUILD_ID", 0)
DISCORD_DM_ROLE_ID = env_int("DISCORD_DM_ROLE_ID", 0)
QUIP_FILE = Path(os.getenv("QUIP_FILE", str(BASE_DIR / "quips.json")))

CENTRAL = ZoneInfo("America/Chicago")
POLL_SECONDS = max(30, env_int("GITHUB_POLL_SECONDS", 60))
ANNOUNCE_EXISTING_SUBMISSIONS = env_bool("ANNOUNCE_EXISTING_SUBMISSIONS")
NOTIFICATION_POLL_SECONDS = max(5, env_int("SCHEDULING_POLL_SECONDS", 15))

CRIT_SUCCESS_EMOJI = discord.PartialEmoji(name="criticalSuccess", id=1361065140031848479)
CRIT_FAIL_EMOJI = discord.PartialEmoji(name="criticalFailure", id=1361065894339543284)

nat20_pattern = re.compile(r"\(\**?20\**?\)")
nat1_pattern = re.compile(r"\(\**?1\**?\)")
emoji_success_pattern = re.compile(r":?criticalSuccess:?|<:criticalSuccess:\d+>")
emoji_fail_pattern = re.compile(r":?criticalFailure:?|<:criticalFailure:\d+>")

EMBED_COLORS = {
    "Warning": discord.Color.red(),
    "Update": discord.Color.blue(),
    "Announcement": discord.Color.green(),
    "Ideas": discord.Color.purple(),
    "Good News": discord.Color.gold(),
    "Greetings": discord.Color.teal(),
}

reaction_tracker = defaultdict(lambda: {"success": set(), "fail": set()})


def load_quips() -> list[str]:
    try:
        with QUIP_FILE.open(encoding="utf-8") as file:
            value = json.load(file)
            return value if isinstance(value, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def save_quips(quips: list[str]) -> None:
    QUIP_FILE.write_text(json.dumps(quips, indent=2) + "\n", encoding="utf-8")


def shorten(value: str, length: int) -> str:
    clean = re.sub(r"\s+", " ", value or "").strip()
    return clean if len(clean) <= length else clean[: length - 1].rstrip() + "…"


def chunk_lines(lines: list[str], limit: int = 3900) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for original in lines:
        line = shorten(original, limit)
        added = len(line) + (1 if current else 0)
        if current and size + added > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line) + (1 if size else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


class NazzurathBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.reactions = True
        super().__init__(command_prefix="!", intents=intents)
        self.github = GitHubService.from_env()
        self.scheduling = SchedulingService.from_env()
        self.health_runner: web.AppRunner | None = None
        self.report_lock = asyncio.Lock()
        self.completed_vote_dates: set[str] = set()
        self.completed_pages_dates: set[str] = set()
        self.known_proposal_ids: set[int] | None = None

    async def setup_hook(self) -> None:
        await self.github.start()
        await self.scheduling.start()
        self.add_view(InvitationResponseView(self))
        self.add_view(SessionResponseView(self))
        await self.start_health_server()
        homebrew_monitor.change_interval(seconds=POLL_SECONDS)
        homebrew_monitor.start()
        scheduled_reports.start()
        scheduling_notification_worker.change_interval(seconds=NOTIFICATION_POLL_SECONDS)
        scheduling_notification_worker.start()
        scheduling_member_sync.start()
        await self.tree.sync()

    async def close(self) -> None:
        if homebrew_monitor.is_running():
            homebrew_monitor.cancel()
        if scheduled_reports.is_running():
            scheduled_reports.cancel()
        if scheduling_notification_worker.is_running():
            scheduling_notification_worker.cancel()
        if scheduling_member_sync.is_running():
            scheduling_member_sync.cancel()
        await self.github.close()
        await self.scheduling.close()
        if self.health_runner:
            await self.health_runner.cleanup()
        await super().close()

    async def start_health_server(self) -> None:
        async def health(_: web.Request) -> web.Response:
            return web.json_response({
                "ok": True,
                "discordReady": self.is_ready(),
                "githubConfigured": self.github.configured,
                "scheduling": {
                    "configured": self.scheduling.configured,
                    "databaseHealthy": self.scheduling.database_healthy,
                    "guildConfigured": bool(DISCORD_GUILD_ID),
                    "dmRoleConfigured": bool(DISCORD_DM_ROLE_ID),
                    "notificationWorkerRunning": scheduling_notification_worker.is_running(),
                    "memberSyncWorkerRunning": scheduling_member_sync.is_running(),
                    "lastNotificationRun": (
                        self.scheduling.last_notification_run_at.isoformat()
                        if self.scheduling.last_notification_run_at else None
                    ),
                    "lastMemberSync": (
                        self.scheduling.last_member_sync_at.isoformat()
                        if self.scheduling.last_member_sync_at else None
                    ),
                    "lastErrorType": self.scheduling.last_error,
                },
            })

        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        self.health_runner = web.AppRunner(app)
        await self.health_runner.setup()
        site = web.TCPSite(self.health_runner, "0.0.0.0", PORT)
        await site.start()
        log.info("Health server listening on port %s", PORT)

    async def homebrew_channel(self) -> discord.TextChannel:
        channel = self.get_channel(HOMEBREW_CHANNEL_ID)
        if channel is None:
            channel = await self.fetch_channel(HOMEBREW_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"HOMEBREW_CHANNEL_ID {HOMEBREW_CHANNEL_ID} is not a text channel")
        return channel


bot = NazzurathBot()


async def handle_scheduling_response(
    interaction: discord.Interaction,
    response_kind: str,
    response: str,
) -> None:
    if interaction.message is None:
        await interaction.response.send_message("This scheduling message is no longer available.", ephemeral=True)
        return
    try:
        outcome = await bot.scheduling.respond_to_message(
            discord_message_id=str(interaction.message.id),
            actor_discord_id=str(interaction.user.id),
            response_kind=response_kind,
            response=response,
        )
    except Exception as exc:
        log.error(
            "Scheduling response failed for message=%s user=%s: %s",
            interaction.message.id,
            interaction.user.id,
            type(exc).__name__,
        )
        await interaction.response.send_message(
            "I could not save that response. Please try again or use the scheduling website.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(outcome.message, ephemeral=True)
    if outcome.ok and outcome.status:
        disabled_view: discord.ui.View
        if response_kind == "invitation":
            disabled_view = InvitationResponseView(bot, disabled=True)
        else:
            message_url = next(
                (embed.url for embed in interaction.message.embeds if embed.url),
                None,
            )
            disabled_view = SessionResponseView(bot, disabled=True, url=message_url)
        try:
            await interaction.message.edit(view=disabled_view)
        except discord.HTTPException:
            log.warning("Could not disable handled scheduling buttons for message=%s", interaction.message.id)


class InvitationResponseView(discord.ui.View):
    def __init__(self, client: NazzurathBot, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.client = client
        if disabled:
            for item in self.children:
                item.disabled = True

    @discord.ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        custom_id="schedule:invitation:accept:v1",
    )
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await handle_scheduling_response(interaction, "invitation", "accepted")

    @discord.ui.button(
        label="Decline",
        style=discord.ButtonStyle.secondary,
        custom_id="schedule:invitation:decline:v1",
    )
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await handle_scheduling_response(interaction, "invitation", "declined")


class SessionResponseView(discord.ui.View):
    def __init__(
        self,
        client: NazzurathBot,
        disabled: bool = False,
        url: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.client = client
        self.add_item(
            discord.ui.Button(
                label="Open schedule",
                style=discord.ButtonStyle.link,
                url=url or f"{client.scheduling.website_url}/schedule",
            )
        )
        if disabled:
            for item in self.children:
                item.disabled = True

    @discord.ui.button(
        label="Attending",
        style=discord.ButtonStyle.success,
        custom_id="schedule:session:accept:v1",
    )
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await handle_scheduling_response(interaction, "session", "accepted")

    @discord.ui.button(
        label="Decline",
        style=discord.ButtonStyle.secondary,
        custom_id="schedule:session:decline:v1",
    )
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await handle_scheduling_response(interaction, "session", "declined")


def scheduling_view(notification: SchedulingNotification) -> discord.ui.View | None:
    rendered = render_notification(notification, bot.scheduling.website_url)
    if rendered.response_kind == "invitation":
        return InvitationResponseView(bot)
    if rendered.response_kind == "session":
        return SessionResponseView(bot, url=rendered.url)
    return None


async def deliver_scheduling_notification(notification: SchedulingNotification) -> None:
    rendered = render_notification(notification, bot.scheduling.website_url)
    recipient_id = int(notification.recipient_discord_id)
    user = bot.get_user(recipient_id)
    if user is None:
        user = await bot.fetch_user(recipient_id)
    embed = discord.Embed(
        title=shorten(rendered.title, 256),
        description=shorten(rendered.description, 4096),
        url=rendered.url,
        color=discord.Color.from_rgb(146, 113, 63),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="Tazzurath scheduling")
    message = await user.send(
        embed=embed,
        view=scheduling_view(notification),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await bot.scheduling.mark_notification_sent(notification.id, str(message.id))
    log.info(
        "Delivered scheduling notification id=%s event=%s recipient=%s",
        notification.id,
        notification.event_type,
        notification.recipient_discord_id,
    )


async def has_admin_role(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and any(
        role.id == ADMIN_ROLE_ID for role in interaction.user.roles
    )


@bot.tree.command(name="announce", description="Announce a message in a channel")
@app_commands.describe(desc="The announcement text", add_update_prefix="Prefix the title with Update")
async def announce(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    desc: str,
    color: str = "Announcement",
    add_update_prefix: bool = False,
    image: Optional[str] = None,
    thumbnail: Optional[str] = None,
    footer: Optional[str] = None,
    timestamp: bool = False,
) -> None:
    if not await has_admin_role(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    quips = load_quips()
    if add_update_prefix:
        title = f"Update: {title}"
    embed = discord.Embed(
        title=shorten(title, 256),
        description=shorten(desc, 4096),
        color=EMBED_COLORS.get(color, discord.Color.green()),
    )
    if quips:
        embed.add_field(name="Quip", value=shorten(random.choice(quips), 1024), inline=False)
    if image:
        embed.set_image(url=image)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if footer:
        embed.set_footer(text=shorten(footer, 2048))
    if timestamp:
        embed.timestamp = discord.utils.utcnow()

    await channel.send(embed=embed)
    await interaction.response.send_message(f"📢 Sent to {channel.mention}", ephemeral=True)


@bot.tree.command(name="announce_quip", description="Add a quip to the collection")
async def announce_quip(interaction: discord.Interaction, quip: str) -> None:
    if not await has_admin_role(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return
    quips = load_quips()
    quips.append(quip)
    save_quips(quips)
    await interaction.response.send_message("💬 Quip added.", ephemeral=True)


@bot.event
async def on_message(message: discord.Message) -> None:
    await bot.process_commands(message)
    if message.author.id != AVRAE_USER_ID or not message.embeds:
        return

    for embed in message.embeds:
        text_blocks = [embed.description] if embed.description else []
        text_blocks.extend(field.value for field in embed.fields)
        found_nat20 = any(nat20_pattern.search(text) or emoji_success_pattern.search(text) for text in text_blocks)
        found_nat1 = any(nat1_pattern.search(text) or emoji_fail_pattern.search(text) for text in text_blocks)

        if found_nat20:
            await message.add_reaction(CRIT_SUCCESS_EMOJI)
        if found_nat1:
            await message.add_reaction(CRIT_FAIL_EMOJI)
        if found_nat20 or found_nat1:
            message_type = "both" if found_nat20 and found_nat1 else "success" if found_nat20 else "fail"
            await forward_embed(message, embed, message_type)
            return


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if bot.user is None or payload.user_id == bot.user.id or payload.guild_id is None:
        return
    channel = bot.get_channel(payload.channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden):
        return
    if message.author.id != AVRAE_USER_ID or not message.embeds:
        return

    guild = channel.guild
    try:
        member = payload.member or guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
    except discord.NotFound:
        return
    if TRUSTED_ROLE_ID not in [role.id for role in member.roles]:
        return

    if payload.emoji == CRIT_SUCCESS_EMOJI:
        reaction_tracker[message.id]["success"].add(member.id)
        key = "success"
    elif payload.emoji == CRIT_FAIL_EMOJI:
        reaction_tracker[message.id]["fail"].add(member.id)
        key = "fail"
    else:
        return

    if reaction_tracker[message.id][key]:
        await forward_embed(message, message.embeds[0], key)
        reaction_tracker[message.id][key].clear()


async def forward_embed(original_message: discord.Message, embed: discord.Embed, message_type: str) -> None:
    channel = bot.get_channel(FORWARD_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return
    message_text = {
        "success": f"{CRIT_SUCCESS_EMOJI} **Critical success detected!**",
        "fail": f"{CRIT_FAIL_EMOJI} **Critical failure detected!**",
        "both": f"{CRIT_SUCCESS_EMOJI} {CRIT_FAIL_EMOJI} **Critical success and failure detected!**",
    }.get(message_type, "**Roll detected!**")
    await channel.send(f"{message_text}\n[Jump to message]({original_message.jump_url})", embed=embed)


async def announced_issue_numbers(channel: discord.TextChannel, after: datetime | None = None) -> set[int]:
    numbers: set[int] = set()
    async for message in channel.history(limit=None if after else 200, after=after):
        for embed in message.embeds:
            footer = embed.footer.text or ""
            match = re.fullmatch(r"NazzurathBot:homebrew:(\d+)", footer)
            if match:
                numbers.add(int(match.group(1)))
    return numbers


async def report_marker_exists(channel: discord.TextChannel, marker: str, after: datetime) -> bool:
    async for message in channel.history(limit=100, after=after):
        if any(embed.footer.text == marker for embed in message.embeds):
            return True
    return False


async def announce_proposal(channel: discord.TextChannel, proposal: Proposal) -> None:
    description = shorten(proposal.body, 1000) or "No description provided."
    embed = discord.Embed(
        title=shorten(proposal.title, 256),
        url=bot.github.registry_url,
        description=description,
        color=discord.Color.from_rgb(146, 113, 63),
        timestamp=proposal.created_at,
    )
    embed.set_author(name="New Tazzurath homebrew submission")
    embed.add_field(name="Submitted by", value=shorten(proposal.author, 1024), inline=True)
    embed.add_field(name="Destination", value=shorten(proposal.destination, 1024), inline=True)
    if proposal.source_url:
        embed.add_field(name="Submitted source", value=f"[Open source material]({proposal.source_url})", inline=False)
    embed.add_field(
        name="Vote",
        value=f"[Open the homebrew registry]({bot.github.registry_url}) to review and vote.",
        inline=False,
    )
    embed.set_footer(text=f"NazzurathBot:homebrew:{proposal.number}")
    role_ping = f"<@&{HOMEBREW_ROLE_ID}>" if HOMEBREW_ROLE_ID else None
    await channel.send(
        content=role_ping,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True),
    )


async def send_vote_report(channel: discord.TextChannel, now: datetime) -> None:
    marker = f"NazzurathBot:vote-report:{now.date().isoformat()}"
    midnight = datetime.combine(now.date(), time.min, tzinfo=CENTRAL)
    if await report_marker_exists(channel, marker, midnight):
        return
    proposals = await bot.github.list_proposals(include_comments=True)
    voteable = [proposal for proposal in proposals if proposal.status not in {"approved", "disapproved"}]
    if not voteable:
        log.info("No voteable proposals; no 6 AM message sent")
        return

    lines = [
        f"• [#{item.number} — {discord.utils.escape_markdown(shorten(item.title, 110))}]({bot.github.registry_url}) "
        f"— {item.status_label}; **{item.approve}** approve / **{item.disapprove}** disapprove"
        for item in voteable
    ]
    for index, description in enumerate(chunk_lines(lines)):
        embed = discord.Embed(
            title="Current homebrew submissions to vote on" if index == 0 else "Homebrew submissions (continued)",
            description=description,
            url=bot.github.registry_url,
            color=discord.Color.gold(),
            timestamp=now,
        )
        embed.set_footer(text=marker)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def send_pages_report(channel: discord.TextChannel, now: datetime) -> None:
    marker = f"NazzurathBot:pages-report:{now.date().isoformat()}"
    midnight = datetime.combine(now.date(), time.min, tzinfo=CENTRAL)
    if await report_marker_exists(channel, marker, midnight):
        return
    pages = await bot.github.new_pages_between(midnight, now)
    if not pages:
        log.info("No new pages; no noon message sent")
        return

    lines = [f"• [{discord.utils.escape_markdown(shorten(page.title, 120))}]({page.url})" for page in pages]
    for index, description in enumerate(chunk_lines(lines)):
        embed = discord.Embed(
            title=f"New Tazzurath pages today ({len(pages)})" if index == 0 else "New pages (continued)",
            description=description,
            color=discord.Color.blue(),
            timestamp=now,
        )
        embed.set_footer(text=marker)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@tasks.loop(seconds=60)
async def homebrew_monitor() -> None:
    if not bot.github.configured:
        return
    try:
        channel = await bot.homebrew_channel()
        proposals = await bot.github.list_proposals(include_comments=False)
        if bot.known_proposal_ids is None:
            earliest = min((proposal.created_at for proposal in proposals), default=None)
            bot.known_proposal_ids = await announced_issue_numbers(channel, after=earliest)
            if not bot.known_proposal_ids and not ANNOUNCE_EXISTING_SUBMISSIONS:
                bot.known_proposal_ids.update(proposal.number for proposal in proposals)
                log.info("Recorded %s existing proposals without announcing them", len(proposals))
                return
        for proposal in sorted(proposals, key=lambda item: item.number):
            if proposal.number not in bot.known_proposal_ids:
                await announce_proposal(channel, proposal)
                bot.known_proposal_ids.add(proposal.number)
    except (GitHubError, discord.DiscordException, RuntimeError) as exc:
        log.error("Homebrew monitor failed: %s", exc)


@homebrew_monitor.before_loop
async def before_homebrew_monitor() -> None:
    await bot.wait_until_ready()


@tasks.loop(seconds=60)
async def scheduled_reports() -> None:
    if not bot.github.configured or bot.report_lock.locked():
        return
    now = datetime.now(CENTRAL)
    date_key = now.date().isoformat()
    try:
        async with bot.report_lock:
            channel = await bot.homebrew_channel()
            if now.hour >= 6 and date_key not in bot.completed_vote_dates:
                await send_vote_report(channel, now)
                bot.completed_vote_dates.add(date_key)
            if now.hour >= 12 and date_key not in bot.completed_pages_dates:
                await send_pages_report(channel, now)
                bot.completed_pages_dates.add(date_key)
    except (GitHubError, discord.DiscordException, RuntimeError) as exc:
        log.error("Scheduled report failed: %s", exc)


@scheduled_reports.before_loop
async def before_scheduled_reports() -> None:
    await bot.wait_until_ready()


@tasks.loop(seconds=15)
async def scheduling_notification_worker() -> None:
    if not bot.scheduling.configured:
        return
    try:
        await bot.scheduling.release_stale_claims()
        notifications = await bot.scheduling.claim_due_notifications()
    except Exception as exc:
        log.error("Scheduling notification claim failed: %s", type(exc).__name__)
        return

    for notification in notifications:
        try:
            await deliver_scheduling_notification(notification)
        except (discord.Forbidden, discord.NotFound) as exc:
            log.warning(
                "Scheduling DM permanently unavailable id=%s recipient=%s error=%s",
                notification.id,
                notification.recipient_discord_id,
                type(exc).__name__,
            )
            try:
                await bot.scheduling.mark_notification_failed(
                    notification,
                    f"{type(exc).__name__}: Discord private delivery unavailable",
                )
            except Exception as database_error:
                log.error(
                    "Could not persist permanent scheduling failure id=%s: %s",
                    notification.id,
                    type(database_error).__name__,
                )
        except Exception as exc:
            try:
                if notification.attempt_count >= MAX_NOTIFICATION_ATTEMPTS:
                    await bot.scheduling.mark_notification_failed(
                        notification,
                        f"{type(exc).__name__}: delivery retries exhausted",
                    )
                    log.error(
                        "Scheduling notification exhausted retries id=%s event=%s",
                        notification.id,
                        notification.event_type,
                    )
                else:
                    await bot.scheduling.mark_notification_retry(
                        notification.id,
                        notification.attempt_count,
                        f"{type(exc).__name__}: transient delivery failure",
                    )
                    log.warning(
                        "Scheduling notification will retry id=%s attempt=%s error=%s",
                        notification.id,
                        notification.attempt_count,
                        type(exc).__name__,
                    )
            except Exception as database_error:
                log.error(
                    "Could not persist scheduling retry id=%s: %s",
                    notification.id,
                    type(database_error).__name__,
                )


@scheduling_notification_worker.before_loop
async def before_scheduling_notification_worker() -> None:
    await bot.wait_until_ready()


@tasks.loop(minutes=10)
async def scheduling_member_sync() -> None:
    if not bot.scheduling.configured or not DISCORD_GUILD_ID:
        return
    try:
        guild = bot.get_guild(DISCORD_GUILD_ID)
        if guild is None:
            guild = await bot.fetch_guild(DISCORD_GUILD_ID)
        members = [member async for member in guild.fetch_members(limit=None) if not member.bot]
        records = [
            GuildMemberRecord(
                discord_id=str(member.id),
                username=member.name,
                display_name=member.display_name,
                avatar_url=str(member.display_avatar.url) if member.display_avatar else None,
                role_ids=tuple(str(role.id) for role in member.roles),
            )
            for member in members
        ]
        synced = await bot.scheduling.sync_guild_members(records)
        log.info("Synchronized %s Discord guild members", synced)
    except Exception as exc:
        log.exception("Scheduling member synchronization failed: %s", type(exc).__name__)


@scheduling_member_sync.before_loop
async def before_scheduling_member_sync() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    if not bot.github.configured:
        log.warning("GitHub integration is disabled: set GITHUB_TOKEN")
    if not bot.scheduling.configured:
        log.warning("Scheduling integration is disabled: set DATABASE_URL")
    elif not DISCORD_GUILD_ID:
        log.warning("Scheduling member synchronization is disabled: set DISCORD_GUILD_ID")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and add the token.")
    bot.run(TOKEN, log_handler=None)
