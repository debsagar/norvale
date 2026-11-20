"""Copy the PLM publisher's approved business record export to its private analytical archive."""
import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from integrations.plm import PLM
from integrations.lake.plm_reader import scan_publications


def families(config):
    return sorted({product['family'] for product in config['products'].values()})


def publication_source(config, reader):
    source = config.get('plm_source') or config['source'] + '-plm'
    if reader is not None and (reader['source'] != source
            or reader['reader']['url'].rstrip('/') != config['plm']['url'].rstrip('/')):
        raise ValueError('PLM analytics reader and company publisher target different sources')
    return source


def collect(run, lake, *, reader=None, on=None):
    config = json.loads((run/'env/company-sources.json').read_text())
    if on is None:
        state = json.loads((run/'state.json').read_text())
        on = (date(2027, 1, 1) + timedelta(days=state['day'])).isoformat()
    source = publication_source(config, reader)
    client = PLM(**config['plm'])
    return scan_publications(lake, source, client, families(config), on)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    print(collect(args.run, args.archive))
