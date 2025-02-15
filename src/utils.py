import sys
import os
import subprocess
import logging

logger = logging.getLogger(__name__)


def is_verbose():
    """Check if verbose logging is enabled"""
    return os.environ.get("GIT_FAT_VERBOSE") is not None


def mkdir_p(path):
    """Create directory and parents if they don't exist"""
    os.makedirs(path, exist_ok=True)


def umask():
    """Get umask without changing it"""
    old = os.umask(0)
    os.umask(old)
    return old


def get_git_root():
    """Get git repository root directory"""
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "--show-toplevel"])
            .strip()
            .decode("utf-8")
        )
    except subprocess.CalledProcessError:
        logger.error("Not in a git repository")
        sys.exit(1)


def get_git_dir():
    """Get .git directory location"""
    return (
        subprocess.check_output(["git", "rev-parse", "--git-dir"])
        .strip()
        .decode("utf-8")
    )


def gitconfig_get(name, file=None):
    """Get git config value"""
    args = ["git", "config", "--get"]
    if file is not None:
        args += ["--file", file]
    args.append(name)

    try:
        output = subprocess.check_output(args)
        return output.strip().decode("utf-8")
    except subprocess.CalledProcessError:
        if file is None:
            return None
        return gitconfig_get(name)


def gitconfig_set(name, value, file=None):
    """Set git config value"""
    args = ["git", "config"]
    if file is not None:
        args += ["--file", file]
    args += [name, value]
    subprocess.check_call(args)
