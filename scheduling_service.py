from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode, urljoin

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("nazzurath.scheduling")

MAX_NOTIFICATION_ATTEMPTS = 6
CLAIM_TIMEOUT_MINUTES = 5


@dataclass(frozen=True)
class GuildMemberRecord:
    discord_id: str
    username: str
    display_name: str
    avatar_url: str | None
    role_ids: tuple[str, ...]


@dataclass(frozen=True)
class SchedulingNotification:
    id: str
    recipient_discord_id: str
    event_type: str
    payload: dict[str, Any]
    attempt_count: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "SchedulingNotification":
        payload = row.get("payload")
        return cls(
            id=str(row["id"]),
            recipient_discord_id=str(row["recipient_discord_id"]),
            event_type=str(row["event_type"]),
            payload=dict(payload) if isinstance(payload, Mapping) else {},
            attempt_count=int(row.get("attempt_count") or 0),
        )


@dataclass(frozen=True)
class RenderedNotification:
    title: str
    description: str
    url: str
    response_kind: str | None = None


@dataclass(frozen=True)
class ResponseOutcome:
    ok: bool
    message: str
    status: str | None = None


def retry_delay_seconds(attempt_count: int) -> int:
    """Return a bounded exponential delay after a failed delivery attempt."""
    attempt = max(1, int(attempt_count))
    return min(15 * (2 ** (attempt - 1)), 15 * 60)


def _clean(value: Any, fallback: str) -> str:
    text = " ".join(str(value or "").split())
    return text or fallback


def _value(payload: Mapping[str, Any], *keys: str) -> Any:
    return next((payload[key] for key in keys if payload.get(key) is not None), None)


def _discord_timestamp(value: Any, style: str = "F") -> str | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return f"<t:{int(parsed.timestamp())}:{style}>"


def _schedule_url(website_url: str, payload: Mapping[str, Any]) -> str:
    base = f"{website_url.rstrip('/')}/schedule"
    params: dict[str, str] = {}
    group_id = _value(payload, "group_id", "groupId")
    session_id = _value(payload, "session_id", "sessionId")
    if group_id:
        params["group"] = str(group_id)
    if session_id:
        params["session"] = str(session_id)
    return f"{base}?{urlencode(params)}" if params else base


def _homebrew_url(website_url: str, payload: Mapping[str, Any], settled: bool = False) -> str:
    published_url = str(payload.get("published_url") or "").strip()
    if published_url:
        return urljoin(f"{website_url.rstrip('/')}/", published_url)
    base = f"{website_url.rstrip('/')}/homebrew-registry"
    if settled:
        base += "/settled"
    post_id = str(_value(payload, "post_id", "postId") or "").strip()
    return f"{base}#homebrew-post-{post_id}" if post_id else base


def _session_description(payload: Mapping[str, Any], group_name: str, when: str, prefix: str) -> str:
    session_title = _clean(payload.get("title"), "Session")
    location = str(_value(payload, "meeting_location", "meetingLocation") or "").strip()
    description = f"{prefix} **{session_title}** for **{group_name}** {when}."
    if location:
        description += f"\n**Where:** {location}"
    return description


def render_notification(
    notification: SchedulingNotification,
    website_url: str,
) -> RenderedNotification:
    """Build Discord-safe copy from the durable website notification payload."""
    payload = notification.payload
    event_type = notification.event_type
    if event_type == "homebrew_submitted":
        title = _clean(payload.get("title"), "Your homebrew submission")
        return RenderedNotification(
            title="Homebrew submission received",
            description=(
                f"**{title}** was submitted for community review. "
                "I’ll send you another PM when an administrator approves or denies it."
            ),
            url=_homebrew_url(website_url, payload),
        )
    if event_type == "homebrew_decision":
        title = _clean(payload.get("title"), "Your homebrew submission")
        approved = payload.get("decision") == "approved"
        decision = "approved" if approved else "denied"
        decision_by = _clean(payload.get("decision_by"), "an administrator")
        return RenderedNotification(
            title=f"Homebrew {decision}",
            description=f"**{title}** was **{decision}** by **{decision_by}**.",
            url=_homebrew_url(website_url, payload, settled=not approved),
        )
    group_name = _clean(_value(payload, "group_name", "groupName"), "your group")
    actor_name = _clean(
        _value(
            payload,
            "actor_name",
            "actorName",
            "inviter_name",
            "inviterName",
            "member_name",
            "memberName",
        ),
        "A group member",
    )
    url = _schedule_url(website_url, payload)

    start_value = _value(payload, "starts_at", "start_at", "start_time", "startsAt", "startTime")
    end_value = _value(payload, "ends_at", "end_at", "end_time", "endsAt", "endTime")
    starts_at = _discord_timestamp(start_value, "F")
    starts_relative = _discord_timestamp(start_value, "R")
    ends_at = _discord_timestamp(end_value, "t")
    if starts_at:
        when = starts_at
        if ends_at:
            when += f"–{ends_at}"
        if starts_relative:
            when += f" ({starts_relative})"
    else:
        when = _clean(payload.get("when"), "Open the schedule for the current time.")

    if event_type == "group_invitation":
        return RenderedNotification(
            title="Scheduling group invitation",
            description=f"**{actor_name}** invited you to join **{group_name}**.",
            url=url,
            response_kind="invitation",
        )
    if event_type == "invitation_response":
        response = _clean(payload.get("response"), "responded")
        return RenderedNotification(
            title=f"Group invitation {response}",
            description=f"**{actor_name}** {response} the invitation to **{group_name}**.",
            url=url,
        )
    if event_type in {"session_confirmed", "session_proposal"}:
        return RenderedNotification(
            title="New session scheduled",
            description=_session_description(payload, group_name, when, "A new session was scheduled:"),
            url=url,
            response_kind="session_manager" if payload.get("can_manage") is True else "session",
        )
    if event_type == "session_rescheduled":
        return RenderedNotification(
            title="Session rescheduled",
            description=_session_description(payload, group_name, when, "The updated session is:") + " Please respond again.",
            url=url,
            response_kind="session_manager" if payload.get("can_manage") is True else "session",
        )
    if event_type == "session_cancelled":
        return RenderedNotification(
            title="Session cancelled",
            description=f"The **{group_name}** session that was scheduled for {when} has been cancelled.",
            url=url,
        )
    if event_type == "session_rsvp_response":
        response = _clean(payload.get("response"), "responded")
        return RenderedNotification(
            title=f"Session response: {response}",
            description=f"**{actor_name}** marked **{response}** for the **{group_name}** session on {when}.",
            url=url,
        )
    if event_type in {"session_reminder_24h", "session_reminder_1h", "session_reminder"}:
        lead = "24-hour" if event_type.endswith("24h") else "1-hour" if event_type.endswith("1h") else "Session"
        return RenderedNotification(
            title=f"{lead} reminder",
            description=f"Your **{group_name}** session starts {when}.",
            url=url,
        )
    if event_type == "delivery_warning":
        member_name = _clean(_value(payload, "member_name", "memberName"), "A group member")
        return RenderedNotification(
            title="Discord delivery failed",
            description=(
                f"I could not privately notify **{member_name}** about **{group_name}**. "
                "The notification is still available on the website."
            ),
            url=url,
        )

    if event_type == "date_poll_opened":
        poll_id = _clean(payload.get("poll_id"), "")
        poll_title = _clean(payload.get("title"), "Next session")
        return RenderedNotification(
            title=f"Choose a date: {poll_title}",
            description=_clean(payload.get("message"), f"Vote on the proposed dates for **{group_name}**."),
            url=f"{website_url.rstrip('/')}/schedule?tab=polls&poll={poll_id}",
        )

    return RenderedNotification(
        title=_clean(payload.get("title"), "Tazzurath scheduling update"),
        description=_clean(payload.get("message") or payload.get("description"), "Open the schedule for details."),
        url=url,
    )


class SchedulingService:
    """Shared PostgreSQL service for Discord member sync and the notification outbox."""

    def __init__(
        self,
        database_url: str,
        website_url: str,
        pool: Any | None = None,
        privileged_role_ids: Sequence[str] = (),
    ) -> None:
        self.database_url = database_url.strip()
        self.website_url = website_url.rstrip("/") or "https://www.tazzurath.com"
        self.pool: Any | None = pool
        self.privileged_role_ids = tuple(str(role_id) for role_id in privileged_role_ids if role_id)
        self.database_healthy = False
        self.last_error: str | None = None
        self.last_notification_run_at: datetime | None = None
        self.last_member_sync_at: datetime | None = None

    @classmethod
    def from_env(cls) -> "SchedulingService":
        return cls(
            database_url=os.getenv("DATABASE_URL", ""),
            website_url=os.getenv("WEBSITE_URL", "https://www.tazzurath.com"),
            privileged_role_ids=(
                os.getenv("DISCORD_DM_ROLE_ID", ""),
                os.getenv("ADMIN_ROLE_ID", ""),
            ),
        )

    @property
    def configured(self) -> bool:
        return bool(self.database_url or self.pool is not None)

    async def start(self) -> bool:
        if not self.configured:
            log.info("Scheduling integration is disabled: DATABASE_URL is not set")
            return False
        if self.pool is None:
            self.pool = AsyncConnectionPool(
                conninfo=self.database_url,
                min_size=1,
                max_size=5,
                open=False,
                timeout=10,
                # Supabase's transaction pooler must not receive named prepared
                # statements that outlive the borrowed backend connection.
                kwargs={"row_factory": dict_row, "prepare_threshold": None},
            )
        try:
            if getattr(self.pool, "closed", True):
                await self.pool.open(wait=True)
            async with self.pool.connection() as conn:
                await conn.execute("SELECT 1")
            self.database_healthy = True
            self.last_error = None
            backfilled = await self._backfill_manager_session_notifications()
            log.info("Scheduling database connection is ready")
            if backfilled:
                log.info("Queued %s missing scheduling-manager receipt(s)", backfilled)
            return True
        except Exception as exc:
            self.database_healthy = False
            self.last_error = type(exc).__name__
            log.error("Scheduling database connection failed: %s", type(exc).__name__)
            return False

    async def ensure_connected(self) -> bool:
        if not self.configured:
            return False
        if self.pool is None or not self.database_healthy:
            return await self.start()
        return True

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
        self.database_healthy = False

    async def _backfill_manager_session_notifications(self) -> int:
        """Queue the missing DM receipt for future sessions created before manager DMs existed."""
        assert self.pool is not None
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO notifications (
                    recipient_discord_id, event_type, related_entity_type,
                    related_entity_id, payload, discord_delivery_status,
                    due_at, attempt_count, idempotency_key
                )
                SELECT session_row.created_by_discord_id, 'session_confirmed', 'session',
                       session_row.id,
                       jsonb_build_object(
                           'group_id', group_row.id::text,
                           'group_name', group_row.name,
                           'session_id', session_row.id::text,
                           'starts_at', session_row.starts_at,
                           'ends_at', session_row.ends_at,
                           'timezone', session_row.timezone,
                           'revision', session_row.revision,
                           'can_manage', TRUE,
                           'manager_discord_id', session_row.created_by_discord_id
                       ),
                       'pending', NOW(), 0,
                       'session-manager-receipt:' || session_row.id::text || ':' ||
                           session_row.revision::text || ':' || session_row.created_by_discord_id
                FROM sessions AS session_row
                JOIN scheduling_groups AS group_row ON group_row.id = session_row.group_id
                WHERE session_row.status = 'confirmed'
                  AND session_row.starts_at >= NOW()
                  AND session_row.created_by_discord_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM notifications AS existing
                      WHERE existing.related_entity_type = 'session'
                        AND existing.related_entity_id = session_row.id
                        AND existing.recipient_discord_id = session_row.created_by_discord_id
                        AND existing.event_type IN ('session_confirmed', 'session_rescheduled')
                        AND existing.payload->>'revision' = session_row.revision::text
                  )
                ON CONFLICT (idempotency_key) DO NOTHING
                """
            )
        return max(0, cursor.rowcount)

    async def list_managed_sessions(self, actor_discord_id: str, limit: int = 25) -> list[dict[str, Any]]:
        """List future dates the Discord member is allowed to manage."""
        if not await self.ensure_connected():
            return []
        assert self.pool is not None
        try:
            async with self.pool.connection() as conn:
                cursor = await conn.execute(
                    """
                    SELECT session_row.id, session_row.group_id, group_row.name AS group_name,
                           session_row.starts_at, session_row.ends_at, session_row.timezone,
                           session_row.revision
                    FROM sessions AS session_row
                    JOIN scheduling_groups AS group_row ON group_row.id = session_row.group_id
                    WHERE session_row.status = 'confirmed'
                      AND session_row.starts_at >= NOW()
                      AND (
                          group_row.owner_discord_id = %s
                          OR EXISTS (
                              SELECT 1 FROM group_memberships AS manager
                              WHERE manager.group_id = group_row.id
                                AND manager.discord_id = %s
                                AND manager.status = 'accepted'
                                AND manager.role = 'co_dm'
                          )
                          OR EXISTS (
                              SELECT 1 FROM discord_members AS manager_member
                              WHERE manager_member.discord_id = %s
                                AND manager_member.role_ids ?| %s::text[]
                          )
                      )
                    ORDER BY session_row.starts_at
                    LIMIT %s
                    """,
                    (
                        actor_discord_id,
                        actor_discord_id,
                        actor_discord_id,
                        list(self.privileged_role_ids),
                        max(1, min(int(limit), 25)),
                    ),
                )
                rows = await cursor.fetchall()
            self._record_success()
            return [dict(row) for row in rows]
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def cancel_managed_session(
        self,
        actor_discord_id: str,
        session_id: str,
        expected_revision: int | None = None,
    ) -> ResponseOutcome:
        """Cancel a future session from Discord and notify the remaining roster."""
        if not await self.ensure_connected():
            return ResponseOutcome(False, "Scheduling is temporarily unavailable. Please use the website.")
        assert self.pool is not None
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    outcome = await self._cancel_managed_session(
                        conn,
                        actor_discord_id,
                        session_id,
                        expected_revision,
                    )
            self._record_success()
            return outcome
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def _cancel_managed_session(
        self,
        conn: Any,
        actor_discord_id: str,
        session_id: str,
        expected_revision: int | None = None,
    ) -> ResponseOutcome:
        cursor = await conn.execute(
            """
            SELECT session_row.*, group_row.name AS group_name,
                   group_row.owner_discord_id
            FROM sessions AS session_row
            JOIN scheduling_groups AS group_row ON group_row.id = session_row.group_id
            WHERE session_row.id = %s
              AND (
                  group_row.owner_discord_id = %s
                  OR EXISTS (
                      SELECT 1 FROM group_memberships AS manager
                      WHERE manager.group_id = group_row.id
                        AND manager.discord_id = %s
                        AND manager.status = 'accepted'
                        AND manager.role = 'co_dm'
                  )
                  OR EXISTS (
                      SELECT 1 FROM discord_members AS manager_member
                      WHERE manager_member.discord_id = %s
                        AND manager_member.role_ids ?| %s::text[]
                  )
              )
            FOR UPDATE OF session_row
            """,
            (
                session_id,
                actor_discord_id,
                actor_discord_id,
                actor_discord_id,
                list(self.privileged_role_ids),
            ),
        )
        session = await cursor.fetchone()
        if session is None:
            return ResponseOutcome(False, "That session does not exist or you are not allowed to cancel it.")
        if str(session["status"]) == "cancelled":
            return ResponseOutcome(True, "That session is already cancelled.", "cancelled")
        if expected_revision is not None and int(session["revision"]) != int(expected_revision):
            return ResponseOutcome(False, "That date was replaced by a newer one. Open the latest scheduling message.")

        await conn.execute(
            "UPDATE sessions SET status = 'cancelled', updated_at = NOW() WHERE id = %s",
            (session_id,),
        )
        await conn.execute(
            """
            UPDATE notifications
            SET discord_delivery_status = 'cancelled', updated_at = NOW()
            WHERE related_entity_type = 'session' AND related_entity_id = %s
              AND discord_delivery_status = 'pending'
            """,
            (session_id,),
        )
        attendees_cursor = await conn.execute(
            """
            SELECT attendee.discord_id, member.display_name
            FROM session_attendees AS attendee
            JOIN discord_members AS member ON member.discord_id = attendee.discord_id
            WHERE attendee.session_id = %s
            """,
            (session_id,),
        )
        attendees = await attendees_cursor.fetchall()
        payload_base = {
            "group_id": str(session["group_id"]),
            "group_name": str(session["group_name"]),
            "session_id": str(session_id),
            "starts_at": session["starts_at"].isoformat(),
            "ends_at": session["ends_at"].isoformat(),
            "timezone": str(session["timezone"]),
            "revision": int(session["revision"]),
        }
        for attendee in attendees:
            recipient_id = str(attendee["discord_id"])
            if recipient_id == actor_discord_id:
                continue
            await conn.execute(
                """
                INSERT INTO notifications (
                    recipient_discord_id, event_type, related_entity_type,
                    related_entity_id, payload, discord_delivery_status,
                    due_at, attempt_count, idempotency_key
                )
                VALUES (%s, 'session_cancelled', 'session', %s, %s, 'pending', NOW(), 0, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    recipient_id,
                    session_id,
                    Jsonb({**payload_base, "recipient_name": attendee["display_name"]}),
                    f"session-cancelled:{session_id}:{recipient_id}",
                ),
            )
        await conn.execute(
            """
            INSERT INTO scheduling_audit_log (
                actor_discord_id, action, entity_type, entity_id, details
            ) VALUES (%s, 'session.cancelled.discord', 'session', %s, %s)
            """,
            (actor_discord_id, session_id, Jsonb({"source": "discord"})),
        )
        return ResponseOutcome(
            True,
            f"The **{session['group_name']}** session has been cancelled. Everyone on the roster will be notified.",
            "cancelled",
        )

    def _record_success(self) -> None:
        self.database_healthy = True
        self.last_error = None

    def _record_failure(self, exc: Exception) -> None:
        self.database_healthy = False
        self.last_error = type(exc).__name__

    async def sync_guild_members(self, members: Iterable[GuildMemberRecord]) -> int:
        if not await self.ensure_connected():
            return 0
        records = list(members)
        ids = [member.discord_id for member in records]
        assert self.pool is not None
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    if records:
                        async with conn.cursor() as cursor:
                            await cursor.executemany(
                                """
                                INSERT INTO discord_members (
                                    discord_id, username, display_name, avatar_url, role_ids,
                                    active, last_synced_at, updated_at
                                )
                                VALUES (%s, %s, %s, %s, %s, TRUE, NOW(), NOW())
                                ON CONFLICT (discord_id) DO UPDATE SET
                                    username = EXCLUDED.username,
                                    display_name = EXCLUDED.display_name,
                                    avatar_url = EXCLUDED.avatar_url,
                                    role_ids = EXCLUDED.role_ids,
                                    active = TRUE,
                                    last_synced_at = NOW(),
                                    updated_at = NOW()
                                """,
                                [
                                    (
                                        member.discord_id,
                                        member.username,
                                        member.display_name,
                                        member.avatar_url,
                                        Jsonb(list(member.role_ids)),
                                    )
                                    for member in records
                                ],
                            )
                        await conn.execute(
                            """
                            UPDATE discord_members
                            SET active = FALSE, last_synced_at = NOW(), updated_at = NOW()
                            WHERE active = TRUE AND NOT (discord_id = ANY(%s))
                            """,
                            (ids,),
                        )
                    else:
                        await conn.execute(
                            """
                            UPDATE discord_members
                            SET active = FALSE, last_synced_at = NOW(), updated_at = NOW()
                            WHERE active = TRUE
                            """
                        )
            self.last_member_sync_at = datetime.now(timezone.utc)
            self._record_success()
            return len(records)
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def list_ping_website_roles(self, query: str = "", limit: int = 25) -> list[dict[str, Any]]:
        if not await self.ensure_connected():
            return []
        assert self.pool is not None
        clean_query = str(query or "").strip()
        try:
            async with self.pool.connection() as conn:
                cursor = await conn.execute(
                    """
                    SELECT id::text, slug, name, color, role_type
                    FROM website_roles
                    WHERE active = TRUE AND ping_enabled = TRUE
                      AND (%s = '' OR name ILIKE '%%' || %s || '%%' OR slug ILIKE '%%' || %s || '%%')
                    ORDER BY name
                    LIMIT %s
                    """,
                    (clean_query, clean_query, clean_query, max(1, min(int(limit), 25))),
                )
                rows = await cursor.fetchall()
            self._record_success()
            return list(rows)
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def resolve_website_role_ping(self, role_id: str) -> tuple[dict[str, Any], list[str]] | None:
        if not await self.ensure_connected():
            return None
        assert self.pool is not None
        try:
            async with self.pool.connection() as conn:
                role_cursor = await conn.execute(
                    """
                    SELECT id::text, slug, name, color, role_type
                    FROM website_roles
                    WHERE id = %s::uuid AND active = TRUE AND ping_enabled = TRUE
                    """,
                    (role_id,),
                )
                role = await role_cursor.fetchone()
                if role is None:
                    return None
                member_cursor = await conn.execute(
                    """
                    SELECT DISTINCT membership.discord_id
                    FROM website_role_memberships AS membership
                    JOIN discord_members AS member ON member.discord_id = membership.discord_id
                    WHERE membership.role_id = %s::uuid
                      AND membership.status = 'approved'
                      AND member.active = TRUE
                      AND (
                        %s <> 'dm_scope'
                        OR member.role_ids ?| %s::text[]
                      )
                    ORDER BY membership.discord_id
                    """,
                    (role_id, role["role_type"], list(self.privileged_role_ids)),
                )
                members = [str(row["discord_id"]) for row in await member_cursor.fetchall()]
            self._record_success()
            return role, members
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def audit_website_role_ping(
        self,
        *,
        actor_discord_id: str,
        role_id: str,
        channel_id: str,
        member_count: int,
        message: str,
    ) -> None:
        if not await self.ensure_connected():
            return
        assert self.pool is not None
        try:
            async with self.pool.connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO website_role_audit_log
                      (actor_discord_id, action, role_id, details)
                    VALUES (%s, 'role.pinged', %s::uuid, %s)
                    """,
                    (
                        actor_discord_id,
                        role_id,
                        Jsonb({
                            "channel_id": channel_id,
                            "member_count": member_count,
                            "message": message,
                        }),
                    ),
                )
            self._record_success()
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def claim_due_notifications(self, limit: int = 20) -> list[SchedulingNotification]:
        if not await self.ensure_connected():
            return []
        assert self.pool is not None
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    cursor = await conn.execute(
                        """
                        WITH claimable AS (
                            SELECT id
                            FROM notifications
                            WHERE discord_delivery_status = 'pending'
                              AND due_at <= NOW()
                              AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
                              AND (
                                event_type NOT IN ('session_reminder_24h', 'session_reminder_1h')
                                OR EXISTS (
                                  SELECT 1 FROM session_attendees attendee
                                  WHERE attendee.id = NULLIF(payload->>'attendee_id', '')::uuid
                                    AND attendee.status IN ('pending', 'accepted')
                                )
                              )
                            ORDER BY due_at, created_at
                            FOR UPDATE SKIP LOCKED
                            LIMIT %s
                        )
                        UPDATE notifications AS notification
                        SET discord_delivery_status = 'processing',
                            claimed_at = NOW(),
                            attempt_count = notification.attempt_count + 1,
                            updated_at = NOW()
                        FROM claimable
                        WHERE notification.id = claimable.id
                        RETURNING notification.*
                        """,
                        (max(1, min(int(limit), 100)),),
                    )
                    rows = await cursor.fetchall()
            self.last_notification_run_at = datetime.now(timezone.utc)
            self._record_success()
            return [SchedulingNotification.from_row(row) for row in rows]
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def release_stale_claims(self) -> int:
        if not await self.ensure_connected():
            return 0
        assert self.pool is not None
        try:
            async with self.pool.connection() as conn:
                cursor = await conn.execute(
                    """
                    UPDATE notifications
                    SET discord_delivery_status = 'pending', claimed_at = NULL, updated_at = NOW()
                    WHERE discord_delivery_status = 'processing'
                      AND claimed_at < NOW() - (%s * INTERVAL '1 minute')
                    """,
                    (CLAIM_TIMEOUT_MINUTES,),
                )
                count = cursor.rowcount
            self._record_success()
            return max(0, count)
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def mark_notification_sent(self, notification_id: str, discord_message_id: str) -> None:
        await self._update_delivery(
            """
            UPDATE notifications
            SET discord_delivery_status = 'sent', discord_message_id = %s,
                delivered_at = NOW(), claimed_at = NULL, last_error = NULL,
                updated_at = NOW()
            WHERE id = %s AND discord_delivery_status = 'processing'
            """,
            (discord_message_id, notification_id),
        )

    async def mark_notification_retry(self, notification_id: str, attempt_count: int, error: str) -> None:
        delay = retry_delay_seconds(attempt_count)
        await self._update_delivery(
            """
            UPDATE notifications
            SET discord_delivery_status = 'pending', claimed_at = NULL,
                next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                last_error = %s, updated_at = NOW()
            WHERE id = %s AND discord_delivery_status = 'processing'
            """,
            (delay, error[:500], notification_id),
        )

    async def _update_delivery(self, query: str, params: Sequence[Any]) -> None:
        if not await self.ensure_connected():
            return
        assert self.pool is not None
        try:
            async with self.pool.connection() as conn:
                await conn.execute(query, params)
            self._record_success()
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def mark_notification_failed(
        self,
        notification: SchedulingNotification,
        error: str,
    ) -> None:
        """Permanently fail delivery and add durable site warnings for group managers."""
        if not await self.ensure_connected():
            return
        assert self.pool is not None
        payload = notification.payload
        group_id = _value(payload, "group_id", "groupId")
        member_name = _clean(
            _value(payload, "recipient_name", "recipientName"),
            notification.recipient_discord_id,
        )
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE notifications
                        SET discord_delivery_status = 'failed', claimed_at = NULL,
                            failed_at = NOW(), last_error = %s, updated_at = NOW()
                        WHERE id = %s AND discord_delivery_status = 'processing'
                        """,
                        (error[:500], notification.id),
                    )
                    if group_id and notification.event_type != "delivery_warning":
                        await conn.execute(
                            """
                            INSERT INTO notifications (
                                recipient_discord_id, event_type, related_entity_type,
                                related_entity_id, payload, site_read_at,
                                discord_delivery_status, due_at, attempt_count,
                                idempotency_key, created_at, updated_at
                            )
                            SELECT managers.discord_id, 'delivery_warning', 'group', group_row.id,
                                   %s, NULL, 'pending', NOW(), 0,
                                   'delivery-warning:' || %s || ':' || managers.discord_id,
                                   NOW(), NOW()
                            FROM scheduling_groups AS group_row
                            CROSS JOIN LATERAL (
                                SELECT group_row.owner_discord_id AS discord_id
                                UNION
                                SELECT membership.discord_id
                                FROM group_memberships AS membership
                                WHERE membership.group_id = group_row.id
                                  AND membership.role = 'co_dm'
                                  AND membership.status = 'accepted'
                            ) AS managers
                            WHERE group_row.id = %s
                              AND managers.discord_id <> %s
                            ON CONFLICT (idempotency_key) DO NOTHING
                            """,
                            (
                                Jsonb(
                                    {
                                        "group_id": str(group_id),
                                        "group_name": payload.get("group_name"),
                                        "member_name": member_name,
                                        "failed_notification_id": notification.id,
                                    }
                                ),
                                notification.id,
                                group_id,
                                notification.recipient_discord_id,
                            ),
                        )
            self._record_success()
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def respond_to_message(
        self,
        discord_message_id: str,
        actor_discord_id: str,
        response_kind: str,
        response: str,
    ) -> ResponseOutcome:
        valid_response = (
            response_kind in {"invitation", "session"} and response in {"accepted", "declined"}
        ) or (response_kind == "session_manager" and response == "cancelled")
        if not valid_response:
            return ResponseOutcome(False, "That scheduling response is not valid.")
        if not await self.ensure_connected():
            return ResponseOutcome(False, "Scheduling is temporarily unavailable. Please use the website.")
        assert self.pool is not None
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    cursor = await conn.execute(
                        """
                        SELECT *
                        FROM notifications
                        WHERE discord_message_id = %s
                          AND recipient_discord_id = %s
                        FOR UPDATE
                        """,
                        (discord_message_id, actor_discord_id),
                    )
                    notification_row = await cursor.fetchone()
                    if notification_row is None:
                        return ResponseOutcome(False, "This button belongs to another member or is no longer active.")
                    event_type = str(notification_row["event_type"])
                    payload_value = notification_row.get("payload")
                    payload = dict(payload_value) if isinstance(payload_value, Mapping) else {}
                    if response_kind == "invitation":
                        if event_type != "group_invitation":
                            return ResponseOutcome(False, "This is not a group invitation.")
                        outcome = await self._respond_to_invitation(
                            conn, payload, actor_discord_id, response
                        )
                    elif response_kind == "session":
                        if event_type not in {"session_confirmed", "session_proposal", "session_rescheduled"}:
                            return ResponseOutcome(False, "This is not an active session proposal.")
                        outcome = await self._respond_to_session(
                            conn, payload, actor_discord_id, response
                        )
                    else:
                        if event_type not in {"session_confirmed", "session_rescheduled"}:
                            return ResponseOutcome(False, "This is not an active scheduled date.")
                        if payload.get("can_manage") is not True:
                            return ResponseOutcome(False, "This scheduling message does not have manager controls.")
                        manager_id = str(_value(payload, "manager_discord_id", "managerDiscordId") or "")
                        if manager_id and manager_id != actor_discord_id:
                            return ResponseOutcome(False, "This manager control belongs to another member.")
                        session_id = _value(payload, "session_id", "sessionId")
                        if not session_id:
                            return ResponseOutcome(False, "This message is missing its session reference.")
                        outcome = await self._cancel_managed_session(
                            conn,
                            actor_discord_id,
                            str(session_id),
                            int(payload["revision"]) if payload.get("revision") is not None else None,
                        )
            self._record_success()
            return outcome
        except Exception as exc:
            self._record_failure(exc)
            raise

    async def _respond_to_invitation(
        self,
        conn: Any,
        payload: Mapping[str, Any],
        actor_discord_id: str,
        response: str,
    ) -> ResponseOutcome:
        membership_id = _value(payload, "membership_id", "membershipId", "invitation_id", "invitationId")
        if not membership_id:
            return ResponseOutcome(False, "This invitation is missing its membership reference.")
        cursor = await conn.execute(
            """
            SELECT membership.*, group_row.name AS group_name, group_row.owner_discord_id,
                   group_row.status AS group_status,
                   member.display_name AS actor_name
            FROM group_memberships AS membership
            JOIN scheduling_groups AS group_row ON group_row.id = membership.group_id
            JOIN discord_members AS member ON member.discord_id = membership.discord_id
            WHERE membership.id = %s AND membership.discord_id = %s
            FOR UPDATE OF membership
            """,
            (membership_id, actor_discord_id),
        )
        membership = await cursor.fetchone()
        if membership is None:
            return ResponseOutcome(False, "This invitation belongs to another member or no longer exists.")
        if str(membership["group_status"]) != "active":
            return ResponseOutcome(False, "This scheduling group is archived.")
        current = str(membership["status"])
        if current == response:
            return ResponseOutcome(True, f"Your invitation is already marked **{response}**.", response)
        if current != "pending":
            return ResponseOutcome(False, f"This invitation is already **{current}**.", current)
        await conn.execute(
            """
            UPDATE group_memberships
            SET status = %s, responded_at = NOW(), updated_at = NOW()
            WHERE id = %s
            """,
            (response, membership_id),
        )
        await self._insert_manager_response_notifications(
            conn=conn,
            group_id=membership["group_id"],
            owner_discord_id=str(membership["owner_discord_id"]),
            actor_discord_id=actor_discord_id,
            event_type="invitation_response",
            entity_type="membership",
            entity_id=membership_id,
            response=response,
            group_name=str(membership["group_name"]),
            actor_name=str(membership["actor_name"]),
        )
        return ResponseOutcome(True, f"Your group invitation is now **{response}**.", response)

    async def _respond_to_session(
        self,
        conn: Any,
        payload: Mapping[str, Any],
        actor_discord_id: str,
        response: str,
    ) -> ResponseOutcome:
        attendee_id = _value(payload, "attendee_id", "attendeeId")
        session_id = _value(payload, "session_id", "sessionId")
        revision = payload.get("revision")
        if not attendee_id and not session_id:
            return ResponseOutcome(False, "This proposal is missing its attendance reference.")
        identity_clause = "attendee.id = %s" if attendee_id else "attendee.session_id = %s"
        identity_value = attendee_id or session_id
        cursor = await conn.execute(
            f"""
            SELECT attendee.*, session_row.group_id, session_row.revision,
                   session_row.starts_at, session_row.ends_at,
                   session_row.status AS session_status,
                   group_row.name AS group_name, group_row.owner_discord_id,
                   member.display_name AS actor_name
            FROM session_attendees AS attendee
            JOIN sessions AS session_row ON session_row.id = attendee.session_id
            JOIN scheduling_groups AS group_row ON group_row.id = session_row.group_id
            JOIN discord_members AS member ON member.discord_id = attendee.discord_id
            WHERE {identity_clause}
              AND attendee.discord_id = %s
            FOR UPDATE OF attendee
            """,
            (identity_value, actor_discord_id),
        )
        attendee = await cursor.fetchone()
        if attendee is None:
            return ResponseOutcome(False, "This session response belongs to another member or no longer exists.")
        if str(attendee["session_status"]) != "confirmed":
            return ResponseOutcome(False, "This session is no longer active.")
        if revision is not None and int(attendee["revision"]) != int(revision):
            return ResponseOutcome(False, "This proposal was replaced by a newer session time. Open the latest message.")
        current = str(attendee["status"])
        if current == response:
            return ResponseOutcome(True, f"Your attendance is already marked **{response}**.", response)
        if actor_discord_id == str(attendee["owner_discord_id"]):
            return ResponseOutcome(False, "The scheduling DM is automatically marked as attending.", current)
        await conn.execute(
            """
            UPDATE session_attendees
            SET status = %s, responded_at = NOW(), response_source = 'discord', updated_at = NOW()
            WHERE id = %s
            """,
            (response, attendee["id"]),
        )
        if response == "declined":
            await conn.execute(
                """
                UPDATE notifications
                SET discord_delivery_status = 'cancelled', updated_at = NOW()
                WHERE related_entity_type = 'session' AND related_entity_id = %s
                  AND recipient_discord_id = %s
                  AND event_type IN ('session_reminder_24h', 'session_reminder_1h')
                  AND (payload->>'attendee_id') = %s
                  AND discord_delivery_status = 'pending'
                """,
                (attendee["session_id"], actor_discord_id, str(attendee["id"])),
            )
        else:
            await conn.execute(
                """
                UPDATE notifications
                SET discord_delivery_status = 'pending', updated_at = NOW()
                WHERE related_entity_type = 'session' AND related_entity_id = %s
                  AND recipient_discord_id = %s
                  AND event_type IN ('session_reminder_24h', 'session_reminder_1h')
                  AND discord_delivery_status = 'cancelled' AND due_at > NOW()
                  AND (payload->>'revision')::integer = %s
                  AND (payload->>'attendee_id') = %s
                """,
                (
                    attendee["session_id"],
                    actor_discord_id,
                    int(attendee["revision"]),
                    str(attendee["id"]),
                ),
            )
        await self._insert_manager_response_notifications(
            conn=conn,
            group_id=attendee["group_id"],
            owner_discord_id=str(attendee["owner_discord_id"]),
            actor_discord_id=actor_discord_id,
            event_type="session_rsvp_response",
            entity_type="session",
            entity_id=attendee["session_id"],
            response=response,
            group_name=str(attendee["group_name"]),
            actor_name=str(attendee["actor_name"]),
            extra_payload={
                "session_id": str(attendee["session_id"]),
                "starts_at": attendee["starts_at"].isoformat(),
                "ends_at": attendee["ends_at"].isoformat(),
            },
        )
        return ResponseOutcome(True, f"Your attendance is now **{response}**.", response)

    async def _insert_manager_response_notifications(
        self,
        conn: Any,
        group_id: Any,
        owner_discord_id: str,
        actor_discord_id: str,
        event_type: str,
        entity_type: str,
        entity_id: Any,
        response: str,
        group_name: str,
        actor_name: str,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            "group_id": str(group_id),
            "group_name": group_name,
            "actor_discord_id": actor_discord_id,
            "actor_name": actor_name,
            "response": response,
            **dict(extra_payload or {}),
        }
        await conn.execute(
            """
            INSERT INTO notifications (
                recipient_discord_id, event_type, related_entity_type, related_entity_id,
                payload, discord_delivery_status, due_at, attempt_count,
                idempotency_key, created_at, updated_at
            )
            SELECT managers.discord_id, %s, %s, %s, %s, 'pending', NOW(), 0,
                   %s || ':' || managers.discord_id, NOW(), NOW()
            FROM (
                SELECT %s::text AS discord_id
                UNION
                SELECT membership.discord_id
                FROM group_memberships AS membership
                WHERE membership.group_id = %s
                  AND membership.role = 'co_dm'
                  AND membership.status = 'accepted'
            ) AS managers
            WHERE managers.discord_id <> %s
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                event_type,
                entity_type,
                entity_id,
                Jsonb(payload),
                f"{event_type}:{entity_id}:{actor_discord_id}:{response}",
                owner_discord_id,
                group_id,
                actor_discord_id,
            ),
        )
