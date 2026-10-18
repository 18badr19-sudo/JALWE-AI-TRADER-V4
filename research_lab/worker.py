"""Private child entry point. Install isolation BEFORE importing native code."""
import json
import logging
import os
from pathlib import Path
import sys
import tempfile


def main():
    bundle = json.load(sys.stdin)
    with tempfile.TemporaryDirectory(prefix='jalwe-offline-lab-') as directory:
        os.environ.clear()
        os.environ.update(ALPACA_API_KEY='offline-no-credentials',
            ALPACA_SECRET_KEY='offline-no-credentials',
            ALPACA_BASE_URL='https://paper-api.alpaca.markets',
            PAPER_TRADING='true', ALLOW_LIVE_TRADING='false',
            JALWE_AUTO_PAPER_EXECUTION='false', JALWE_BROKER_SUBMISSION='false',
            JALWE_DECISION_REPLAY_ENABLED='false', JALWE_LEARNING_ENABLED='false',
            JALWE_CONTROLLER_DATA_DIR=directory,
            JALWE_DATABASE_PATH=str(Path(directory) / 'isolated.db'))
        import dotenv
        dotenv.load_dotenv = lambda *a, **k: False

        def boundary(event, args):
            if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                         'socket.bind', 'subprocess.Popen', 'os.system'}:
                raise PermissionError('OFFLINE_LAB_NETWORK_AND_PROCESS_DENIED')
            if event == 'sqlite3.connect' and str(args[0]) != ':memory:':
                if not Path(str(args[0])).resolve().is_relative_to(Path(directory)):
                    raise PermissionError('OFFLINE_LAB_EXTERNAL_DATABASE_DENIED')
        sys.addaudithook(boundary)
        logging.basicConfig(stream=sys.stderr, level=logging.ERROR)
        from research_lab.simulator import compare
        result = compare(bundle)
        json.dump(result, sys.stdout, allow_nan=False, ensure_ascii=False,
                  default=lambda value:value.isoformat())


if __name__ == '__main__':
    main()
