"""Standard Google APIs, including personal Gmail accounts."""
import base64
from html.parser import HTMLParser
from urllib.parse import quote

import httpx

from app.workspace import GMAIL_API, WorkspaceError


class GoogleRestWorkspace:
    def __init__(self, token_provider, http, memory, sender=''):
        self._token = token_provider
        self._http = http
        self.memory = memory
        self.sender = sender
        self._account = ""
        self._pending = None

    def request(self, method, url, **kwargs):
        try:
            response = self._http.request(method, url, headers={'Authorization': 'Bearer ' + self._token()}, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404 and url.endswith('/history'):
                raise WorkspaceError('Gmail history checkpoint expired. Monitoring is held; no old mail will be scanned. Reset the account checkpoint deliberately to start fresh.') from exc
            try:
                detail = exc.response.json().get('error', {}).get('message', '')
            except (ValueError, AttributeError):
                detail = ''
            raise WorkspaceError(f'Google API HTTP {exc.response.status_code}: {detail[:600]}') from exc
        return response.json()

    def iter_inbox(self):
        self._pending = None
        profile = self.request('GET', GMAIL_API + '/profile')
        account = profile['emailAddress']
        self._account = account
        cursor = self.memory.checkpoint(account)
        if cursor is None:
            self.memory.set_checkpoint(account, profile['historyId'])
            return []
        params = {'startHistoryId': cursor, 'historyTypes': 'messageAdded', 'maxResults': 500}
        ids = []
        while True:
            page = self.request('GET', GMAIL_API + '/history', params=params)
            for entry in page.get('history', []):
                for added in entry.get('messagesAdded', []):
                    message = added['message']
                    labels = set(message.get('labelIds', []))
                    if 'INBOX' in labels and not labels.intersection({'SENT', 'DRAFT'}):
                        ids.append(message['id'])
            if not page.get('nextPageToken'):
                self._pending = (account, page['historyId'])
                return list(dict.fromkeys(ids))
            params['pageToken'] = page['nextPageToken']

    def commit_poll(self):
        if self._pending:
            self.memory.set_checkpoint(*self._pending)
            self._pending = None

    def get_message(self, message_id):
        raw = self.request('GET', GMAIL_API + '/messages/' + quote(message_id, safe=''), params={'format': 'full'})
        payload = raw.get('payload', {})
        headers = {}
        for header in payload.get('headers', []):
            key = header['name'].lower()
            headers[key] = ', '.join(filter(None, [headers.get(key), header['value']]))
        plain, html, attachments = [], [], []

        def walk(part):
            if part.get('filename'):
                body = part.get('body', {})
                attachments.append({'filename': part['filename'], 'id': body.get('attachmentId', ''), 'data': body.get('data')})
                return
            data = part.get('body', {}).get('data')
            if data:
                charset = 'utf-8'
                from email.message import Message
                for header in part.get('headers', []):
                    if header['name'].lower() == 'content-type':
                        mime = Message(); mime['content-type'] = header['value']
                        charset = mime.get_content_charset() or 'utf-8'
                decoded = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))
                try:
                    text = decoded.decode(charset, errors='replace')
                except LookupError:
                    text = decoded.decode('utf-8', errors='replace')
                if part.get('mimeType') == 'text/plain':
                    plain.append(text)
                elif part.get('mimeType') == 'text/html':
                    html.append(text)
            for child in part.get('parts', []):
                walk(child)
        walk(payload)
        if not plain and html:
            class Text(HTMLParser):
                def handle_data(self, data):
                    plain.append(data)
            Text().feed('\n'.join(html))
        return {'id': raw['id'], 'sender': headers.get('from', ''), 'subject': headers.get('subject', ''),
                'to': [headers['to']] if headers.get('to') else [],
                'cc': [headers['cc']] if headers.get('cc') else [], 'mailboxAddress': self._account,
                'threadId': raw.get('threadId', ''), 'rfcMessageId': headers.get('message-id', ''),
                'references': headers.get('references', ''),
                'plaintextBody': '\n'.join(plain), 'snippet': raw.get('snippet', ''), 'attachments': attachments}

    def send_doc_reply(self, message, doc_link):
        from email.message import EmailMessage
        from email.utils import parseaddr
        import re
        _, recipient = parseaddr(message.get('sender', ''))
        if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', recipient):
            raise WorkspaceError('Cannot reply: the email sender has no valid address.')
        reply = EmailMessage()
        reply['To'] = recipient
        subject = message.get('subject', '')
        reply['Subject'] = subject if subject.lower().startswith('re:') else 'Re: ' + subject
        original_id = message.get('rfcMessageId', '')
        if original_id:
            reply['In-Reply-To'] = original_id
            reply['References'] = ' '.join(filter(None, [message.get('references', ''), original_id]))
        reply.set_content('The article draft is ready:\n\n' + doc_link + '\n')
        payload = {'raw': base64.urlsafe_b64encode(reply.as_bytes()).decode('ascii')}
        if message.get('threadId'):
            payload['threadId'] = message['threadId']
        return self.request('POST', GMAIL_API + '/messages/send', json=payload)

    def attachment_bytes(self, message_id, attachment_id):
        result = self.request('GET', GMAIL_API + '/messages/' + quote(message_id, safe='') + '/attachments/' + quote(attachment_id, safe=''))
        data = result['data']
        return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))

    def create_event(self, arguments):
        event = {key: arguments[key] for key in ('summary', 'description', 'location', 'attendees') if key in arguments}
        event['start'] = {'dateTime': arguments['startTime'], 'timeZone': arguments['timeZone']}
        event['end'] = {'dateTime': arguments['endTime'], 'timeZone': arguments['timeZone']}
        calendar_id = quote(arguments.get('calendarId', 'primary'), safe='')
        return self.request('POST', f'https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events',
                            params={'sendUpdates': 'all' if event.get('attendees') else 'none'}, json=event)

    def create_document(self, title, text):
        created = self.request('POST', 'https://docs.googleapis.com/v1/documents', json={'title': title[:200]})
        document_id = created['documentId']
        self.request('POST', f'https://docs.googleapis.com/v1/documents/{quote(document_id, safe="")}:batchUpdate',
                     json={'requests': [{'insertText': {'text': text, 'location': {'index': 1}}}]})
        return f'https://docs.google.com/document/d/{document_id}/edit'

    def close(self):
        pass
