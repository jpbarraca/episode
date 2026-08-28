class ReolinkError(Exception):
    """Base exception for Reolink API errors."""


class ReolinkLoginError(ReolinkError):
    """Authentication failure with the device."""


class ReolinkStreamError(ReolinkError):
    """Failure to discover or access a stream URL."""


class ReolinkEventError(ReolinkError):
    """Failure related to event subscription or polling."""
