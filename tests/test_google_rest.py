import base64
import tempfile
import unittest
from pathlib import Path

import httpx

from app.google_rest import GoogleRestWorkspace
from app.memory import Memory


class RestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.tmp.name) / 'state.db')

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def workspace(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return GoogleRestWorkspace(lambda: 'test-token', client, self.memory)

    def test_first_poll_skips_old_mail(self):
        calls = []
        def handler(r):
            calls.append(r.url.path)
            return httpx.Response(200, json={'emailAddress': 'personal@gmail.com', 'historyId': '100'})
        w = self.workspace(handler)
        self.assertEqual(w.iter_inbox(), [])
        self.assertEqual(calls, ['/gmail/v1/users/me/profile'])
        self.assertEqual(self.memory.checkpoint('personal@gmail.com'), '100')

    def test_paginated_new_inbox_and_checkpoint_commit(self):
        self.memory.set_checkpoint('personal@gmail.com', '100')
        def handler(r):
            if r.url.path.endswith('/profile'):
                return httpx.Response(200, json={'emailAddress': 'personal@gmail.com', 'historyId': '120'})
            self.assertEqual(r.url.params['startHistoryId'], '100')
            if r.url.params.get('pageToken') == 'next':
                return httpx.Response(200, json={'historyId': '120'})
            return httpx.Response(200, json={'historyId': '110', 'nextPageToken': 'next', 'history': [{'messagesAdded': [
                {'message': {'id': 'new', 'labelIds': ['INBOX']}},
                {'message': {'id': 'sent', 'labelIds': ['SENT']}},
                {'message': {'id': 'draft', 'labelIds': ['INBOX', 'DRAFT']}},
            ]}]})
        w = self.workspace(handler)
        self.assertEqual(w.iter_inbox(), ['new'])
        self.assertEqual(self.memory.checkpoint('personal@gmail.com'), '100')
        w.commit_poll()
        self.assertEqual(self.memory.checkpoint('personal@gmail.com'), '120')

    def test_mime_body_and_attachment(self):
        body = base64.urlsafe_b64encode(b'Hello newsroom').decode()
        w = self.workspace(lambda r: httpx.Response(200, json={'id': 'one', 'payload': {'headers': [{'name': 'From', 'value': 'sender@example.com'}], 'parts': [
            {'mimeType': 'text/plain', 'body': {'data': body}},
            {'filename': 'copy.pdf', 'body': {'attachmentId': 'att'}},
        ]}}))
        message = w.get_message('one')
        self.assertEqual(message['plaintextBody'], 'Hello newsroom')
        self.assertEqual(message['attachments'][0]['id'], 'att')

    def test_expired_history_does_not_backfill(self):
        self.memory.set_checkpoint('a', '1')
        def handler(r):
            if r.url.path.endswith('/profile'):
                return httpx.Response(200, json={'emailAddress': 'a', 'historyId': '10'})
            return httpx.Response(404, json={})
        w = self.workspace(handler)
        with self.assertRaisesRegex(Exception, 'checkpoint expired'):
            w.iter_inbox()
        self.assertEqual(self.memory.checkpoint('a'), '1')

    def test_multiple_header_guests_reach_calendar(self):
        from app.workspace import email_from_gmail
        from app.specialists import event_arguments
        from app.models import TriageOutput
        posted = []
        def handler(r):
            if r.method == 'POST':
                import json
                posted.append(json.loads(r.content))
                self.assertEqual(r.url.params['sendUpdates'], 'all')
                return httpx.Response(200, json={'id': 'event'})
            if r.url.path.endswith('/profile'):
                return httpx.Response(200, json={'emailAddress': 'me@gmail.com', 'historyId': '100'})
            return httpx.Response(200, json={'id': 'new', 'payload': {'headers': [
                {'name': 'From', 'value': 'Sender <sender@example.com>'},
                {'name': 'To', 'value': 'Me <me@gmail.com>, "Doe, Jane" <jane@example.com>'},
                {'name': 'To', 'value': 'second@example.com'},
                {'name': 'Cc', 'value': 'SENDER@example.com, colleague@example.com, ME@gmail.com'},
                {'name': 'Bcc', 'value': 'hidden@example.com'},
            ]}})
        w = self.workspace(handler)
        w.iter_inbox()
        email = email_from_gmail(w.get_message('new'))
        decision = TriageOutput(route='calendar', confidence=.9, reason='meeting', messageId='new',
                                disposition='dispatch', engine='heuristic', fields={'start': '2026-11-07T10:00:00+08:00'})
        w.create_event(event_arguments(email, decision))
        self.assertEqual(posted[0]['attendees'], [
            {'email': 'sender@example.com', 'displayName': 'Sender'},
            {'email': 'jane@example.com', 'displayName': 'Doe, Jane'},
            {'email': 'second@example.com'}, {'email': 'colleague@example.com'},
        ])

    def test_missing_recipient_headers_preserves_sender_only(self):
        from app.models import InboundEmail
        from app.specialists import calendar_attendees
        email = InboundEmail(messageId='one', **{'from': 'sender@example.com'})
        self.assertEqual(calendar_attendees(email), [{'email': 'sender@example.com'}])

    def test_invalid_addresses_are_ignored(self):
        from app.models import InboundEmail
        from app.specialists import calendar_attendees
        email = InboundEmail(messageId='one', **{'from': 'invalid', 'to': ['bad@, valid@example.com'], 'cc': ['valid@example.com']})
        self.assertEqual(calendar_attendees(email), [{'email': 'valid@example.com'}])

    def test_standard_calendar_and_docs_requests(self):
        calls = []
        def handler(r):
            import json
            calls.append((r.method, str(r.url), json.loads(r.content)))
            return httpx.Response(200, json={'documentId': 'doc'} if r.url.path == '/v1/documents' else {'id': 'event'})
        w = self.workspace(handler)
        w.create_event({'summary': 'Briefing', 'startTime': '2026-11-07T10:00:00+08:00', 'endTime': '2026-11-07T11:00:00+08:00', 'timeZone': 'Asia/Singapore', 'attendees': [{'email': 'sender@example.com'}]})
        self.assertIn('sendUpdates=all', calls[0][1])
        self.assertEqual(calls[0][2]['attendees'], [{'email': 'sender@example.com'}])
        self.assertEqual(w.create_document('Title', 'Copy'), 'https://docs.google.com/document/d/doc/edit')
        self.assertEqual(calls[-1][2]['requests'][0]['insertText']['text'], 'Copy')


if __name__ == '__main__':
    unittest.main()
