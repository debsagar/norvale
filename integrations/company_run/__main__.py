"""Resume the isolated recurring company demo. Each Engine.step checkpoints one phase."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from native.company import Engine, DEFAULT_CONFIG, initialize
from . import timing


class AcceptanceEngine(Engine):
    def scheduled_company_event(self, clock, payload):
        return timing.dispatch_event(self, clock, payload)

    def run_phase(self, day, key, action):
        super().run_phase(day, key, action)
        self.day, self.event = day, key

    def recurring_events(self):
        yield from super().recurring_events()
        if 'company_timing' not in self.config:
            # Timed completion already posts on its own clock edge. A shift in
            # the untimed acceptance entry can finish after its daily poll.
            day = self.config['days'] - 1
            yield day, f'd{day:04}:completion-readback', self.company_erp.poll_work

    def write(self, key, dt, data, role=None, submit=False, before_submit=None):
        if dt == 'Delivery Note' and before_submit and not self.state.get('demo_revocation'):
            guard = before_submit
            def revoke_then_check(draft):
                from integrations.qms.adapter import QMS
                shipment = self.company_erp.state['shipments'][key]
                batch = shipment['rows'][0]['batch']
                inspection = self.state['acceptance_assays'][batch]['id']
                cfg = self.company_erp.authority.settings()
                doc = QMS(**cfg['lab']['approver']).transition(inspection, 'Revoke Release')
                self.state['demo_revocation'] = dict(batch=batch, inspection=doc['name'],
                                                     draft=draft['name'], version=doc['modified'])
                self.persist()
                guard(draft)
            before_submit = revoke_then_check
        return super().write(key, dt, data, role, submit, before_submit=before_submit)

    def inspections(self):
        if (self.run / 'env/company-sources.json').exists():
            from .demo import Demo
            demo = Demo(self)
            if 'company_timing' in self.config:
                timing.queue_samples(self, self.additions, self.additions.now + 1, demo.eligible_samples())
            else:
                demo.samples()
        super().inspections()

    def production(self):
        if not (self.run / 'env/company-sources.json').exists():
            return super().production()
        from .demo import Demo
        demo = Demo(self)
        for order in self.state['ops']['orders'].values():
            if not order.get('production_profile'):
                demo.publish(order['item'])
        super().production()
        if 'company_timing' in self.config:
            timing.queue_dispatches(self, demo)
        else:
            demo.execute()

    def choose(self, key, role, evidence, question, action='approve', model=False):
        # Explicit local role inputs apply only in this entry point.
        if key in self.state['decisions']:
            return self.state['decisions'][key]['output']
        if role == 'SCM_MGR' and key.startswith('po-review:'):
            quote = evidence['supplier_quote']
            approve = (evidence['supplier'] == quote['supplier'] and
                       evidence['grand_total'] == quote['grand_total'] and
                       quote['valid_till'] >= self.date() and evidence['grand_total'] > 0)
        elif role == 'CFO' and key.startswith('review:'):
            approve = bool(evidence['native_balances']) and not evidence['pending_requests']
        elif role == 'SCM_MGR' and key.startswith('change-review:'):
            approve = False
        else:
            raise ValueError('No acceptance input for ' + key)
        reason = 'Local role review of dated native evidence.'
        output = dict(action='approve' if approve else 'defer', reason=reason, message=reason)
        self.state['decisions'][key] = dict(mode='local-acceptance', output=output,
            input=dict(role=role, evidence=evidence, as_of=self.date(), question=question))
        self.persist()
        return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--through', type=int)
    args = parser.parse_args()
    run = args.run.resolve()
    os.umask(0o077)
    os.environ['NORVALE_OFFLINE'] = '1'
    factory = 'integrations.company_run.authority:Authority'
    if not (run / 'config.json').exists():
        initialize(run, dict(DEFAULT_CONFIG, days=args.days, orders_per_day=1, report_every=2,
                             company_authority=factory, company_timing=timing.DEFAULT_TIMING))
    elif json.loads((run / 'config.json').read_text()).get('company_authority') != factory:
        parser.error('Choose a dedicated company acceptance directory')
    if json.loads((run / 'config.json').read_text()).get('company_operating'):
        parser.error('Qualification requires a dedicated acceptance directory')
    if not (run / 'env/site-created').exists():
        initialize(run)
    if not (run / 'ticket-check.json').exists():
        subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] /
                        'native/provision_tickets_v2.py'), str(run)], check=True)
    engine = AcceptanceEngine(run)
    engine.through(engine.config['days'] - 1 if args.through is None else args.through)


if __name__ == '__main__':
    main()
