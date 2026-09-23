"""Governed canonical book identity migration public API."""

from book_translator.book_identity.engine import (
    BOOK_IDENTITY_MIGRATION_VERSION,
    BookIdentityRewriteError,
    RewriteResult,
    ScalarRewrite,
    apply_scalar_rewrites,
    migration_identity,
    semantic_identity,
    strip_paths,
)
from book_translator.book_identity.workflow import (
    REGISTRY_TARGETS,
    BookIdentityMigrationError,
    BookIdentityMigrationPlan,
    PlannedRegistry,
    RegistryTarget,
    execute_book_identity_migration,
    load_migrations,
    plan_book_identity_migration,
    serialize_migration,
)

__all__ = [
    "BOOK_IDENTITY_MIGRATION_VERSION",
    "REGISTRY_TARGETS",
    "BookIdentityMigrationError",
    "BookIdentityMigrationPlan",
    "BookIdentityRewriteError",
    "PlannedRegistry",
    "RegistryTarget",
    "RewriteResult",
    "ScalarRewrite",
    "apply_scalar_rewrites",
    "execute_book_identity_migration",
    "load_migrations",
    "migration_identity",
    "plan_book_identity_migration",
    "semantic_identity",
    "serialize_migration",
    "strip_paths",
]
