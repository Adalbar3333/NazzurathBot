from __future__ import annotations

import json
import logging
import os
import random
import re
import time as monotonic_time
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from github_service import (
    GitHubError,
    GitHubService,
    Proposal,
    proposals_resolved_since,
)
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
ping_user_cooldowns: dict[int, float] = {}
ping_role_cooldowns: dict[str, float] = {}


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

    async def setup_hook(self) -> None:
        await self.github.start()
        await self.scheduling.start()
        self.add_view(InvitationResponseView(self))
        self.add_view(SessionResponseView(self))
        self.add_view(SessionManagerView(self))
        await self.start_health_server()
        scheduling_notification_worker.change_interval(seconds=NOTIFICATION_POLL_SECONDS)
        scheduling_notification_worker.start()
        scheduling_member_sync.start()
        await self.tree.sync()

    async def close(self) -> None:
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
        elif response_kind == "session_manager":
            message_url = next(
                (embed.url for embed in interaction.message.embeds if embed.url),
                None,
            )
            disabled_view = SessionManagerView(bot, disabled=True, url=message_url)
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


class ConfirmManagerMessageCancellationView(discord.ui.View):
    def __init__(self, actor_id: int, message: discord.Message) -> None:
        super().__init__(timeout=120)
        self.actor_id = actor_id
        self.message = message

    @discord.ui.button(label="Keep session", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This confirmation belongs to another member.", ephemeral=True)
            return
        await interaction.response.edit_message(content="The scheduled date was not changed.", view=None)

    @discord.ui.button(label="Confirm cancellation", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This confirmation belongs to another member.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            outcome = await bot.scheduling.respond_to_message(
                discord_message_id=str(self.message.id),
                actor_discord_id=str(interaction.user.id),
                response_kind="session_manager",
                response="cancelled",
            )
        except Exception as exc:
            log.error("Scheduling-message cancellation failed: %s", type(exc).__name__)
            await interaction.edit_original_response(
                content="I could not cancel that session. Please try again or use the scheduling website.",
                view=None,
            )
            return
        if outcome.ok and outcome.status:
            message_url = next((embed.url for embed in self.message.embeds if embed.url), None)
            try:
                await self.message.edit(view=SessionManagerView(bot, disabled=True, url=message_url))
            except discord.HTTPException:
                log.warning("Could not disable cancelled session controls for message=%s", self.message.id)
        await interaction.edit_original_response(content=outcome.message, view=None)


class SessionManagerView(discord.ui.View):
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
                label="Edit on website",
                style=discord.ButtonStyle.link,
                url=url or f"{client.scheduling.website_url}/schedule",
            )
        )
        if disabled:
            for item in self.children:
                item.disabled = True

    @discord.ui.button(
        label="Cancel session",
        style=discord.ButtonStyle.danger,
        custom_id="schedule:session-manager:cancel:v1",
    )
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.message is None:
            await interaction.response.send_message("This scheduling message is no longer available.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Cancel this scheduled date? Everyone on its roster will be notified.",
            view=ConfirmManagerMessageCancellationView(interaction.user.id, interaction.message),
            ephemeral=True,
        )


def scheduling_view(notification: SchedulingNotification) -> discord.ui.View | None:
    rendered = render_notification(notification, bot.scheduling.website_url)
    if rendered.response_kind == "invitation":
        return InvitationResponseView(bot)
    if rendered.response_kind == "session":
        return SessionResponseView(bot, url=rendered.url)
    if rendered.response_kind == "session_manager":
        return SessionManagerView(bot, url=rendered.url)
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
    embed.set_footer(
        text="Tazzurath homebrew"
        if notification.event_type.startswith("homebrew_")
        else "Tazzurath scheduling"
    )
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


def managed_session_label(session: dict[str, Any]) -> str:
    starts_at = session.get("starts_at")
    if isinstance(starts_at, datetime):
        date_label = starts_at.strftime("%b %d, %Y")
    else:
        date_label = "Scheduled date"
    return shorten(f"{session.get('group_name') or 'Campaign'} — {date_label}", 100)


class ConfirmSessionCancellationView(discord.ui.View):
    def __init__(self, actor_id: int, session: dict[str, Any]) -> None:
        super().__init__(timeout=120)
        self.actor_id = actor_id
        self.session = session

    @discord.ui.button(label="Keep session", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This confirmation belongs to another member.", ephemeral=True)
            return
        await interaction.response.edit_message(content="The scheduled date was not changed.", view=None)

    @discord.ui.button(label="Confirm cancellation", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This confirmation belongs to another member.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            outcome = await bot.scheduling.cancel_managed_session(
                actor_discord_id=str(interaction.user.id),
                session_id=str(self.session["id"]),
                expected_revision=int(self.session["revision"]),
            )
        except Exception as exc:
            log.error("Slash-command session cancellation failed: %s", type(exc).__name__)
            await interaction.edit_original_response(
                content="I could not cancel that session. Please try again or use the scheduling website.",
                view=None,
            )
            return
        await interaction.edit_original_response(content=outcome.message, view=None)


class ManagedSessionSelect(discord.ui.Select):
    def __init__(self, actor_id: int, sessions: list[dict[str, Any]]) -> None:
        self.actor_id = actor_id
        self.sessions = {str(session["id"]): session for session in sessions}
        options = []
        for session in sessions:
            starts_at = session.get("starts_at")
            timestamp = (
                starts_at.astimezone(CENTRAL).strftime("%a, %b %d at %I:%M %p Central")
                if isinstance(starts_at, datetime)
                else "Scheduled date"
            )
            options.append(discord.SelectOption(
                label=managed_session_label(session),
                value=str(session["id"]),
                description=shorten(timestamp, 100),
            ))
        super().__init__(
            placeholder="Choose a scheduled date to cancel…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This session list belongs to another member.", ephemeral=True)
            return
        session = self.sessions[self.values[0]]
        starts_at = session.get("starts_at")
        when = f"<t:{int(starts_at.timestamp())}:F>" if isinstance(starts_at, datetime) else "that date"
        await interaction.response.send_message(
            f"Cancel **{session.get('group_name') or 'this campaign'}** on {when}? This will notify its roster.",
            view=ConfirmSessionCancellationView(interaction.user.id, session),
            ephemeral=True,
        )


class ManagedSessionsView(discord.ui.View):
    def __init__(self, actor_id: int, sessions: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.add_item(ManagedSessionSelect(actor_id, sessions))
        self.add_item(discord.ui.Button(
            label="Edit dates on website",
            style=discord.ButtonStyle.link,
            url=f"{bot.scheduling.website_url}/schedule?tab=sessions",
        ))


@bot.tree.command(name="sessions", description="View, edit, or cancel your scheduled campaign dates")
async def scheduled_dates(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        sessions = await bot.scheduling.list_managed_sessions(str(interaction.user.id))
    except Exception as exc:
        log.error("Scheduled-date lookup failed: %s", type(exc).__name__)
        await interaction.followup.send(
            "I could not load scheduled dates. Please try again or use the scheduling website.",
            ephemeral=True,
        )
        return
    if not sessions:
        await interaction.followup.send(
            f"You do not have any future dates to manage. [Open the scheduler]({bot.scheduling.website_url}/schedule).",
            ephemeral=True,
        )
        return
    lines = []
    for session in sessions:
        starts_at = session.get("starts_at")
        ends_at = session.get("ends_at")
        when = f"<t:{int(starts_at.timestamp())}:F>" if isinstance(starts_at, datetime) else "Time unavailable"
        if isinstance(ends_at, datetime):
            when += f"–<t:{int(ends_at.timestamp())}:t>"
        lines.append(f"• **{session.get('group_name') or 'Campaign'}** — {when}")
    embed = discord.Embed(
        title="Your scheduled dates",
        description="\n".join(lines),
        color=discord.Color.from_rgb(146, 113, 63),
    )
    embed.set_footer(text="Choose a date below to cancel it, or open the website to alter it.")
    await interaction.followup.send(
        embed=embed,
        view=ManagedSessionsView(interaction.user.id, sessions),
        ephemeral=True,
    )


async def has_admin_role(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and any(
        role.id == ADMIN_ROLE_ID for role in interaction.user.roles
    )


async def website_role_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id != DISCORD_GUILD_ID or not bot.scheduling.configured:
        return []
    try:
        roles = await bot.scheduling.list_ping_website_roles(current)
    except Exception as exc:
        log.error("Website role autocomplete failed: %s", type(exc).__name__)
        return []
    return [app_commands.Choice(name=role["name"], value=role["id"]) for role in roles]


def ping_message_chunks(role_name: str, actor_mention: str, member_ids: list[str], message: str) -> list[str]:
    safe_message = re.sub(r"@", "@\u200b", shorten(message, 500))
    header = f"**{role_name}** — requested by {actor_mention}"
    if safe_message:
        header += f"\n{safe_message}"
    chunks: list[str] = []
    current = header
    for member_id in member_ids:
        mention = f"<@{member_id}>"
        candidate = f"{current}\n{mention}"
        if len(candidate) > 1950:
            chunks.append(current)
            current = mention
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


@bot.tree.command(name="ping", description="Ping the approved members of a Tazzurath website role")
@app_commands.describe(role="Website role to ping", message="Optional reason for the ping")
@app_commands.autocomplete(role=website_role_autocomplete)
async def ping_website_role(
    interaction: discord.Interaction,
    role: str,
    message: str = "",
) -> None:
    if interaction.guild_id != DISCORD_GUILD_ID or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command is available only in the Tazzurath Discord server.", ephemeral=True)
        return
    if not bot.scheduling.configured:
        await interaction.response.send_message("Website roles are not configured yet.", ephemeral=True)
        return

    now = monotonic_time.monotonic()
    is_admin = await has_admin_role(interaction)
    user_remaining = 60 - (now - ping_user_cooldowns.get(interaction.user.id, 0))
    role_remaining = 300 - (now - ping_role_cooldowns.get(role, 0))
    if user_remaining > 0:
        await interaction.response.send_message(f"Please wait {int(user_remaining) + 1} seconds before using another website-role ping.", ephemeral=True)
        return
    if role_remaining > 0 and not is_admin:
        await interaction.response.send_message(f"That role was pinged recently. Try again in {int(role_remaining) + 1} seconds.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        resolved = await bot.scheduling.resolve_website_role_ping(role)
        if resolved is None:
            await interaction.followup.send("That website role is unavailable or no longer ping-enabled.", ephemeral=True)
            return
        role_record, member_ids = resolved
        current_member_ids = {str(member.id) for member in interaction.guild.members}
        member_ids = [member_id for member_id in member_ids if member_id in current_member_ids]
        if not member_ids:
            await interaction.followup.send(f"**{role_record['name']}** has no approved active members to ping.", ephemeral=True)
            return

        allowed_mentions = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)
        chunks = ping_message_chunks(role_record["name"], interaction.user.mention, member_ids, message)
        await interaction.followup.send(chunks[0], allowed_mentions=allowed_mentions)
        for chunk in chunks[1:]:
            await interaction.channel.send(chunk, allowed_mentions=allowed_mentions)

        ping_user_cooldowns[interaction.user.id] = now
        ping_role_cooldowns[role] = now
        await bot.scheduling.audit_website_role_ping(
            actor_discord_id=str(interaction.user.id),
            role_id=role_record["id"],
            channel_id=str(interaction.channel_id),
            member_count=len(member_ids),
            message=shorten(message, 500),
        )
    except Exception as exc:
        log.error("Website role ping failed: %s", type(exc).__name__)
        await interaction.followup.send("I could not resolve that website role right now.", ephemeral=True)


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


async def latest_report_time(channel: discord.TextChannel, marker_prefix: str) -> datetime | None:
    async for message in channel.history(limit=500):
        if any((embed.footer.text or "").startswith(marker_prefix) for embed in message.embeds):
            return message.created_at
    return None


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


async def send_vote_report(channel: discord.TextChannel, now: datetime) -> bool:
    marker = f"NazzurathBot:vote-report:{now.date().isoformat()}"
    previous_report_at = await latest_report_time(channel, "NazzurathBot:vote-report:")
    resolution_cutoff = previous_report_at or (now - timedelta(days=1))
    proposals = await bot.github.list_proposals(include_comments=True)
    voteable = [proposal for proposal in proposals if proposal.status not in {"approved", "disapproved"}]
    resolved = proposals_resolved_since(proposals, resolution_cutoff)
    if not voteable and not resolved:
        return False

    lines: list[str] = []
    if resolved:
        lines.append("**Resolved since the previous voting update**")
        lines.extend(
            f"• {'✅' if item.status == 'approved' else '❌'} "
            f"[#{item.number} — {discord.utils.escape_markdown(shorten(item.title, 110))}]"
            f"({bot.github.registry_url}/settled#homebrew-post-{item.number}) — "
            f"**{'Approved' if item.status == 'approved' else 'Denied'}**"
            for item in resolved
        )
    if voteable:
        if lines:
            lines.append("")
        lines.append("**Currently open for voting**")
        lines.extend(
            f"• [#{item.number} — {discord.utils.escape_markdown(shorten(item.title, 110))}]"
            f"({bot.github.registry_url}#homebrew-post-{item.number}) "
            f"— {item.status_label}; **{item.approve}** approve / **{item.disapprove}** disapprove"
            for item in voteable
        )
    for index, description in enumerate(chunk_lines(lines)):
        embed = discord.Embed(
            title="Homebrew voting update" if index == 0 else "Homebrew voting update (continued)",
            description=description,
            url=bot.github.registry_url,
            color=discord.Color.gold(),
            timestamp=now,
        )
        embed.set_footer(text=marker)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    return True


async def send_pages_report(channel: discord.TextChannel, now: datetime) -> bool:
    marker = f"NazzurathBot:pages-report:{now.date().isoformat()}"
    midnight = datetime.combine(now.date(), time.min, tzinfo=CENTRAL)
    pages = await bot.github.new_pages_between(midnight, now)
    if not pages:
        return False

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
    return True


async def begin_manual_homebrew_post(interaction: discord.Interaction) -> bool:
    if not await has_admin_role(interaction):
        await interaction.response.send_message("You do not have permission to post homebrew updates.", ephemeral=True)
        return False
    if not bot.github.configured:
        await interaction.response.send_message("The GitHub homebrew connection is not configured.", ephemeral=True)
        return False
    await interaction.response.defer(ephemeral=True, thinking=True)
    return True


@bot.tree.command(name="homebrew_post", description="Post one homebrew submission to the homebrew channel")
@app_commands.describe(issue_number="The homebrew proposal number shown in the registry")
async def homebrew_post(interaction: discord.Interaction, issue_number: int) -> None:
    if not await begin_manual_homebrew_post(interaction):
        return
    try:
        proposals = await bot.github.list_proposals(include_comments=False)
        proposal = next((item for item in proposals if item.number == issue_number), None)
        if proposal is None:
            await interaction.followup.send(f"Homebrew proposal #{issue_number} was not found.", ephemeral=True)
            return
        await announce_proposal(await bot.homebrew_channel(), proposal)
        await interaction.followup.send(f"Posted homebrew proposal #{issue_number}.", ephemeral=True)
    except (GitHubError, discord.DiscordException, RuntimeError) as exc:
        log.error("Manual homebrew post failed: %s", exc)
        await interaction.followup.send("I could not post that homebrew proposal.", ephemeral=True)


@bot.tree.command(name="homebrew_voting_update", description="Post the current homebrew voting report")
async def homebrew_voting_update(interaction: discord.Interaction) -> None:
    if not await begin_manual_homebrew_post(interaction):
        return
    try:
        posted = await send_vote_report(await bot.homebrew_channel(), datetime.now(CENTRAL))
        message = "Posted the homebrew voting update." if posted else "There are no open or newly resolved proposals to post."
        await interaction.followup.send(message, ephemeral=True)
    except (GitHubError, discord.DiscordException, RuntimeError) as exc:
        log.error("Manual homebrew voting update failed: %s", exc)
        await interaction.followup.send("I could not post the homebrew voting update.", ephemeral=True)


@bot.tree.command(name="homebrew_pages_update", description="Post today's newly published website pages")
async def homebrew_pages_update(interaction: discord.Interaction) -> None:
    if not await begin_manual_homebrew_post(interaction):
        return
    try:
        posted = await send_pages_report(await bot.homebrew_channel(), datetime.now(CENTRAL))
        message = "Posted today's new-page update." if posted else "There are no new pages to post today."
        await interaction.followup.send(message, ephemeral=True)
    except (GitHubError, discord.DiscordException, RuntimeError) as exc:
        log.error("Manual new-page update failed: %s", exc)
        await interaction.followup.send("I could not post the new-page update.", ephemeral=True)


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
