"""
credentials.py
--------------
The myTNB password is kept in Windows Credential Manager (via keyring), never in settings.json.
Look for it under "TNB Smart Sync" in Control Panel > Credential Manager > Windows Credentials.

It is saved as "local machine" so it stays on this PC only: it does not roam to other PCs with
a domain/roaming Windows profile, and copying the app folder (settings.json included) to another
PC brings the email across but not the password -- that PC asks for it once.
"""

from __future__ import annotations

import logging

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

logger = logging.getLogger(__name__)

SERVICE = "TNB Smart Sync"
LOCAL_MACHINE = "local_machine"  # keyring's name for win32cred.CRED_PERSIST_LOCAL_MACHINE


def _keyring():
    """The active keyring, set to keep new passwords on this PC only (Windows backend)."""
    backend = keyring.get_keyring()
    if hasattr(backend, "persist"):  # only the Windows Credential Manager backend has this
        backend.persist = LOCAL_MACHINE
    return backend


def get_password(email: str) -> str | None:
    if not email:
        return None
    try:
        return _keyring().get_password(SERVICE, email)
    except KeyringError as exc:
        logger.error("Could not read Credential Manager: %s", exc)
        return None


def set_password(email: str, password: str) -> None:
    if not email or not password:
        raise ValueError("Email and password are both required.")
    _keyring().set_password(SERVICE, email, password)


def delete_password(email: str) -> None:
    if not email:
        return
    try:
        _keyring().delete_password(SERVICE, email)
    except PasswordDeleteError:
        pass  # nothing stored
