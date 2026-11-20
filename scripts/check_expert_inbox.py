"""Offline protocol check: the public tools see evidence only after expert replies."""
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from norvale.agents.expert_inbox import ExpertInbox, EXPERTS


class Mail:
    def __init__(self, address, mailboxes):
        self.addr, self.mailboxes = address, mailboxes
        self.rows = []

    def send_mail(self, to, subject, body, when, in_reply_to=None):
        recipient = self.mailboxes[to[0]]
        uid = str(len(recipient.rows) + 1)
        recipient.rows.append({"from": self.addr, "to": to[0], "subject": subject, "body": body,
                               "date": format_datetime(when), "uid": uid,
                               "message_id": f"<{uid}@{to[0]}>", "in_reply_to": in_reply_to})

    def find_subject(self, subject):
        return [{**r, "subject": " " + r["subject"]} for r in self.rows if r["subject"] == subject]

    def read_mail(self, uid):
        return self.rows[int(uid)-1]


def check():
    mailboxes = {}
    clients = {}
    for role in (*EXPERTS, "PLANNER"):
        addr = role.lower() + "@example.test"
        clients[role] = mailboxes[addr] = Mail(addr, mailboxes)
    when = datetime(2027, 2, 1, tzinfo=timezone.utc)
    with TemporaryDirectory() as directory:
        def reopen():
            return ExpertInbox("Filler-1", when.date(), b"test fixture key", clients, lambda: now, directory)
        now = when
        e = reopen()
        assert set(e.question()) == {"version", "case", "opened", "question", "contacts"}
        assert e.inbox() == [] and e.grade("unknown", [])
        assert e.respond("QA_MGR") is None
        for role in EXPERTS:
            request = e.ask(role)
            assert e.ask(role) == request
            assert len(clients[role].rows) == 1
            now = when + timedelta(hours=2)
            reply = e.respond(role)
            assert reopen().respond(role) == reply
            if role == "MAINT_LEAD":
                assert e.grade("unknown", [reply])
        rows = e.inbox()
        answer = max(row["body"].split()[-1].rstrip(".") for row in rows)
        ids = [row["message_id"] for row in rows]
        assert len(clients["PLANNER"].rows) == 2 and e.grade(answer, ids)
        assert not e.grade(answer, ids[:1]) and not e.grade("unknown", [])
        now = when
        assert not e.inbox() and e.grade("unknown", [])
    print("Two expert replies, scoped evidence, cutoff, replay and private grading passed.")


if __name__ == "__main__":
    check()
