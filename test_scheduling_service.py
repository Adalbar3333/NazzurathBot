import unittest
from datetime import datetime, timezone

from scheduling_service import (
    GuildMemberRecord,
    SchedulingNotification,
    SchedulingService,
    render_notification,
    retry_delay_seconds,
)


class NotificationFormattingTests(unittest.TestCase):
    def test_retry_backoff_is_exponential_and_bounded(self):
        self.assertEqual(
            [retry_delay_seconds(attempt) for attempt in range(1, 9)],
            [15, 30, 60, 120, 240, 480, 900, 900],
        )

    def test_group_invitation_has_persistent_response_kind_and_group_link(self):
        notification = SchedulingNotification(
            id="notification-1",
            recipient_discord_id="123",
            event_type="group_invitation",
            payload={
                "group_id": "group-1",
                "group_name": "The Thursday Company",
                "inviter_name": "Dungeon Master",
            },
            attempt_count=1,
        )

        rendered = render_notification(notification, "https://www.tazzurath.com/")

        self.assertEqual(rendered.response_kind, "invitation")
        self.assertIn("Thursday Company", rendered.description)
        self.assertEqual(rendered.url, "https://www.tazzurath.com/schedule?group=group-1")

    def test_session_uses_discord_timestamp_tags(self):
        notification = SchedulingNotification(
            id="notification-2",
            recipient_discord_id="123",
            event_type="session_rescheduled",
            payload={
                "group_id": "group-1",
                "session_id": "session-1",
                "group_name": "Heroes",
                "starts_at": "2026-09-20T20:00:00Z",
                "ends_at": "2026-09-20T23:00:00Z",
            },
            attempt_count=1,
        )

        rendered = render_notification(notification, "https://example.com")

        start_epoch = int(datetime(2026, 9, 20, 20, tzinfo=timezone.utc).timestamp())
        end_epoch = int(datetime(2026, 9, 20, 23, tzinfo=timezone.utc).timestamp())
        self.assertIn(f"<t:{start_epoch}:F>", rendered.description)
        self.assertIn(f"<t:{start_epoch}:R>", rendered.description)
        self.assertIn(f"<t:{end_epoch}:t>", rendered.description)
        self.assertEqual(rendered.response_kind, "session")
        self.assertIn("group=group-1", rendered.url)
        self.assertIn("session=session-1", rendered.url)


class FakeCursor:
    def __init__(self, rows=None, rowcount=0):
        self.rows = list(rows or [])
        self.rowcount = rowcount

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, claim_rows=None):
        self.claim_rows = list(claim_rows or [])
        self.calls = []
        self.executemany_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def transaction(self):
        return self

    def cursor(self):
        return self

    async def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.calls.append((normalized, params))
        if "RETURNING notification.*" in normalized:
            return FakeCursor(self.claim_rows)
        return FakeCursor(rowcount=1)

    async def executemany(self, query, params):
        self.executemany_calls.append((" ".join(query.split()), list(params)))


class FakePool:
    def __init__(self, connection):
        self.connection_value = connection
        self.opened = False
        self.closed = True

    async def open(self, wait=True):
        self.opened = True
        self.closed = False

    def connection(self):
        return self.connection_value

    async def close(self):
        self.closed = True


class FakeInvitationConnection(FakeConnection):
    def __init__(self):
        super().__init__()
        self.membership_status = "pending"
        self.membership_updates = 0
        self.manager_notifications = 0

    async def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.calls.append((normalized, params))
        if "FROM notifications" in normalized and "discord_message_id" in normalized:
            if params == ("message-1", "42"):
                return FakeCursor(
                    [{
                        "event_type": "group_invitation",
                        "payload": {"membership_id": "membership-1"},
                    }]
                )
            return FakeCursor()
        if "FROM group_memberships AS membership" in normalized and "FOR UPDATE OF membership" in normalized:
            return FakeCursor(
                [{
                    "id": "membership-1",
                    "group_id": "group-1",
                    "discord_id": "42",
                    "status": self.membership_status,
                    "group_name": "Heroes",
                    "group_status": "active",
                    "owner_discord_id": "99",
                    "actor_name": "Ada",
                }]
            )
        if normalized.startswith("UPDATE group_memberships"):
            self.membership_status = params[0]
            self.membership_updates += 1
            return FakeCursor(rowcount=1)
        if normalized.startswith("INSERT INTO notifications"):
            self.manager_notifications += 1
            return FakeCursor(rowcount=1)
        return FakeCursor(rowcount=1)


class SchedulingServiceDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_claims_due_notifications_and_maps_database_rows(self):
        connection = FakeConnection(
            claim_rows=[
                {
                    "id": "notice-1",
                    "recipient_discord_id": "42",
                    "event_type": "session_reminder_1h",
                    "payload": {"group_name": "Heroes"},
                    "attempt_count": 2,
                }
            ]
        )
        service = SchedulingService("", "https://example.com", pool=FakePool(connection))

        notifications = await service.claim_due_notifications(limit=7)

        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].id, "notice-1")
        self.assertEqual(notifications[0].attempt_count, 2)
        claim_call = next(query for query, _ in connection.calls if "SKIP LOCKED" in query)
        self.assertIn("discord_delivery_status = 'processing'", claim_call)
        self.assertIn("attendee.status IN ('pending', 'accepted')", claim_call)
        self.assertTrue(service.database_healthy)

    async def test_sync_upserts_current_members_and_marks_departed_in_one_transaction(self):
        connection = FakeConnection()
        service = SchedulingService("", "https://example.com", pool=FakePool(connection))
        member = GuildMemberRecord(
            discord_id="42",
            username="ada",
            display_name="Ada",
            avatar_url="https://cdn.example/avatar.png",
            role_ids=("10", "11"),
        )

        count = await service.sync_guild_members([member])

        self.assertEqual(count, 1)
        self.assertEqual(len(connection.executemany_calls), 1)
        self.assertIn("ON CONFLICT (discord_id) DO UPDATE", connection.executemany_calls[0][0])
        departure_query = next(query for query, _ in connection.calls if "NOT (discord_id = ANY" in query)
        self.assertIn("active = FALSE", departure_query)
        self.assertIsNotNone(service.last_member_sync_at)

    async def test_button_response_verifies_recipient_and_is_idempotent(self):
        connection = FakeInvitationConnection()
        service = SchedulingService("", "https://example.com", pool=FakePool(connection))

        unauthorized = await service.respond_to_message(
            "message-1", "someone-else", "invitation", "accepted"
        )
        first = await service.respond_to_message(
            "message-1", "42", "invitation", "accepted"
        )
        repeated = await service.respond_to_message(
            "message-1", "42", "invitation", "accepted"
        )

        self.assertFalse(unauthorized.ok)
        self.assertTrue(first.ok)
        self.assertEqual(first.status, "accepted")
        self.assertTrue(repeated.ok)
        self.assertIn("already", repeated.message)
        self.assertEqual(connection.membership_updates, 1)
        self.assertEqual(connection.manager_notifications, 1)


if __name__ == "__main__":
    unittest.main()
