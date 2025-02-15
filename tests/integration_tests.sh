#!/bin/bash -e

PROJ_DIR=$(git rev-parse --show-toplevel 2>/dev/null || dirname "$(readlink -f "$0")")

# shellcheck source=/dev/null
source "${PROJ_DIR}/modules/trap-failure/failure.sh"
set -E -o functrace
trap 'failure "LINENO" "BASH_LINENO" "${BASH_COMMAND}" "${?}"' ERR

NO_CLEANUP=false
VERBOSE=false
WORK_DIR="tests_workdir"

RED="\033[1;31m"
GREEN="\033[1;32m"
YELLOW="\033[1;33m"
MAGENTA="\033[1;35m"
CYAN="\033[1;36m"
ENDCOLOR="\033[0m"

SUCCESS_CLR=$GREEN
NORMAL_CLR=$ENDCOLOR
NO_CLEANUP_CLR=$MAGENTA
INFO_CLR=$CYAN
WARNING_CLR=$YELLOW
ERROR_CLR=$RED

log() {
    local colorVar="$1"
    local message="$2"
    echo -e "${colorVar}${message}${ENDCOLOR}"
}
log_s() {
    log "$SUCCESS_CLR" "$1"
}

log_n() {
    log "$NORMAL_CLR" "$1"
}

log_d() {
    log "$NO_CLEANUP_CLR" "$1"
}

log_i() {
    log "$INFO_CLR" "$1"
}
log_w() {
    log "$WARNING_CLR" "$1"
}
log_e() {
    log "$ERROR_CLR" "$1"
}

usage() {
    log_n "Usage: $0 [-n] [-v] [-h]"
    exit 1
}

while getopts "nvh" opt; do
    case "$opt" in
    n)
        NO_CLEANUP=true
        ;;
    v)
        VERBOSE=true
        ;;
    h)
        usage
        ;;
    *)
        usage
        ;;
    esac
done
shift $((OPTIND - 1))

[ "$VERBOSE" = true ] && set -x

setup() {
    log_i "Setting up..."

    WORK_DIR=$(mktemp -d -t git-fat-tests-XXXXXX)
    log_i "Working directory: $WORK_DIR"
}

cleanup() {
    if [ "$NO_CLEANUP" = true ]; then
        log_i "No clean up for $WORK_DIR..."
        return 0
    fi

    log_i "Cleaning up..."
    if [ -d "$WORK_DIR" ]; then
        log_i "Deleting working directory $WORK_DIR"
        rm -rf "$WORK_DIR"
    fi
}

change_working_dir() {
    if ! cd "$1"; then
        log_e "Failed to change working directory to $1"
        exit 1
    fi
    log_i "Changed working directory to $1"
}

successCount=0
failureCount=0
run_test() {
    local testName="$1"
    local bufferTestStreams="${2:-true}"
    local outFile
    local errFile
    outFile="$(mktemp)"
    errFile="$(mktemp)"
    log_d "Running test: $testName"
    if $bufferTestStreams; then
        if "$testName" >"$outFile" 2>"$errFile"; then
            log_s "$testName: succeess"
            successCount=$((successCount + 1))
        else
            failureCount=$((failureCount + 1))
            log_e "$testName: failure"
            log_w "STDOUT:[[ -------- "
            cat "$outFile"
            log_w "-------- ]]"
            log_e "STDERR:[[ -------- "
            cat "$errFile"
            log_e "-------- ]]"
        fi
    else
        if "$testName"; then
            log_s "$testName: success"
            successCount=$((successCount + 1))
        else
            failureCount=$((failureCount + 1))
            log_e "$testName: failure"
        fi
    fi
    rm -f "$outFile" "$errFile"
}

test_setup_repos() {
    change_working_dir "$WORK_DIR"
    git init fat-test || return 1
    cd fat-test
    git fat init || return 1
    cat - >>.gitfat <<EOF
[rsync]
remote = localhost:/tmp/fat-store
EOF
    echo '*.fat filter=fat -crlf' >.gitattributes
    git add .gitattributes .gitfat || return 1
    git commit -m 'Initial fat repository' || return 1
    ln -s /oe/dss-oe/dss-add-ons-testing-build/deploy/licenses/common-licenses/GPL-3 c
    git add c || return 1
    git commit -m 'add broken symlink' || return 1
    echo 'fat content a' >a.fat
    git add a.fat || return 1
    git commit -m 'add a.fat' || return 1
    echo 'fat content b' >b.fat
    git add b.fat || return 1
    git commit -m 'add b.fat' || return 1
    echo 'revise fat content a' >a.fat
    git commit -am 'revise a.fat' || return 1
    git fat push || return 1
}

test_uninitialized_checkout_pull() {
    change_working_dir "$WORK_DIR"
    git clone fat-test fat-test2 || return 1
    cd fat-test2
    if git fat checkout; then
        log_e "ERROR: \"git fat checkout\" in uninitialised repo should fail"
        return 1
    fi
    if git fat pull -- 'a.fa*'; then
        log_e "ERROR: \"git fat pull\" in uninitialised repo should fail"
        return 1
    fi
}

test_init_and_pull() {
    change_working_dir "$WORK_DIR"
    cd fat-test2
    git fat init || return 1
    git fat pull -- 'a.fa*' || return 1
    cat a.fat
    echo 'file which is committed and removed afterwards' >d
    git add d || return 1
    git commit -m 'add d with normal content' || return 1
    rm d
    git fat pull || return 1
}

test_verify_command() {
    change_working_dir "$WORK_DIR"
    cd fat-test2
    modify_object="b93926aa7599db4380ba8af7773db7c1404082ed"
    chmod a+w .git/fat/objects/$modify_object
    mv .git/fat/objects/$modify_object \
        .git/fat/objects/$modify_object.bak
    echo "Not the right data" >.git/fat/objects/$modify_object
    git fat verify
    if [ $? -eq 0 ]; then # It should have failed with non-zero status
        log_e "Verify did not detect invalid object"
        return 1
    fi
    mv .git/fat/objects/$modify_object.bak \
        .git/fat/objects/$modify_object
}

log_d "Starting tests with NO_CLEANUP=$NO_CLEANUP, VERBOSE=$VERBOSE"

setup
trap cleanup EXIT

run_test test_setup_repos
run_test test_uninitialized_checkout_pull
run_test test_init_and_pull
run_test test_verify_command

log_i "Tests complete: $successCount successes, $failureCount failures"
