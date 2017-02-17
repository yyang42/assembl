"""Reindex contents in elasticsearch."""
import logging.config
import argparse
import traceback
import pdb

from pyramid.paster import get_appsettings
import transaction

from assembl.lib.sqla import (
    configure_engine, get_session_maker)
from assembl.lib.zmqlib import configure_zmq
from assembl.lib.config import set_config
from assembl.indexing.reindex import reindex_all_contents

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "configuration",
        help="configuration file with destination database configuration")
    args = parser.parse_args()
    settings = get_appsettings(args.configuration, 'assembl')
    set_config(settings)
    logging.config.fileConfig(args.configuration)
    configure_zmq(settings['changes.socket'], False)
    configure_engine(settings, True)
    session = get_session_maker()()
    try:
        reindex_all_contents(session)
        transaction.commit()
    except Exception as e:
        traceback.print_exc()
        pdb.post_mortem()

if __name__ == '__main__':
    main()
