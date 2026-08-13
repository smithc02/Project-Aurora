"""Direct-only startup WAL checkpoint composition for owned history storage."""

from aurora_core.health_history.database_lifecycle import (
    DatabaseLifecycleError,
    DatabaseLifecycleRejection,
    HealthHistoryDatabaseLifecycle,
)
from aurora_core.health_history.startup_preflight import (
    preflight_health_history_storage,
)
from aurora_core.health_history.storage_envelope import (
    PassiveCheckpointOutcome,
    StorageDecisionOutcome,
    StorageDecisionResult,
)
from aurora_core.health_history.store import StoreError


def checkpoint_health_history_startup_wal(
    lifecycle: HealthHistoryDatabaseLifecycle,
) -> StorageDecisionResult:
    """Attempt one PASSIVE checkpoint only when startup WAL pressure requires it."""
    initial = preflight_health_history_storage(lifecycle)
    if initial.outcome is not StorageDecisionOutcome.WAL_CHECKPOINT_DUE:
        return initial

    store = lifecycle.store
    if store.closed:
        raise DatabaseLifecycleError(DatabaseLifecycleRejection.TRUST_FAILED) from None
    try:
        checkpoint = store.passive_wal_checkpoint()
    except StoreError as error:
        if error.reason == "store_closed":
            raise DatabaseLifecycleError(
                DatabaseLifecycleRejection.TRUST_FAILED
            ) from None
        raise

    if checkpoint.outcome is PassiveCheckpointOutcome.BUSY:
        return initial
    return preflight_health_history_storage(lifecycle)
