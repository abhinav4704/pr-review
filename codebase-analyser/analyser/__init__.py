"""Codebase analyser package (manual-first deterministic MVP)."""

from .audit import AuditResult, run_audit, format_audit_report

__all__ = ["AuditResult", "run_audit", "format_audit_report"]
