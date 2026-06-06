"""
Master ingestion script. Run from the backend/ directory:

    python -m ingestion.run_all

For ESPN private league auth, set env vars:
    ESPN_S2=<cookie>  SWID=<cookie>
"""

import os
import sys

sys.path.insert(0, str(__file__).rsplit("/ingestion", 1)[0])

from db.database import init_db
from ingestion.sleeper import run_ingestion as sleeper_ingest
from ingestion.espn import run_ingestion as espn_ingest


def main():
    init_db()

    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")

    print("\n" + "=" * 50)
    print("ESPN Ingestion")
    print("=" * 50)
    espn_ingest(espn_s2=espn_s2, swid=swid)

    print("\n" + "=" * 50)
    print("Sleeper Ingestion")
    print("=" * 50)
    sleeper_ingest()


if __name__ == "__main__":
    main()
