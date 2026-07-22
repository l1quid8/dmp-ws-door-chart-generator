"""Typed exceptions for the injector.

A single base class (InjectorError) lets the CLI catch everything the injector
raises deliberately and print a clean message, while still letting unexpected
bugs surface as full tracebacks.
"""
from __future__ import annotations


class InjectorError(Exception):
    """Base class for every error the injector raises on purpose."""


class DriverNotFoundError(InjectorError):
    """The DBISAM v4 ODBC driver is not registered on this machine."""


class SignatureError(InjectorError):
    """The DBISAM driver cannot open the tables — likely a table-signature mismatch."""


class SchemaMismatchError(InjectorError):
    """The production DB's DBINFO version does not match the pinned template version."""


class TemplateError(InjectorError):
    """The golden staging-DB template is missing or malformed."""


class AccountCollisionError(InjectorError):
    """An account with this number already exists in the production database."""


class GuardViolationError(InjectorError):
    """A write was attempted against a forbidden path (e.g. the live C:\\Link\\Db)."""


class WorksheetError(InjectorError):
    """The DMP worksheet could not be read or is missing required data."""


class DBSYSAutomationError(InjectorError):
    """Driving DBSYS via its GUI failed — the caller should fall back to the
    manual handoff (the generated script can still be run by hand)."""


class ODBCApplyError(InjectorError):
    """Applying the clone script through the DBISAM ODBC driver failed — the
    caller should fall back to driving DBSYS."""


class VerificationError(InjectorError):
    """Post-write read-back did not match the source design."""
