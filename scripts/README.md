# scripts

The smoke tests (#3) and the end-to-end script (Danny, #9), which runs the demo path on the GB10 and prints pass or fail for each step.

The gates live here too: `validate_repo.py` and `scan_secrets.py` (run by `make check`), and `check_pr_issue.py`, which CI runs on every pull request to make sure its description names its issue.
