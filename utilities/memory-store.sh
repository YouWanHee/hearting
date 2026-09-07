#!/usr/bin/env sh
# Read-only R0-R5 resolver; Python consumers use tools/memory/store_resolve.py.
# Uses the core path tools stat and realpath, never Python on hook hot paths.
set -u

_mem_die() {
  printf 'memory store resolution error: %s\n' "$1" >&2
  exit 3
}

# Unlike test -e/-f, stat lets us distinguish absence from EACCES/ELOOP.
# Normalize tool diagnostics to the candidate path; never expose tool stderr.
_mem_probe() {
  if _mem_kind=$(LC_ALL=C stat -L -c '%F' -- "$1" 2>&1); then
    case $_mem_kind in
      'regular file'|'regular empty file') _mem_kind=file ;;
      directory) _mem_kind=directory ;;
      *) _mem_kind=other ;;
    esac
  else
    case $_mem_kind in
      *': No such file or directory'|*': Not a directory') _mem_kind=missing ;;
      *) _mem_die "$2" ;;
    esac
  fi
}

if [ -n "${MEM_STORE:-}" ]; then
  printf '%s\n' "$MEM_STORE"
  exit 0
fi
_mem_home=$HOME
_mem_xdg=${XDG_DATA_HOME:-$_mem_home/.local/share}

# Prefix marks the one R5-excluded candidate. Each complete path is a quoted
# argument: no delimiter, whitespace, newline, glob or eval parsing of paths.
set --
[ -z "${AGENT_HOME:-}" ] || set -- "$@" "0:$AGENT_HOME/memory"
[ -z "${CLAUDE_HOME:-}" ] || set -- "$@" "0:$CLAUDE_HOME/memory"
set -- "$@" "0:$_mem_home/hearting/memory" \
  "0:$_mem_home/agent_setting/memory" "0:$_mem_home/.claude/memory" \
  "1:$_mem_xdg/hearting/current/memory" "0:$_mem_xdg/hearting/memory"

_mem_count=0
_mem_list=
_mem_selected=
# At most seven candidates. Separate variables preserve arbitrary path bytes
# without encoding a list in a string or creating temporary state.
_mem_id1= _mem_id2= _mem_id3= _mem_id4= _mem_id5= _mem_id6= _mem_id7=
for _mem_entry in "$@"; do
  _mem_candidate=${_mem_entry#*:}
  _mem_db=$_mem_candidate/memory.db
  _mem_probe "$_mem_db" "$_mem_candidate"
  [ "$_mem_kind" = file ] || continue
  # Resolve the database file itself, including a file symlink, not just its
  # parent directory. The suffix retains any trailing newlines in its name.
  if _mem_identity=$(realpath -e -- "$_mem_db" 2>/dev/null && printf '.'); then
    _mem_identity=${_mem_identity%.}
    _mem_identity=${_mem_identity%"
"}
  else
    _mem_die "$_mem_candidate"
  fi
  _mem_duplicate=0
  for _mem_seen in "$_mem_id1" "$_mem_id2" "$_mem_id3" "$_mem_id4" \
    "$_mem_id5" "$_mem_id6" "$_mem_id7"; do
    [ "$_mem_seen" != "$_mem_identity" ] || _mem_duplicate=1
  done
  [ "$_mem_duplicate" = 0 ] || continue
  _mem_count=$((_mem_count + 1))
  case $_mem_count in
    1) _mem_id1=$_mem_identity; _mem_selected=$_mem_candidate; _mem_list=$_mem_candidate ;;
    2) _mem_id2=$_mem_identity ;;
    3) _mem_id3=$_mem_identity ;;
    4) _mem_id4=$_mem_identity ;;
    5) _mem_id5=$_mem_identity ;;
    6) _mem_id6=$_mem_identity ;;
    7) _mem_id7=$_mem_identity ;;
  esac
  [ "$_mem_count" = 1 ] || _mem_list="$_mem_list, $_mem_candidate"
done

if [ "$_mem_count" -gt 1 ]; then
  printf 'memory store resolution error: multiple memory databases found: %s; set MEM_STORE to one of them\n' \
    "$_mem_list" >&2
  exit 3
fi
if [ "$_mem_count" = 1 ]; then
  printf '%s\n' "$_mem_selected"
  exit 0
fi
for _mem_entry in "$@"; do
  case $_mem_entry in 1:*) continue ;; esac
  _mem_candidate=${_mem_entry#*:}
  _mem_probe "$_mem_candidate" "$_mem_candidate"
  if [ "$_mem_kind" = directory ]; then
    printf '%s\n' "$_mem_candidate"
    exit 0
  fi
done
printf '%s\n' "$_mem_xdg/hearting/memory"
