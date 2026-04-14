"""
Background task runner — polls for queued tasks and executes them.

Phase 3 will wire this to codeclub.dev.loop.run().
For now it's a skeleton that transitions task states.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class TaskRunner:
    """Background loop that picks and runs queued tasks."""

    def __init__(self, poll_interval: float = 10.0):
        self._poll_interval = poll_interval
        self._running = True

    def stop(self):
        self._running = False

    async def run_loop(self):
        """Poll every N seconds for queued tasks. Runs as asyncio.create_task()."""
        log.info("Task runner started (poll every %.0fs)", self._poll_interval)
        while self._running:
            try:
                await self._tick()
            except Exception:
                log.exception("Task runner tick failed")
            await asyncio.sleep(self._poll_interval)
        log.info("Task runner stopped")

    async def _tick(self):
        """Single poll cycle — pick highest-priority queued task and run it."""
        # Phase 3: will import get_db, pick a queued task, run the dev loop,
        # stream progress via SSE, and transition through the state machine.
        pass
