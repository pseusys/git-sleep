"""Entry point for ``python -m git_sleep``."""

from sys import exit as sys_exit

from git_sleep.cli import main

sys_exit(main())
