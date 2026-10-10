"""本地 SMTP 收信器（aiosmtpd）：测试只发往这里，绝不外发。收件人本地部分含 reject550 → 550 拒收；含 tempfail → 451。"""
import socket
from email import message_from_bytes

from aiosmtpd.controller import Controller


class Handler:
    def __init__(self):
        self.messages = []

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if "reject550" in address:
            return "550 5.1.1 User unknown"
        if "tempfail" in address:
            return "451 4.7.1 Try again later"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.messages.append({"from": envelope.mail_from, "to": list(envelope.rcpt_tos), "msg": message_from_bytes(envelope.content)})
        return "250 Message accepted"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Sink:
    def __init__(self):
        self.handler = Handler()
        self.port = free_port()
        self.ctl = Controller(self.handler, hostname="127.0.0.1", port=self.port)

    def __enter__(self):
        self.ctl.start()
        return self

    def __exit__(self, *a):
        self.ctl.stop()

    @property
    def messages(self):
        return self.handler.messages
