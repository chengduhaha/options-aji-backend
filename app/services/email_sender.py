"""Transactional email delivery for auth verification codes."""

from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Literal

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

EmailProvider = Literal["smtp", "resend", "console"]


class EmailSendError(Exception):
    """Raised when outbound email could not be delivered."""


def is_email_configured(settings: Settings | None = None) -> bool:
    cfg = settings or get_settings()
    provider = (cfg.email_provider or "smtp").strip().lower()
    from_email = cfg.smtp_from_email.strip()
    if not from_email:
        return False
    if provider == "resend":
        return bool(cfg.resend_api_key.strip())
    if provider == "smtp":
        return bool(cfg.smtp_host.strip())
    if provider == "console":
        return True
    return False


def _format_expires_zh(expires_at: datetime) -> str:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _verification_subject(settings: Settings) -> str:
    name = settings.smtp_from_name.strip() or "OptionsAji"
    return f"{name} 邮箱验证码"


def _verification_bodies(code: str, expires_at: datetime, settings: Settings) -> tuple[str, str]:
    name = settings.smtp_from_name.strip() or "OptionsAji"
    expires_zh = _format_expires_zh(expires_at)
    text = (
        f"您好，\n\n"
        f"您正在注册 {name} 账号。验证码为：{code}\n"
        f"有效期至：{expires_zh}\n\n"
        f"如非本人操作，请忽略此邮件。\n"
    )
    html = (
        f"<p>您好，</p>"
        f"<p>您正在注册 <strong>{name}</strong> 账号。</p>"
        f"<p style='font-size:22px;letter-spacing:4px;font-weight:bold'>{code}</p>"
        f"<p>有效期至：<strong>{expires_zh}</strong></p>"
        f"<p style='color:#666'>如非本人操作，请忽略此邮件。</p>"
    )
    return text, html


def _from_header(settings: Settings) -> str:
    email = settings.smtp_from_email.strip()
    name = settings.smtp_from_name.strip() or "OptionsAji"
    return f"{name} <{email}>"


def _send_via_smtp(*, to_email: str, subject: str, text: str, html: str, settings: Settings) -> None:
    host = settings.smtp_host.strip()
    port = int(settings.smtp_port)
    username = settings.smtp_username.strip()
    password = settings.smtp_password.strip()
    from_email = settings.smtp_from_email.strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _from_header(settings)
    msg["To"] = to_email
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        if settings.smtp_use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.sendmail(from_email, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if settings.smtp_use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                if username:
                    smtp.login(username, password)
                smtp.sendmail(from_email, [to_email], msg.as_string())
    except OSError as exc:
        raise EmailSendError(f"smtp_connection_failed: {exc}") from exc
    except smtplib.SMTPException as exc:
        raise EmailSendError(f"smtp_send_failed: {exc}") from exc


def _send_via_resend(*, to_email: str, subject: str, text: str, html: str, settings: Settings) -> None:
    api_key = settings.resend_api_key.strip()
    payload = {
        "from": _from_header(settings),
        "to": [to_email],
        "subject": subject,
        "text": text,
        "html": html,
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 400:
                raise EmailSendError(f"resend_http_{resp.status_code}: {resp.text[:300]}")
    except httpx.HTTPError as exc:
        raise EmailSendError(f"resend_request_failed: {exc}") from exc


def send_verification_email(
    *,
    to_email: str,
    code: str,
    expires_at: datetime,
    settings: Settings | None = None,
) -> None:
    """Send a 6-digit registration verification code. Raises :class:`EmailSendError` on failure."""

    cfg = settings or get_settings()
    provider = (cfg.email_provider or "smtp").strip().lower()
    subject = _verification_subject(cfg)
    text, html = _verification_bodies(code, expires_at, cfg)
    recipient = to_email.strip().lower()

    if provider == "console":
        logger.info(
            "EMAIL_CONSOLE to=%s subject=%s code=%s expires=%s",
            recipient,
            subject,
            code,
            expires_at.isoformat(),
        )
        return

    if not is_email_configured(cfg):
        raise EmailSendError("email_not_configured")

    if provider == "resend":
        _send_via_resend(to_email=recipient, subject=subject, text=text, html=html, settings=cfg)
    elif provider == "smtp":
        _send_via_smtp(to_email=recipient, subject=subject, text=text, html=html, settings=cfg)
    else:
        raise EmailSendError(f"unknown_email_provider:{provider}")

    logger.info("Verification email sent to=%s provider=%s", recipient, provider)
