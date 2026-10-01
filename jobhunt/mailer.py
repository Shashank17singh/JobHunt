"""Send the digest over SMTP. Gmail: use an App Password, not your login."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


def send(subject: str, html_body: str, attachments: list[str | os.PathLike] | None = None) -> None:
    """Sends an HTML email with optional attachments using SMTP."""
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = os.getenv("MAIL_TO", user)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content("This digest is HTML. Open it in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    if attachments:
        for attachment in attachments:
            path = Path(attachment)
            if path.exists():
                with open(path, "rb") as f:
                    content = f.read()
                maintype = "application"
                subtype = "pdf" if path.suffix.lower() == ".pdf" else "octet-stream"
                msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=path.name)

    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    print(f"  mailed -> {to_addr} with {len(attachments or [])} attachments")
