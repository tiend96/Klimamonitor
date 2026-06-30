"""
Notification channels: e-mail (SMTP / Gmail) and ntfy push (desktop + phone).

All credentials come from environment variables so nothing secret lives in the
repo.  Each sender is best-effort: if it is not configured or fails, it logs and
returns False instead of crashing the run.

Env vars
--------
E-mail (Gmail or any SMTP):
    SMTP_HOST       default smtp.gmail.com
    SMTP_PORT       default 587  (STARTTLS)
    SMTP_USER       your full gmail address
    SMTP_PASS       a Google "App password" (NOT your normal password)
    EMAIL_TO        where to send (defaults to SMTP_USER)

ntfy push:
    NTFY_TOPIC      the topic name you subscribed to in the ntfy app
    NTFY_SERVER     default https://ntfy.sh
    NTFY_TOKEN      optional access token for protected topics
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

import requests


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    return default


def send_email(subject: str, body: str) -> bool:
    host = _env("SMTP_HOST", default="smtp.gmail.com")
    port = int(_env("SMTP_PORT", default="587"))
    user = _env("SMTP_USER", "GMAIL_USER")
    password = _env("SMTP_PASS", "GMAIL_APP_PASSWORD")
    to_addr = _env("EMAIL_TO", default=user)

    if not (user and password and to_addr):
        print("[email] not configured (need SMTP_USER / SMTP_PASS) - skipping")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(body)

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)
        print(f"[email] sent to {to_addr}")
        return True
    except Exception as e:
        print(f"[email] FAILED: {type(e).__name__}: {e}")
        return False


def send_ntfy(title: str, message: str, *, priority: str = "default",
              tags: str = "", click: str = "") -> bool:
    topic = _env("NTFY_TOPIC")
    if not topic:
        print("[ntfy] not configured (need NTFY_TOPIC) - skipping")
        return False
    server = _env("NTFY_SERVER", default="https://ntfy.sh").rstrip("/")
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    token = _env("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(f"{server}/{topic}",
                          data=message.encode("utf-8"),
                          headers=headers, timeout=20)
        if r.status_code < 300:
            print(f"[ntfy] pushed to {server}/{topic}")
            return True
        print(f"[ntfy] FAILED: HTTP {r.status_code} {r.text[:120]}")
        return False
    except Exception as e:
        print(f"[ntfy] FAILED: {type(e).__name__}: {e}")
        return False


def notify(title: str, body: str, *, priority: str = "default",
           tags: str = "bell", click: str = "") -> None:
    """Send to every configured channel. Subject line == ntfy title."""
    send_email(title, body)
    send_ntfy(title, body, priority=priority, tags=tags, click=click)
