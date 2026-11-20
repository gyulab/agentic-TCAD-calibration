"""Entry point of a detached job process: python -m tcadcal.jobrunner <workspace> <job_id>."""
import sys

from .jobs import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
