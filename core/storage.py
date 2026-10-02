"""Shared state location for controller, watcher and research child."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

def data_directory():
    explicit = os.getenv('JALWE_CONTROLLER_DATA_DIR', '').strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    railway = Path('/app/data')
    legacy = BASE_DIR / 'data'
    if railway.is_dir() and os.access(railway, os.W_OK):
        if railway.resolve() != legacy.resolve() and (legacy / 'jalwe_v4.db').exists() and not (railway / 'jalwe_v4.db').exists():
            raise RuntimeError('Existing JALWE database requires an explicit storage migration; refusing an empty database.')
        return railway
    return legacy

def database_path():
    explicit = os.getenv('JALWE_DATABASE_PATH', '').strip()
    return explicit or str(data_directory() / 'jalwe_v4.db')
