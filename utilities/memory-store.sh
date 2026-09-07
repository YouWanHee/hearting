#!/usr/bin/env sh
# Read-only POSIX realization of tools/memory/store_resolve.py, R0-R5.
# Paths are compared without opening databases or granting creation authority.
set -u
set -f

_mem_store_die() {
  printf 'memory store resolution error: %s\n' "$1" >&2
  exit 3
}

# R0: nonempty caller overrides are returned without probing.
if [ -n "${MEM_STORE:-}" ]; then
  printf '%s\n' "$MEM_STORE"
  exit 0
fi

# Resolve all components, including the final DB-file symlink. Boolean test
# probes alone hide ELOOP/EACCES as absence, so walk searchable parents and
# follow links explicitly. Only ENOENT and ENOTDIR count as absence. No path
# lists are serialized through delimiters, eval, or unquoted expansions.
# Outputs: _mem_kind = missing|file|directory|other and _mem_identity.
_mem_probe() {
  _mem_kind=missing
  _mem_identity=
  _mem_pending=$1
  case $_mem_pending in
    /*) ;;
    *)
      _mem_cwd=$(pwd -P && printf '.') || _mem_store_die "$_mem_cand"
      _mem_cwd=${_mem_cwd%.}; _mem_cwd=${_mem_cwd%?}
      _mem_pending=$_mem_cwd/$_mem_pending
      ;;
  esac
  _mem_resolved=/
  _mem_links=0
  _mem_requires_directory=0
  while [ -n "$_mem_pending" ]; do
    case $_mem_pending in
      */*) _mem_part=${_mem_pending%%/*}; _mem_pending=${_mem_pending#*/} ;;
      *) _mem_part=$_mem_pending; _mem_pending= ;;
    esac
    [ -d "$_mem_resolved" ] || return 0
    [ -x "$_mem_resolved" ] || _mem_store_die "$_mem_cand"
    case $_mem_part in
      ''|.) continue ;;
      ..) _mem_resolved=${_mem_resolved%/*}; _mem_resolved=${_mem_resolved:-/}; continue ;;
    esac
    # The preceding component must be a searchable directory before a child
    # can be probed. A regular-file parent is the R2 ENOTDIR wrong-type case.
    _mem_next=${_mem_resolved%/}/$_mem_part
    if [ -L "$_mem_next" ]; then
      _mem_links=$((_mem_links + 1))
      [ "$_mem_links" -le 40 ] || _mem_store_die "$_mem_cand"
      # The sentinel preserves any newlines at the end of a link target.
      _mem_target=$(readlink -- "$_mem_next" 2>/dev/null && printf '.') || _mem_store_die "$_mem_cand"
      _mem_target=${_mem_target%.}; _mem_target=${_mem_target%?}
      if [ -z "$_mem_pending" ]; then
        case $_mem_target in */) _mem_requires_directory=1 ;; esac
      fi
      case $_mem_target in /*) _mem_resolved=/ ;; esac
      _mem_pending=$_mem_target${_mem_pending:+/$_mem_pending}
      continue
    fi
    [ -e "$_mem_next" ] || return 0
    _mem_resolved=$_mem_next
  done
  if [ "$_mem_requires_directory" -eq 1 ] && [ ! -d "$_mem_resolved" ]; then return 0; fi
  _mem_identity=$_mem_resolved
  if [ -f "$_mem_resolved" ]; then _mem_kind=file
  elif [ -d "$_mem_resolved" ]; then _mem_kind=directory
  else _mem_kind=other
  fi
}

_mem_home=$HOME
_mem_xdg_data=${XDG_DATA_HOME:-$_mem_home/.local/share}
# The first character is the managed-current marker; the rest is a quoted path.
set --
[ -z "${AGENT_HOME:-}" ] || set -- "$@" "0$AGENT_HOME/memory"
[ -z "${CLAUDE_HOME:-}" ] || set -- "$@" "0$CLAUDE_HOME/memory"
set -- "$@" "0$_mem_home/hearting/memory" "0$_mem_home/agent_setting/memory" \
  "0$_mem_home/.claude/memory" "1$_mem_xdg_data/hearting/current/memory" \
  "0$_mem_xdg_data/hearting/memory"

_mem_first_pass=1
_mem_first_original=
_mem_fallback=
_mem_conflict_list=
# The for-list is expanded once. During the loop, positional parameters hold
# only already-seen canonical DB paths, also individually quoted.
for _mem_entry in "$@"; do
  if [ "$_mem_first_pass" -eq 1 ]; then set --; _mem_first_pass=0; fi
  _mem_cand=${_mem_entry#?}
  _mem_probe "$_mem_cand/memory.db"
  if [ "$_mem_kind" = file ]; then
    _mem_duplicate=0
    for _mem_seen in "$@"; do
      if [ "$_mem_seen" = "$_mem_identity" ]; then _mem_duplicate=1; break; fi
    done
    if [ "$_mem_duplicate" -eq 0 ]; then
      set -- "$@" "$_mem_identity"
      [ -n "$_mem_first_original" ] || _mem_first_original=$_mem_cand
      _mem_conflict_list=$_mem_conflict_list${_mem_conflict_list:+, }$_mem_cand
    fi
  fi
  if [ -z "$_mem_fallback" ] && [ "${_mem_entry%"$_mem_cand"}" = 0 ]; then
    _mem_probe "$_mem_cand"
    [ "$_mem_kind" != directory ] || _mem_fallback=$_mem_cand
  fi
done

if [ "$#" -ge 2 ]; then
  printf 'memory store resolution error: multiple memory databases found: %s; set MEM_STORE to one of them\n' \
    "$_mem_conflict_list" >&2
  exit 3
fi
if [ "$#" -eq 1 ]; then
  printf '%s\n' "$_mem_first_original"
else
  printf '%s\n' "${_mem_fallback:-$_mem_xdg_data/hearting/memory}"
fi
