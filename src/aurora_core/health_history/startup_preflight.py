"""Direct-only startup readiness for owned health-history storage."""

from aurora_core.health_history.database_lifecycle import (
    DatabaseLifecycleError,
    DatabaseLifecycleRejection,
    HealthHistoryDatabaseLifecycle,
)
from aurora_core.health_history.storage_envelope import (
    StorageDecisionResult,
    decide_storage_action,
)


def preflight_health_history_storage(
    lifecycle: HealthHistoryDatabaseLifecycle,
) -> StorageDecisionResult:
    """Inspect one owned Store for startup readiness without remediation."""
    store = lifecycle.store
    if store.closed:
        raise DatabaseLifecycleError(DatabaseLifecycleRejection.TRUST_FAILED) from None
    capacity = store.inspect_storage_capacity()
    free_space = store.inspect_free_space()
    wal = store.inspect_wal()
    return decide_storage_action(
        capacity,
        free_space,
        wal,
        capacity_maintenance_attempted=False,
    )
