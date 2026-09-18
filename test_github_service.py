import base64
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timezone

from github_service import (
    EVENT_TAG,
    POST_TAG,
    GitHubService,
    apply_comments,
    decode_tagged_body,
    page_from_path,
    proposals_resolved_since,
    proposal_from_issue,
)


def tagged(tag, metadata, content="", secret=""):
    payload = dict(metadata)
    if secret:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        payload["signature"] = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest()
        ).decode().rstrip("=")
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"<!-- {tag}:{encoded} -->\n\n{content}"


class GitHubServiceTests(unittest.TestCase):
    def test_decodes_website_tag(self):
        body = tagged(POST_TAG, {"authorName": "Ada"}, "A good item")
        metadata, content = decode_tagged_body(body, POST_TAG)
        self.assertEqual(metadata["authorName"], "Ada")
        self.assertEqual(content, "A good item")

    def test_rejects_bad_signature_when_secret_is_configured(self):
        issue = {
            "number": 7,
            "title": "Sword",
            "body": tagged(POST_TAG, {"authorName": "Ada"}, "Details", secret="wrong"),
            "html_url": "https://github.com/o/r/issues/7",
            "created_at": "2026-09-12T12:00:00Z",
        }
        self.assertIsNone(proposal_from_issue(issue, "right"))

    def test_applies_latest_weighted_votes_and_decision(self):
        issue = {
            "number": 7,
            "title": "Sword",
            "body": tagged(
                POST_TAG,
                {
                    "authorNickname": "Ada",
                    "destination": {"kind": "magic-items"},
                    "sourceUrl": "https://example.com/sword",
                },
                "Details",
            ),
            "html_url": "https://github.com/o/r/issues/7",
            "created_at": "2026-09-12T12:00:00Z",
        }
        proposal = proposal_from_issue(issue)
        self.assertEqual(proposal.source_url, "https://example.com/sword")
        comments = [
            {"body": tagged(EVENT_TAG, {"type": "vote", "authorKey": "a", "choice": "approve", "weight": 5})},
            {"body": tagged(EVENT_TAG, {"type": "call-vote"})},
            {"body": tagged(EVENT_TAG, {"type": "vote", "authorKey": "a", "choice": "disapprove", "weight": 5})},
            {"body": tagged(EVENT_TAG, {"type": "vote", "authorKey": "b", "choice": "approve", "weight": 1})},
        ]
        result = apply_comments(proposal, comments)
        self.assertEqual(result.status, "voting")
        self.assertEqual(result.approve, 1)
        self.assertEqual(result.disapprove, 5)

    def test_builds_encoded_site_page_link(self):
        page = page_from_path("content/Gods and Godhood/My Page.md", "https://www.tazzurath.com/")
        self.assertEqual(page.title, "My Page")
        self.assertEqual(page.url, "https://www.tazzurath.com/read/Gods%20and%20Godhood/My%20Page")

    def test_records_decision_time_and_filters_resolutions_since_last_report(self):
        issue = {
            "number": 8,
            "title": "Frost Wyrm",
            "body": tagged(POST_TAG, {"authorName": "Ada"}, "Details"),
            "html_url": "https://github.com/o/r/issues/8",
            "created_at": "2026-09-12T12:00:00Z",
        }
        proposal = proposal_from_issue(issue)
        resolved = apply_comments(proposal, [{
            "created_at": "2026-09-17T11:00:00Z",
            "body": tagged(EVENT_TAG, {"type": "decision", "decision": "approved"}),
        }])

        self.assertEqual(resolved.status, "approved")
        self.assertEqual(resolved.decided_at, datetime(2026, 9, 17, 11, tzinfo=timezone.utc))
        self.assertEqual(
            proposals_resolved_since(
                [resolved],
                datetime(2026, 9, 17, 10, tzinfo=timezone.utc),
            ),
            [resolved],
        )
        self.assertEqual(
            proposals_resolved_since(
                [resolved],
                datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
            ),
            [],
        )

    def test_ignores_non_content_files(self):
        self.assertIsNone(page_from_path("README.md", "https://example.com"))
        self.assertIsNone(page_from_path("content/image.png", "https://example.com"))


class GitHubServiceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_finds_added_pages_across_commit_details(self):
        service = GitHubService("token", "owner", "repo", "main", "https://example.com")

        async def fake_request(path, params=None):
            if path.endswith("/commits"):
                return [{"sha": "new"}, {"sha": "old"}]
            if path.endswith("/old"):
                return {"files": [{"status": "added", "filename": "content/lore/Old Page.md"}]}
            if path.endswith("/new"):
                return {"files": [
                    {"status": "modified", "filename": "content/lore/Old Page.md"},
                    {"status": "added", "filename": "content/lore/New Page.md"},
                ]}
            raise AssertionError(path)

        service.request = fake_request
        pages = await service.new_pages_between(
            datetime(2026, 9, 12, tzinfo=timezone.utc),
            datetime(2026, 9, 13, tzinfo=timezone.utc),
        )
        self.assertEqual([page.title for page in pages], ["New Page", "Old Page"])


if __name__ == "__main__":
    unittest.main()
