import asyncio
import logging

from app.db.queue import LeaseLost

logger = logging.getLogger(__name__)


class LeasedWorker:
    """Keep a task lease alive and cancel processing if ownership is lost."""

    async def _heartbeat(self, task):
        while True:
            await asyncio.sleep(self.settings.worker_lease_seconds / 3)
            await self.queue.heartbeat(task)

    async def run_once(self):
        task = await self.queue.claim()
        if task is None:
            return False
        work = asyncio.create_task(self._process(task))
        heartbeat = asyncio.create_task(self._heartbeat(task))
        try:
            done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
            else:
                await heartbeat
        except LeaseLost:
            logger.warning("Worker lost its task lease")
        finally:
            for running in (work, heartbeat):
                running.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)
        return True
