"""Notification channel adapters (SMTP e-mail, Zoom Team Chat webhook, log, null) and their factory."""

from __future__ import annotations

from branchops.application.interfaces.notifications import NotificationChannel
from branchops.config.settings import Settings


def build_channels(settings: Settings) -> dict[str, NotificationChannel]:
    from branchops.infrastructure.notifications.basic import LogChannel, NullChannel

    cfg = settings.notifications
    channels: dict[str, NotificationChannel] = {}
    if cfg.email.backend == "smtp":
        from branchops.infrastructure.notifications.smtp import SmtpEmailChannel

        channels["email"] = SmtpEmailChannel(cfg.email, settings.network)
    else:
        channels["email"] = LogChannel("email") if cfg.email.backend == "log" else NullChannel("email")
    if cfg.zoom.backend == "webhook":
        from branchops.infrastructure.notifications.zoom import ZoomTeamChatChannel

        channels["zoom"] = ZoomTeamChatChannel(cfg.zoom, settings.network)
    else:
        channels["zoom"] = LogChannel("zoom") if cfg.zoom.backend == "log" else NullChannel("zoom")
    return channels
