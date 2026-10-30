"""Scheduling behavior without a live background loop."""

# standard imports
from threading import Event
from unittest.mock import Mock

# lib imports
import pytest

# local imports
from themerr import scheduled_tasks


def test_run_threaded():
    done = Event()
    thread = scheduled_tasks.run_threaded(target=done.set)
    thread.join(timeout=2)
    assert done.is_set()
    assert not thread.is_alive()
    assert thread.daemon


def test_scheduled_task_logs_start_and_finish(monkeypatch):
    started, finished = Mock(), Mock()
    monkeypatch.setattr(scheduled_tasks.log, 'info', started)
    job = scheduled_tasks.run_threaded(target=finished, task_name='Dashboard refresh')
    job.join(timeout=2)

    finished.assert_called_once_with()
    assert started.call_args_list[0].args == ('Scheduled task started: %s', 'Dashboard refresh')
    assert started.call_args_list[1].args[:2] == ('Scheduled task finished: %s (%.1f seconds)', 'Dashboard refresh')


def test_scheduled_task_failure_is_logged_and_thread_exits(monkeypatch):
    error = Mock()
    monkeypatch.setattr(scheduled_tasks.log, 'exception', error)
    job = scheduled_tasks.run_threaded(target=Mock(side_effect=RuntimeError('failed')),
                                       task_name='Theme scan and queue')
    job.join(timeout=2)

    assert not job.is_alive()
    assert error.call_args.args[:2] == ('Scheduled task failed: %s (%.1f seconds)', 'Theme scan and queue')


def test_schedule_loop(configured, monkeypatch):
    run_all = Mock()
    pending = Mock()

    def sleep(seconds):
        assert seconds == 1
        run_all.assert_called_once()
        pending.assert_called_once()
        raise RuntimeError('stop')

    monkeypatch.setattr(scheduled_tasks.time, 'sleep', sleep)
    monkeypatch.setattr(scheduled_tasks.schedule, 'run_all', run_all)
    monkeypatch.setattr(scheduled_tasks.schedule, 'run_pending', pending)
    try:
        scheduled_tasks.schedule_loop()
    except RuntimeError:
        pass
    run_all.assert_called_once()
    pending.assert_called_once()


def test_setup_scheduling(configured, monkeypatch):
    started = Mock()
    monkeypatch.setattr(scheduled_tasks, 'run_threaded', started)
    scheduled_tasks.schedule.clear()
    configured['Themerr']['BOOL_THEMERR_ENABLED'] = True
    scheduled_tasks.setup_scheduling()
    assert len(scheduled_tasks.schedule.jobs) == 3
    assert all(job.job_func.keywords['daemon'] for job in scheduled_tasks.schedule.jobs)
    started.assert_called_once_with(target=scheduled_tasks.schedule_loop, daemon=True)
    scheduled_tasks.schedule.clear()

    configured['Themerr']['BOOL_THEMERR_ENABLED'] = False
    scheduled_tasks.setup_scheduling()
    assert len(scheduled_tasks.schedule.jobs) == 2
    scheduled_tasks.schedule.clear()


def test_duplicate_dispatch_returns_the_running_job_and_history_is_a_copy(monkeypatch):
    monkeypatch.setattr(scheduled_tasks, '_running', {})
    from collections import deque
    monkeypatch.setattr(scheduled_tasks, '_jobs', deque(maxlen=30))
    started, release = Event(), Event()

    def refresh():
        started.set()
        release.wait(timeout=2)

    first = scheduled_tasks.run_threaded(target=refresh, task_name='Dashboard refresh')
    try:
        assert started.wait(timeout=2)
        assert scheduled_tasks.run_threaded(target=refresh, task_name='Dashboard refresh') is first
        history = scheduled_tasks.job_history()
        assert len(history) == 1
        assert history[0]['id'] == first.job_id
        history[0]['status'] = 'modified'
        assert scheduled_tasks.job_history()[0]['status'] == 'running'
    finally:
        release.set()
        first.join(timeout=2)
    assert scheduled_tasks.job_history()[0]['status'] == 'finished'
    assert scheduled_tasks.job_history()[0]['duration'] is not None
    assert not scheduled_tasks._running


def test_reconfigure_changes_intervals_without_duplicating_or_removing_other_jobs(configured):
    scheduler = scheduled_tasks.schedule
    scheduler.clear()
    unrelated = scheduler.every().hour.do(lambda: None).tag('other')
    try:
        configured['Themerr']['BOOL_THEMERR_ENABLED'] = True
        configured['Themerr']['INT_UPDATE_THEMES_INTERVAL'] = 30
        scheduled_tasks.configure_jobs()
        scheduled_tasks.configure_jobs()
        assert len(scheduler.jobs) == 4
        assert scheduler.get_jobs('themerr')[0].interval == 30
        configured['Themerr']['BOOL_THEMERR_ENABLED'] = False
        configured['Themerr']['INT_UPDATE_DATABASE_CACHE_INTERVAL'] = 45
        scheduled_tasks.configure_jobs()
        assert scheduler.get_jobs('other') == [unrelated]
        assert len(scheduler.get_jobs('themerr')) == 2
        assert scheduler.get_jobs('themerr')[0].interval == 45
        configured['Notifications']['NEW_RELEASE'] = False
        scheduled_tasks.configure_jobs()
        assert len(scheduler.get_jobs('themerr')) == 1
        assert scheduler.get_jobs('other') == [unrelated]
    finally:
        scheduler.clear()


def test_scan_on_boot_defaults_to_enabled(configured):
    assert configured['Themerr']['BOOL_SCAN_ON_BOOT'] is True
    assert scheduled_tasks.scan_on_boot() is True


def test_disabled_scan_on_boot_survives_a_config_reload(configured, monkeypatch):
    from common import config
    configured['Themerr']['BOOL_SCAN_ON_BOOT'] = False
    assert config.save_config(configured)
    reloaded = config.create_config(configured.filename)
    assert reloaded['Themerr']['BOOL_SCAN_ON_BOOT'] is False
    monkeypatch.setattr(config, 'CONFIG', reloaded)
    assert scheduled_tasks.scan_on_boot() is False


def test_schedule_loop_skips_only_the_startup_scan_when_disabled(configured, monkeypatch):
    scheduler = scheduled_tasks.schedule
    scheduler.clear()
    dispatched = []
    monkeypatch.setattr(scheduled_tasks, 'run_threaded', lambda **kwargs: dispatched.append(kwargs['task_name']))

    def stop(seconds):
        raise RuntimeError('stop')

    monkeypatch.setattr(scheduled_tasks.time, 'sleep', stop)
    try:
        configured['Themerr']['BOOL_THEMERR_ENABLED'] = True
        configured['Themerr']['BOOL_SCAN_ON_BOOT'] = False
        scheduled_tasks.configure_jobs()
        with pytest.raises(RuntimeError):
            scheduled_tasks.schedule_loop()
        assert 'Theme scan and queue' not in dispatched
        assert 'Dashboard refresh' in dispatched
        scan_jobs = scheduler.get_jobs('theme-scan')
        assert len(scan_jobs) == 1
        assert scan_jobs[0].next_run is not None
    finally:
        scheduler.clear()
