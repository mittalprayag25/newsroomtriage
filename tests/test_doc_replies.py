import base64
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx

from app.google_rest import GoogleRestWorkspace
from app.job import send_pending_replies
from app.memory import Memory
from app.settings import Settings
from app.workspace import WorkspaceError


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.tmp.name) / 'state.db')
        self.link = 'https://docs.google.com/document/d/doc/edit'
        self.memory.record('source', 'article', 'dispatch', doc_link=self.link)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def test_article_dispatch_automatically_replies_in_normal_job(self):
        from unittest.mock import patch
        from app.job import run_once
        from app.models import TriageOutput
        from app.specialists import ActionResult
        self.memory.reply_state('source', 'sent', 'previous')
        class Desk:
            replies = []
            def iter_inbox(self):
                return ['incoming']
            def get_message(self, mid):
                return {'id': mid, 'sender': 'sender@example.com', 'subject': 'Press release', 'plaintextBody': 'Copy'}
            def send_doc_reply(self, message, link):
                self.replies.append((message['sender'], link))
                return {'id': 'sent-automatically'}
        desk = Desk()
        decision = TriageOutput(route='article', disposition='dispatch', confidence=.95,
                                messageId='incoming', reason='Press release', engine='heuristic', fields={})
        with patch('app.job.run_triage', return_value=decision), patch('app.job.handle_message', return_value=ActionResult('article', 'dispatch', doc_link=self.link)) as specialist:
            first = run_once(Settings(gmail_from=''), desk, self.memory, Path('house-style.md'))
            second = run_once(Settings(gmail_from=''), desk, self.memory, Path('house-style.md'))
        self.assertEqual(desk.replies, [('sender@example.com', self.link)])
        self.assertEqual(first['replies'], [{'messageId': 'incoming', 'sentMessageId': 'sent-automatically'}])
        self.assertEqual(first['failures'], [])
        self.assertEqual(second['skipped'], 1)
        self.assertEqual(specialist.call_count, 1)

    def test_reply_is_threaded_and_only_to_sender(self):
        captured = []
        def handler(r):
            import json
            captured.append(json.loads(r.content))
            return httpx.Response(200, json={'id': 'sent'})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            w = GoogleRestWorkspace(lambda: 'token', http, self.memory)
            w.send_doc_reply({'sender': 'Sender <sender@example.com>', 'subject': 'Press release',
                              'rfcMessageId': '<original@example.com>', 'threadId': 'thread',
                              'cc': ['other@example.com']}, self.link)
        payload = captured[0]
        mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(payload['raw']))
        self.assertEqual(payload['threadId'], 'thread')
        self.assertEqual(mime['To'], 'sender@example.com')
        self.assertIsNone(mime['Cc'])
        self.assertEqual(mime['In-Reply-To'], '<original@example.com>')
        self.assertIn(self.link, mime.get_content())

    def test_success_is_not_sent_again(self):
        class Desk:
            calls = 0
            def get_message(self, mid):
                return {'sender': 'sender@example.com'}
            def send_doc_reply(self, message, link):
                self.calls += 1
                return {'id': 'sent'}
        desk = Desk()
        settings = Settings(gmail_from='')
        report = {'failures': []}
        send_pending_replies(settings, desk, self.memory, report)
        self.memory.queue_doc_reply('source')
        send_pending_replies(settings, desk, self.memory, report)
        self.assertEqual(desk.calls, 1)
        self.assertEqual(report['failures'], [])
        self.assertEqual(self.memory.get('source')['doc_link'], self.link)

    def test_rejected_send_retries_but_timeout_is_held(self):
        class Desk:
            def get_message(self, mid):
                return {'sender': 'sender@example.com'}
            def send_doc_reply(self, message, link):
                r = httpx.Response(403, request=httpx.Request('POST', 'https://gmail.googleapis.com'))
                try:
                    r.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise WorkspaceError('Missing permission') from exc
        report = {'failures': []}
        send_pending_replies(Settings(gmail_from=''), Desk(), self.memory, report)
        self.assertEqual(len(self.memory.pending_replies()), 1)
        class TimeoutDesk(Desk):
            def send_doc_reply(self, message, link):
                raise httpx.ReadTimeout('Unknown send result')
        send_pending_replies(Settings(gmail_from=''), TimeoutDesk(), self.memory, report)
        self.assertEqual(self.memory.pending_replies(), [])
        self.assertEqual(len(report['failures']), 2)


if __name__ == '__main__':
    unittest.main()
