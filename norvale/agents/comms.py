"""Mail (SMTP/IMAP), calendar (CalDAV) and chat (Matrix) for ONE identity. Adapted from codex T9.1 run.py Employee.

The servers decide what this identity may see. Times on messages come from the shared fake clock.
"""
import base64
import email
import imaplib
import json
import smtplib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import format_datetime, make_msgid


class Comms:
    def __init__(self, ident, secret, endpoints):
        self.ident, self.sec, self.eps = ident, secret, endpoints
        self.addr = ident["mail"]
        self.user = ident["chat"]

    # ---------------- mail ----------------
    def send_mail(self, to, subject, text, when, cc=None, in_reply_to=None, calendar=None, method="REQUEST",
                  *, message_id=None, on_status=None):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = self.addr, ", ".join(to), subject
        if cc:
            m["Cc"] = ", ".join(cc)
        m["Date"], m["Message-ID"] = format_datetime(when), message_id or make_msgid(domain=self.addr.split("@")[1])
        if in_reply_to:
            m["In-Reply-To"] = m["References"] = in_reply_to
        m.set_content(text)
        if calendar:
            m.add_attachment(calendar.encode(), maintype="text", subtype="calendar", filename="invite.ics",
                             params={"method": method})
        with smtplib.SMTP(self.eps["mail"]["host"], self.eps["mail"]["smtp"], timeout=30) as s:
            s.login(self.addr, self.sec["password"])
            if on_status:
                on_status("uncertain")
            try:
                s.send_message(m)
            except (smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused,
                    smtplib.SMTPDataError, smtplib.SMTPNotSupportedError) as exc:
                if on_status and (not isinstance(exc, smtplib.SMTPDataError) or 400 <= exc.smtp_code < 600):
                    on_status("rejected")
                raise
            if on_status:
                on_status("accepted")
        return m["Message-ID"]

    def _imap(self):
        box = imaplib.IMAP4(self.eps["mail"]["host"], self.eps["mail"]["imap"])
        box.login(self.addr, self.sec["password"])
        return box

    @staticmethod
    def _checked_imap(result):
        status, data = result
        if status != "OK":
            raise imaplib.IMAP4.error(f"Mail readback failed ({status})")
        return data

    def inbox(self, only_unseen=False, limit=15):
        with self._imap() as box:
            self._checked_imap(box.select("INBOX", readonly=True))
            ids = self._checked_imap(box.uid("search", None, "UNSEEN" if only_unseen else "ALL"))
            out = []
            for uid in ids[0].split()[-limit:]:
                data = self._checked_imap(box.uid(
                    "fetch", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])"))
                hdr = email.message_from_bytes(data[0][1], policy=default_policy)
                out.append({"uid": uid.decode(), "from": str(hdr["From"]), "to": str(hdr["To"]), "subject": str(hdr["Subject"]),
                            "date": str(hdr["Date"]), "seen": b"\\Seen" in data[0][0], "message_id": str(hdr["Message-ID"])})
            return out

    def read_mail(self, uid, *, body_limit=4000):
        with self._imap() as box:
            self._checked_imap(box.select("INBOX"))
            data = self._checked_imap(box.uid("fetch", str(uid), "(RFC822)"))  # marks it \\Seen, like opening it
            if not data or not data[0]:
                raise ValueError(f"no message with uid {uid}")
            msg = email.message_from_bytes(data[0][1], policy=default_policy)
        body, attachments = "", []
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                body += part.get_content()
            elif part.get_content_type() == "text/calendar":
                attachments.append({"type": "calendar invitation", "ics": part.get_content()[:1500]})
            elif part.get_filename():
                attachments.append({"type": part.get_content_type(), "name": part.get_filename()})
        return {"uid": str(uid), "from": str(msg["From"]), "to": str(msg["To"]), "cc": str(msg["Cc"] or ""),
                "subject": str(msg["Subject"]), "date": str(msg["Date"]), "message_id": str(msg["Message-ID"]),
                "body": body[:body_limit], "attachments": attachments, "content_type": msg.get_content_type()}

    def raw_mailbox(self):
        """Controller export: every message in the inbox as raw RFC 822 bytes (not an agent tool)."""
        with self._imap() as box:
            box.select("INBOX", readonly=True)
            _, ids = box.uid("search", None, "ALL")
            return [(u.decode(), box.uid("fetch", u, "(BODY.PEEK[])")[1][0][1]) for u in ids[0].split()]

    # ---------------- calendar ----------------
    def _cal(self, method, path, body=None, headers=None):
        auth = "Basic " + base64.b64encode(f"{self.user}:{self.sec['password']}".encode()).decode()
        req = urllib.request.Request(self.eps["calendar"] + path, data=body.encode() if body else None, method=method,
                                     headers={"Authorization": auth, **(headers or {})})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode(errors="replace")

    def ensure_calendar(self):
        try:
            self._cal("MKCALENDAR", f"/{self.user}/calendar/")
        except urllib.error.HTTPError as e:
            if e.code not in (405, 409):
                raise

    def put_event(self, uid, ics):
        return self._cal("PUT", f"/{self.user}/calendar/{uid}.ics", ics, {"Content-Type": "text/calendar"})[0]

    def calendar_ics(self):
        try:
            return self._cal("GET", f"/{self.user}/calendar/")[1]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ""
            raise

    # ---------------- chat ----------------
    def _mx(self, method, path, body=None):
        req = urllib.request.Request(self.eps["chat"] + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method, headers={"Authorization": "Bearer " + self.sec["chat_token"],
                                                             "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    def mxid(self, handle):
        return f"@{handle}:norvale.test"

    def create_room(self, name, invite_handles, alias=None):
        body = {"name": name, "preset": "private_chat", "invite": [self.mxid(h) for h in invite_handles]}
        if alias:
            body["room_alias_name"] = alias
        return self._mx("POST", "/_matrix/client/v3/createRoom", body)["room_id"]

    def join(self, room_id):
        return self._mx("POST", f"/_matrix/client/v3/join/{urllib.parse.quote(room_id)}", {})

    def send_chat(self, room_id, text):
        return self._mx("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/send/m.room.message/{uuid.uuid4().hex}",
                        {"msgtype": "m.text", "body": text})["event_id"]

    def sync(self, since=None):
        q = "?timeout=0" + (f"&since={urllib.parse.quote(since)}" if since else "")
        return self._mx("GET", "/_matrix/client/v3/sync" + q)

    def room_messages(self, room_id, limit=30):
        r = self._mx("GET", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/messages?dir=b&limit={limit}")
        return [{"sender": e["sender"], "time_ms": e["origin_server_ts"], "text": e["content"].get("body", "")}
                for e in reversed(r.get("chunk", [])) if e.get("type") == "m.room.message"]
