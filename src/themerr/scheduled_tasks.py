# standard imports
from collections import deque
from datetime import datetime, timezone
import os
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Mapping

# lib imports
import schedule

# local imports
from common import config
from common import logger
from common.notifications import check_for_releases
from media_servers.processing import scheduled_update
from themerr.cache import cache_data

log = logger.get_logger(name=__name__)
_job_lock = threading.RLock()
_jobs = deque(maxlen=30)
_running = {}
SCAN_ON_BOOT_ENV = 'THEMERR_SCAN_ON_BOOT'
_SCAN_TAG = 'theme-scan'
_DISABLED_VALUES = {
    '0',
    'false',
    'no',
    'off',
}


def run_threaded(
        target: Callable,
        daemon: bool = True,
        args: Iterable = (),
        task_name: str | None = None,
        **kwargs: Mapping[str, Any],
) -> threading.Thread:
    """
    Run a function in a thread.

    Allows to run a function in a thread, which is useful for long-running tasks, and it
    allows the main thread to continue.

    Parameters
    ----------
    target : Callable
        The function to run in a thread.
    daemon : bool, default True
        Whether the thread should be a daemon thread. Scheduled work must not keep a stopped process alive.
    args : Iterable
        The positional arguments to pass to the function.
    task_name : str or None, optional
        Human-readable scheduled job name to log at start and finish.
    kwargs : Mapping[str, Any]
        The keyword arguments to pass to the function.

    Returns
    -------
    threading.Thread
        The thread that the function is running in.

    Examples
    --------
    >>> run_threaded(target=log.info, daemon=True, args=['Hello, world!'])
    "Hello, world!"
    """
    with _job_lock:
        if task_name and task_name in _running:
            return _running[task_name]
    job = {'id': uuid.uuid4().hex, 'name': task_name,
           'started': datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'status': 'running', 'duration': None}

    def run_job() -> None:
        started = time.monotonic()
        log.info('Scheduled task started: %s', task_name)
        try:
            target(*args, **kwargs)
        except Exception:
            job['status'] = 'failed'
            log.exception('Scheduled task failed: %s (%.1f seconds)', task_name, time.monotonic() - started)
        else:
            job['status'] = 'finished'
            log.info('Scheduled task finished: %s (%.1f seconds)', task_name, time.monotonic() - started)
        finally:
            with _job_lock:
                job['duration'] = round(time.monotonic() - started, 1)
                _running.pop(task_name, None)

    job_thread = threading.Thread(target=run_job if task_name else target,
                                  args=() if task_name else args, kwargs={} if task_name else kwargs)
    if daemon:
        job_thread.daemon = True
    job_thread.job_id = job['id']
    with _job_lock:
        if task_name:
            if task_name in _running:
                return _running[task_name]
            _running[task_name] = job_thread
            _jobs.appendleft(job)
        job_thread.start()
    return job_thread


def scan_on_boot() -> bool:
    """
    Check whether the theme scan should run when the scheduler starts.

    The ``THEMERR_SCAN_ON_BOOT`` environment variable disables the startup scan when set to
    ``false``, ``0``, ``no``, or ``off``. Any other value, or no value, keeps the scan.
    Scheduled scans still run at the configured interval.

    Returns
    -------
    bool
        True when the startup theme scan should run.

    Examples
    --------
    >>> scan_on_boot()
    True
    """
    return os.environ.get(SCAN_ON_BOOT_ENV, '').strip().lower() not in _DISABLED_VALUES


def schedule_loop() -> None:
    """
    Start the schedule loop.

    Before the schedule loop is started, all jobs are run once.

    Examples
    --------
    >>> schedule_loop()
    ...
    """
    log.info('Scheduler started; dispatching initial jobs')
    if scan_on_boot():
        schedule.run_all()  # run all jobs once
    else:
        log.info('Skipping the startup theme scan because %s is disabled', SCAN_ON_BOOT_ENV)
        skipped = schedule.get_jobs(_SCAN_TAG)
        for job in schedule.jobs[:]:
            if job not in skipped:
                job.run()

    while True:
        schedule.run_pending()
        time.sleep(1)


def configure_jobs() -> None:
    """Apply update intervals and enablement without starting another scheduler loop."""
    schedule.clear('themerr')
    if config.CONFIG['Themerr']['BOOL_THEMERR_ENABLED']:
        schedule.every(max(15, int(config.CONFIG['Themerr']['INT_UPDATE_THEMES_INTERVAL']))).minutes.do(
            job_func=run_threaded,
            target=scheduled_update,
            daemon=True,
            task_name='Theme scan and queue',
        ).tag('themerr', _SCAN_TAG)

    schedule.every(max(15, int(config.CONFIG['Themerr']['INT_UPDATE_DATABASE_CACHE_INTERVAL']))).minutes.do(
        job_func=run_threaded,
        target=cache_data,
        daemon=True,
        task_name='Dashboard refresh',
    ).tag('themerr')

    if config.CONFIG['Notifications']['NEW_RELEASE']:
        schedule.every().hour.do(
            job_func=run_threaded,
            target=check_for_releases,
            daemon=True,
            task_name='Release notification check',
        ).tag('themerr')


def setup_scheduling() -> None:
    """Configure scheduled tasks and start the application's dispatch loop."""
    configure_jobs()

    run_threaded(target=schedule_loop, daemon=True)  # start the schedule loop in a thread


def job_history() -> list[dict]:
    """Return bounded recent task history for the admin dashboard.

    Returns
    -------
    list of dict
        Task name, start time, status, and elapsed duration.
    """
    with _job_lock:
        return [dict(job) for job in _jobs]
