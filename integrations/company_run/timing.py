"""Schedule native company handoffs on the recurring controller's durable queue."""
from datetime import timedelta
import json
from fractions import Fraction
from math import ceil

from engine import DAY
from native.recurring_extensions import COMPANY_OPEN, MIDNIGHT
from .demo import Demo
from .operating import Operating
from .production_batch import backorder, queue_operation
from integrations.qms.adapter import APIError
from native.company_erp import Pending
from subprocess import CalledProcessError
from urllib.error import URLError
from xmlrpc.client import Fault


DEFAULT_TIMING = {
    'shifts': {'PRODUCTION': [[480, 960]], 'LAB': [[540, 1020]], 'QA': [[540, 1020]]},
    'staff': {'LAB': 1, 'QA': 1},
    'task_minutes': {'sample': 15, 'result': 10, 'review': 15},
    'quality_due_minutes': DAY,
    'lab_minutes': 90,
}


def schedule(clock, job, edge, at, *, role=None, duration=0, elapsed=False, attempt=0, rows=None):
    # Keep the complete source, operation and stage identity in every edge.
    event = 'company:' + json.dumps([job, edge, attempt], separators=(',', ':'))
    if not clock.has_event(event):
        clock.schedule(event, 'company-native', at, role=role, duration=duration, elapsed=elapsed,
                       payload=dict(job=job, edge=edge, attempt=attempt, rows=rows))


def queue_dispatches(company, demo):
    clock = company.additions
    for work in company.company_erp.state['work'].values():
        if (not work.get('dispatch') or work.get('rejected_remainder')
                or work.get('produced', 0) >= work['qty']
                or (work.get('produced') and not work.get('production_operations'))):
            continue
        allowed = (company.company_erp.attempt('mes-operator:' + work['key'], 'PRODUCTION',
                                              lambda: demo.scheduled(work))
                   if isinstance(demo, Operating) else demo.local_work(work))
        if allowed:
            ack = work['dispatch']
            # Legacy checkpoints keep their existing event and labor identities.
            job = ['mes', ack.get('source'), ack['id'], work['request']['key'], work['key']]
            legacy_event = 'company:' + json.dumps([job, 'start', 0], separators=(',', ':'))
            bounded = (isinstance(allowed, Operating) and
                       (work.get('production_operations') or allowed.cfg.get('production_batch')) and
                       (work.get('production_operations') or not (
                           any(k in work for k in ('mes_start', 'scheduled_labor', 'mes_consumption'))
                           or clock.has_event(legacy_event))))
            if bounded:
                ack = company.company_erp.attempt('mes-batch:' + work['key'], 'PRODUCTION',
                                                  lambda: queue_operation(company, work, allowed))
                if ack is None:
                    continue
            job = ['mes', ack.get('source'), ack['id'], work['request']['key'], work['key']]
            schedule(clock, job, 'start', clock.now + 1, role='PRODUCTION')


def queue_samples(company, clock, at, batches):
    if company.config.get('company_operating'):
        timing = company.config.get('company_timing', {})
        for batch in batches:
            job = ['lab', batch, None]
            schedule(clock, job, 'sample', at, role='LAB', duration=timing.get('task_minutes', {}).get('sample', 0))
            if 'quality_due_minutes' in timing:
                due = at + timing['quality_due_minutes']
                schedule(clock, job, 'deadline', due + 1, role='PLANNER', rows=due)
        return
    assays = company.state.get('acceptance_assays', {})
    for batch in batches:
        prior = assays.get(batch)
        if prior and prior['state'] != 'Rework Authorized':
            continue
        job = ['lab', batch, prior['id'] if prior else None]
        due = max(at, (prior['day'] + 1) * DAY + COMPANY_OPEN) if prior else at
        schedule(clock, job, 'sample', due, role='LAB')


def operating_quality(company, clock, actor, job, edge, attempt, rows):
    flow = company.company_erp
    batch = job[1]
    now = clock.now
    role = {'sample': 'LAB', 'result': 'LAB', 'review': 'QA', 'rework': 'PRODUCTION'}.get(edge)
    minutes = company.config.get('company_timing', {}).get('task_minutes', {})
    if edge == 'deadline':
        reviewed = clock.state.get('quality_reviewed', {}).get(batch)
        if reviewed is not None and reviewed <= rows:
            return
        lot = flow.state['lots'][batch]
        def notify():
            source = company.api['PLANNER'].get(lot['source_type'], lot['source_id'])
            if source['name'] != lot['source_id'] or source.get('docstatus') != 1:
                raise Pending('Planner cannot confirm the posted lot reference')
            fields = dict(reference_type=lot['source_type'], reference_name=source['name'],
                          allocated_to='planner@norvale.test', date=company.dated(rows // DAY),
                          description=f'Quality review overdue for lot {batch}, due at business minute {rows}. '
                                      'Review any effect on open order commitments.')
            task = company.write('overdue-quality:' + batch, 'ToDo', fields, 'PLANNER')
            if any(task.get(k) != v for k,v in fields.items()):
                raise Pending('Overdue quality task differs from its intended recipient or reference')
            return task['name']
        if flow.attempt('quality-deadline:' + batch, 'PLANNER', notify) is None:
            schedule(clock, job, edge, now + DAY, role='PLANNER', attempt=attempt + 1, rows=rows)
        return
    if edge == 'incubate':
        schedule(clock, job, 'result', now, role='LAB', rows=rows, duration=minutes.get('result', 0))
        return

    def perform():
        try:
            if edge == 'sample':
                return actor.sample(batch)
            if edge == 'result':
                return actor.record(batch, rows)
            if edge == 'review':
                return actor.review(batch, rows)
            center = str(actor.rework_center(batch))
            slot = clock.state.get('company_running', {}).get(center)
            if slot is not None:
                slot.setdefault('rework_waiting', []).append([job, attempt, rows])
                return False
            return actor.rework(batch, rows)
        except Pending:
            raise
        except APIError as exc:
            raise Pending(f'Native {edge}: {exc}') from exc
        except (CalledProcessError, Fault, ValueError, KeyError, TypeError) as exc:
            raise Pending(f'Native evidence unavailable at {edge} ({type(exc).__name__})') from exc

    result = flow.attempt(f'operating-{edge}:{batch}', role, perform)
    if result is None:
        retry = (now // DAY + 1) * DAY + COMPANY_OPEN
        schedule(clock, job, edge, retry, role=role, attempt=attempt + 1, rows=rows,
                 duration=30 if edge == 'rework' else minutes.get(edge, 0))
        return
    if edge == 'sample':
        schedule(clock, job, 'incubate', now, rows=result, elapsed=True,
                 duration=company.config['company_timing']['lab_minutes'])
    elif edge == 'result':
        schedule(clock, job, 'review', now + 1, role='QA', rows=result, duration=minutes.get('review', 0))
    elif edge == 'review':
        if 'quality_due_minutes' in company.config.get('company_timing', {}):
            clock.state.setdefault('quality_reviewed', {})[batch] = now
        if result == 'Rework Authorized':
            schedule(clock, job, 'rework', now + 1, role='PRODUCTION', duration=30,
                     rows=rows['inspection'] if isinstance(rows, dict) else rows)
        flow.attempt('sync:' + batch, 'QA', lambda: flow.sync(flow.state['lots'][batch]))
    elif result:
        queue_samples(company, clock, now, [result])


def handle(company, clock, payload):
    job, edge, attempt, rows = (payload[k] for k in ('job', 'edge', 'attempt', 'rows'))
    flow = company.company_erp
    now = clock.now
    actor = Operating(company) if company.config.get('company_operating') else Demo(company)
    if job[0] == 'lab':
        if company.config.get('company_operating'):
            return operating_quality(company, clock, actor, job, edge, attempt, rows)
        if edge == 'sample':
            actor.prepare_samples([flow.state['lots'][job[1]]])
            schedule(clock, job, 'incubate', now, duration=company.config['company_timing']['lab_minutes'],
                     elapsed=True)
        elif edge == 'incubate':
            schedule(clock, job, 'result', now, role='LAB')
        else:
            actor.samples(job[1])
            flow.attempt('quality-reconciliation', 'QA', flow.inspections)
            queue_samples(company, clock, now, [job[1]])
        return

    parent = flow.state['work'][job[-1]]
    operations = parent.get('production_operations')
    work = parent if operations is None else operations[job[2]]
    ack = work['dispatch']
    if job != ['mes', ack.get('source'), ack['id'], work['request']['key'], work['key']]:
        raise ValueError('Scheduled production event differs from the pinned dispatch')

    operating = isinstance(actor, Operating)

    def operate(method, *args, **kwargs):
        participant = actor.scheduled(work) if operating else actor
        if participant is None:
            raise Pending('Scheduled work has no authorized MES operator')
        if method == 'start' and 'native_group' in work:
            remaining = backorder(company, parent)
            if (str(remaining['backorder']), remaining['group'], remaining['remaining']) != (
                    ack['id'], work['native_group'], work['qty']):
                raise Pending('Scheduled MO differs from the remaining native work')
        action = getattr(participant, method)
        try:
            return action(work, *args, **kwargs)
        except (URLError, TimeoutError, ConnectionError):
            if not operating:
                raise
            # Native replay checks the pinned reservation or completion.
            return action(work, *args, **kwargs)

    operation_key = work['key'] if operations is None else json.dumps(job[1:])
    center = str(work['equipment'])
    running = clock.state.setdefault('company_running', {})
    retry_at = (now // DAY + 1) * DAY + COMPANY_OPEN + 1

    def release_center():
        slot = running.pop(center)
        for waiting_job, previous, inspection in slot.get('rework_waiting', []):
            schedule(clock, waiting_job, 'rework', now, role='PRODUCTION',
                     attempt=previous + 1, rows=inspection)
        for waiting_job, previous, *intent in slot['waiting']:
            schedule(clock, waiting_job, 'start', now, role='PRODUCTION',
                     attempt=previous + 1, rows=intent[0] if intent else None)

    if edge == 'start':
        slot = running.get(center)
        owned = slot is not None and slot.get('owner') == job
        if parent.get('rejected_remainder'):
            if owned:
                release_center()
            return
        if slot is not None and not owned:
            # The owner wakes queued jobs when the native operation leaves its center.
            slot['waiting'].append([job, attempt, rows])
            return

        kwargs = {'scheduled': True}
        if operating:
            rows = rows or dict(event=clock.event['id'], at=now, source=ack.get('source'),
                                dispatch=ack['id'])
            kwargs['intent'] = rows
        started = flow.attempt('mes-start:' + operation_key, 'PRODUCTION',
                               lambda: operate('start', **kwargs), defer_transport=operating)
        if started is None:
            if operating and work.get('mes_start') and not owned:
                running[center] = dict(owner=job, waiting=[])
            schedule(clock, job, edge, retry_at, role='PRODUCTION', attempt=attempt + 1, rows=rows)
        elif started:
            if not owned:
                running[center] = dict(owner=job, waiting=[])
            duration = ceil(Fraction(str(actor.output_quantity(work))) *
                            Fraction(str(company.config.get('shift_minutes', 480))) /
                            Fraction(str(company.config['capacity_units'])))
            schedule(clock, job, 'complete', now, role='PRODUCTION', duration=duration, rows=started)
        else:
            if operations is not None:
                work['definitive_stop'] = True
                company.persist()
            if owned:
                release_center()
        return

    # A deferred native finish has a zero-duration retry. Retain the original
    # shift allocation before calling MES; native evidence separately establishes execution.
    if not attempt and not parent.get('rejected_remainder'):
        event = clock.event
        intervals = clock.shift_intervals(event['role'], clock.event_started_at, now)
        if sum(end - start for start, end in intervals) != event['duration']:
            raise ValueError('Production shift intervals disagree with scheduled work')
        reservation = dict(event=event['id'], role=event['role'], center=work['equipment'],
                           dispatch=work['dispatch']['id'], source=work['dispatch'].get('source'),
                           work_order=work['request']['work_order'], intervals=intervals,
                           minutes=event['duration'])
        if work.setdefault('scheduled_labor', reservation) != reservation:
            raise ValueError('Production labor reservation changed during replay')
        company.persist()

    finished = flow.attempt('mes-finish:' + operation_key, 'PRODUCTION',
                            lambda: parent.get('rejected_remainder') or operate('complete', rows),
                            defer_transport=operating)
    if finished is None:
        schedule(clock, job, edge, retry_at, role='PRODUCTION', attempt=attempt + 1, rows=rows)
        return
    if operations is not None and finished is False:
        work['definitive_stop'] = True
        company.persist()
    if operations is not None and finished is True and 'completed_day' not in work:
        work['completed_day'] = company.day
        company.persist()
    release_center()
    flow.attempt('mes-poll:' + work['key'], 'PRODUCTION', lambda: flow.equipment.native(flow.collect, parent))
    flow.poll_work(poll=False)
    batches = [batch for batch, lot in flow.state['lots'].items()
               if lot.get('work_order') == work['request']['work_order']]
    if actor.local_work(parent):
        queue_samples(company, clock, now, batches)


def dispatch_event(company, clock, payload):
    company.day, company.event = clock.now // DAY, clock.event['id']
    company.advance_clock(MIDNIGHT + timedelta(minutes=clock.now))
    handle(company, clock, payload)
    company.log('scheduled_company_work', minute=clock.now, started_at=clock.event_started_at,
                role=clock.event['role'])
