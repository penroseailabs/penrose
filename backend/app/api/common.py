"""What the API's routers share: the database, the gateway client, and run bookkeeping.

Routers reach these as `common.store` and `common.gateway`, so tests can swap them in one place."""

import contextlib

import httpx
from fastapi import HTTPException

from .. import delivery
from ..bell import Signal, Waiters
from ..store import Store

store = Store()
# The device gateway (app/gateway), opened by the app's lifespan.
gateway: httpx.AsyncClient = None  # type: ignore[assignment]
# The event streams waiting for their run's next events (app/bell.py).
waiters = Waiters()
# The worker's doorbell (app/bell.py Doorbell): notified when the API queues or stops a run. `worker_pending`: news
# that came while the worker held no request, answered at its next one.
worker_news = Signal()
worker_pending = False

# A run in one of these states is an open task: the next message continues it.
OPEN = delivery.OPEN


def require_run(run_id):
    state = store.load(run_id)
    if not state:
        raise HTTPException(404, "Run not found")
    return state


def require_chat(chat_id):
    if not store.rows("SELECT id FROM chats WHERE id=?", (chat_id,)):
        raise HTTPException(404, "Conversation not found")


def lease_owners(run_id, state):
    """Who may hold a lease for this run: the run itself (and, for runs saved before the plan, each node)."""
    return [run_id, *(f"{run_id}:{node_id}" for node_id in state.get("nodes", {}))]


async def release(owners):
    """Free the phone from these lease owners; a gateway that is down holds nothing to free."""
    for owner in owners:
        with contextlib.suppress(httpx.HTTPError):
            await gateway.delete(f"/leases/{owner}")


def requeue(db, state):
    """Hand the run back to the worker, unless its previous job is still unwinding."""
    try:
        delivery.requeue(db, state)
    except delivery.Busy as exc:
        raise HTTPException(409, str(exc)) from None


record = delivery.record
