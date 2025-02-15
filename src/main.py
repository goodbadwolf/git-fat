# src/main.py
#!/usr/bin/env python3
import sys
import logging
from . import git_utils
from . import utils

logger = logging.getLogger(__name__)


def setup_logging():
    log_level = logging.DEBUG if utils.is_verbose() else logging.INFO
    logging.basicConfig(stream=sys.stdout, level=log_level, format="%(message)s")


def main():
    setup_logging()
    fat = git_utils.GitFat()

    if len(sys.argv) <= 1:
        logger.info(
            "Usage: git fat [init|status|push|pull|gc|verify|checkout|find|index-filter]"
        )
        sys.exit(1)

    cmd = sys.argv[1]
    cmd_args = sys.argv[2:]

    commands = {
        "filter-clean": fat.cmd_filter_clean,
        "filter-smudge": fat.cmd_filter_smudge,
        "init": fat.cmd_init,
        "status": lambda: fat.cmd_status(cmd_args),
        "push": lambda: fat.cmd_push(cmd_args),
        "pull": lambda: fat.cmd_pull(cmd_args),
        "gc": fat.cmd_gc,
        "verify": fat.cmd_verify,
        "checkout": lambda: fat.cmd_checkout(cmd_args),
        "find": lambda: fat.cmd_find(cmd_args),
        "index-filter": lambda: fat.cmd_index_filter(cmd_args),
    }

    if cmd not in commands:
        logger.info("Unknown command: %s", cmd)
        logger.info(
            "Usage: git fat [init|status|push|pull|gc|verify|checkout|find|index-filter]"
        )
        sys.exit(1)

    commands[cmd]()


if __name__ == "__main__":
    main()
