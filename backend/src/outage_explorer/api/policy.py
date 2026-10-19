"""Who may do what. Every authorization rule in the API is read from this module.

Roles map to capabilities (which endpoints a user may call at all). Endpoints check the capability
before they touch any data.
"""

from enum import StrEnum


class Role(StrEnum):
    ANALYST = "analyst"
    VIEWER = "viewer"
    ADMIN = "admin"


class Capability(StrEnum):
    CATALOG = "catalog"  # list datasets and their columns
    PREVIEW = "preview"  # read rows of a dataset
    QUERY = "query"  # run read-only SQL
    REFRESH = "refresh"  # reload the data from EIA


_READ = frozenset({Capability.CATALOG, Capability.PREVIEW, Capability.QUERY})

ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.VIEWER: _READ,
    Role.ANALYST: _READ,
    Role.ADMIN: _READ | {Capability.REFRESH},
}


def can(role: Role, capability: Capability) -> bool:
    return capability in ROLE_CAPABILITIES[role]
