"""Envoi du magic-link : SMTP (env SMTP_*) ou mode log (dev) — aucun autre email n'est envoyé."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import settings

log = logging.getLogger("voice-skill.mail")


def _message(to: str, link: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Votre lien d'accès — Générateur de skill de voix de marque"
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg.set_content(
        "Bonjour,\n\n"
        "Voici votre lien d'accès au Générateur de skill de voix de marque IA (valable 30 minutes) :\n\n"
        f"{link}\n\n"
        "Il débloque des générations supplémentaires, l'option « extraits de style » et le téléchargement du skill.\n\n"
        "Si vous n'êtes pas à l'origine de cette demande, ignorez simplement cet email.\n\n"
        "Serge Esteves — creapulse.fr\n"
    )
    return msg


def send_magic_link(to: str, link: str) -> None:
    """Lève une exception si l'envoi SMTP échoue (le front affiche alors un message propre)."""
    if settings.mail_mode == "log":
        log.warning("MAIL_MODE=log — magic-link pour %s : %s", to, link)
        return
    msg = _message(to, link)
    if settings.smtp_port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    try:
        server.ehlo()
        if settings.smtp_tls and settings.smtp_port != 465:
            server.starttls()
            server.ehlo()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:  # pragma: no cover
            pass
