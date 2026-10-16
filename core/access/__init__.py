"""Access control: who may do what, in which entity (docs/specs/access.md).

Use ``core.access.require(...)`` in services and ``core.access.scope(...)`` for
entity-filtered querysets. Names are loaded lazily so this package can be
imported while Django apps are still loading.
"""

_EXPORTS = {
    "AccessDenied", "AccessUsageError", "Grant", "require", "has", "entities_for", "scope",
    "get_scoped_or_404", "verify_grant", "require_dual_control", "request_dual_control",
    "decide_dual_control", "system_user", "grant_membership", "revoke_membership", "delegate_approval",
}


def __getattr__(name):
    if name in _EXPORTS:
        from . import services

        return getattr(services, name)
    raise AttributeError(f"module 'core.access' has no attribute {name!r}")
