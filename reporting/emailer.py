"""
Send daily reports via ProtonMail SMTP.

Credentials in .env:
  PROTON_SMTP_PASSWORD=<app-password>

ProtonMail SMTP: smtp.proton.me:587 (STARTTLS)
"""
from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from loguru import logger

_SMTP_HOST = "smtp.proton.me"
_SMTP_PORT = 587
_SMTP_USER = "tool.ryu@proton.me"


def send_report(subject: str, body_text: str, body_html: str = "") -> bool:
    """Send an email report to the bot owner. Returns True on success."""
    password = os.getenv("PROTON_SMTP_PASSWORD", "").strip()
    if not password:
        logger.warning("[Reporter] PROTON_SMTP_PASSWORD not set — skipping email")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"]    = _SMTP_USER
    msg["To"]      = _SMTP_USER
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))
    if body_html:
        msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(_SMTP_USER, password)
            smtp.sendmail(_SMTP_USER, _SMTP_USER, msg.as_string())
        logger.info(f"[Reporter] Email sent: {subject}")
        return True
    except Exception as exc:
        logger.error(f"[Reporter] Email failed: {exc}")
        return False
