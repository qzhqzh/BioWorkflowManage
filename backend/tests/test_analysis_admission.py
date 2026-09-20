"""Run against a disposable PostgreSQL database; SQLite cannot prove admission locking."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import uuid

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from workflows.analysis_runtime import claim_next_run
from workflows.models import AnalysisRun, WDLAsset, WDLSourceRevision

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def admission(settings):
    if connection.vendor != 'postgresql':
        pytest.skip('real PostgreSQL required')
    settings.ANALYSIS_MAX_ACTIVE_RUNS = 1
    settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB = 0


def make_run(engine='miniwdl', **kwargs):
    asset = WDLAsset.objects.create(slug='admission-'+uuid.uuid4().hex, name='Admission', source_filename='workflow.wdl')
    revision = WDLSourceRevision.objects.create(asset=asset, version=1, operation='import', content='version 1.0\nworkflow Admission {}', digest='sha256:test')
    return AnalysisRun.objects.create(asset=asset, revision=revision, workflow_name='Admission', sample_id='SYNTHETIC', execution_engine=engine, **kwargs)


def test_concurrent_engines_share_one_slot():
    make_run('nextflow'); make_run('miniwdl')
    barrier = Barrier(2)
    def claim(engine):
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            claimed = claim_next_run((engine,))
            return claimed.pk if claimed else None
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ['nextflow', 'miniwdl']))
    assert sum(result is not None for result in results) == 1
    assert AnalysisRun.objects.filter(status='preparing').count() == 1
    assert AnalysisRun.objects.filter(status='queued').count() == 1


def test_cancellation_still_occupies_slot():
    make_run('nextflow', status='cancel_requested', lease_expires_at=timezone.now()+timedelta(minutes=1))
    queued = make_run()
    assert claim_next_run() is None
    queued.refresh_from_db(); assert queued.status == 'queued'


def test_stale_other_engine_is_not_recovered():
    stale = make_run('nextflow', status='running', lease_token=uuid.uuid4(), lease_expires_at=timezone.now()-timedelta(minutes=1))
    make_run()
    assert claim_next_run() is None
    stale.refresh_from_db(); assert stale.status == 'running'; assert stale.lease_token is not None


def test_cleanup_failure_preserves_capacity(monkeypatch):
    stale = make_run(status='running', work_directory='/analysis/runs/test', lease_token=uuid.uuid4(), lease_expires_at=timezone.now()-timedelta(minutes=1))
    queued = make_run()
    monkeypatch.setattr('workflows.analysis_runtime._cleanup_execution_resources', lambda *args: ([], ['daemon unavailable']))
    assert claim_next_run() is None
    stale.refresh_from_db(); queued.refresh_from_db()
    assert stale.status == 'running'; assert stale.lease_token is not None; assert queued.status == 'queued'
    assert stale.events.filter(kind='lease').count() == 1
    assert claim_next_run() is None
    assert stale.events.filter(kind='lease').count() == 1


def test_cleanup_success_releases_slot(monkeypatch):
    stale = make_run(status='running', work_directory='/analysis/runs/test', lease_token=uuid.uuid4(), lease_expires_at=timezone.now()-timedelta(minutes=1))
    queued = make_run()
    monkeypatch.setattr('workflows.analysis_runtime._cleanup_execution_resources', lambda *args: (['old-task'], []))
    assert claim_next_run().pk == queued.pk
    stale.refresh_from_db(); assert stale.status == 'failed'; assert stale.lease_token is None
