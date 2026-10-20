#!/bin/bash
# capability-grant-apply.sh — THE ONE PRIVILEGED ACT. Installs and withdraws the narrow sudoers
# entries the capability wizard queues. Everything else runs as the user.
#
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  ★★★ PRIV-2, FIXED HERE 2026-10-12 — THE ENTRY USED TO NAME A SCRIPT THE GRANTEE COULD REWRITE.
#
#  THE CHARGE: the wizard rendered `your-user ALL=(root) NOPASSWD: <repo path>`, and every such script is
#  your-user:your-user mode 775 in a your-user-owned directory. Editing the body and running it as root was a path to
#  root. The operator gained nothing (he already has password-sudo); the AGENT gained everything — an
#  agent running as your-user reached root with no password. The agent is the threat model, so the boundary
#  the capability system claimed to draw was not drawn against the one thing it exists to constrain.
#
#  ★ TWO THINGS THAT DO NOT FIX IT, both measured on this box before anything was written:
#    · chmod — the grantee OWNS the file; clearing group/other write leaves the owner bit.
#    · chown root in place — `bin/` is drwxrwxr-x your-user:your-user and UNLINK IS GOVERNED BY THE DIRECTORY, so
#      a root-owned file inside a directory your-user can write is a root-owned file your-user can replace.
#    Only one thing closes it: the bytes that run as root must live where the grantee cannot write.
#
#  ★★★ SO THIS APPLIER NOW DOES WHAT THE ESTATE ALREADY DOES ELSEWHERE, CLONED NOT RECONSTRUCTED:
#  `/usr/local/lib/khaoss/` is root:root drwxr-xr-x and already holds root-owned 0755 scripts
#  (access-grant, maintain-zram, read-ollama-env, access-expire). Install the SOURCE there root-owned,
#  then name THAT path in the sudoers entry. The repo file becomes the source; installing it is the
#  privileged act. This is the same shape the estate's own reports already use:
#      sudo install -o root -g root -m 0755 <repo source> /usr/local/lib/khaoss/<name>
#
#  ★ AND IT REFUSES. Before installing anything, every queued entry's target is tested for
#  writability by the INVOKING account — the file and its whole directory chain — and an entry that
#  names a writable path is REJECTED with the reason. That refusal covers entries this applier did not
#  render, including hand-written ones, and it is checked in --validate-only too, where it runs
#  WITHOUT root, so the guard is exercisable rather than merely asserted (FA-17, in the one script
#  where being wrong is most expensive).
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# Bootstrap ONCE (operator) — ★ NOTE THE PATH: the applier must itself be root-owned, or the root of
# trust is a file the grantee can rewrite. Install it into CAP_LIB, then grant it:
#   sudo install -d -o root -g root -m 0755 /usr/local/lib/khaoss
#   sudo install -o root -g root -m 0755 ~/boardwalk/bin/capability-grant-apply.sh \
#        /usr/local/lib/khaoss/capability-grant-apply.sh
#   echo 'your-user ALL=(root) NOPASSWD: /usr/local/lib/khaoss/capability-grant-apply.sh' \
#     | sudo tee /etc/sudoers.d/khaoss-grant-apply && sudo chmod 440 /etc/sudoers.d/khaoss-grant-apply
# After that the wizard issues and expires root capabilities without the operator touching sudo again.
#
# SAFETY: every rendered file is validated with `visudo -cf` BEFORE it is installed. A malformed
# /etc/sudoers.d entry breaks sudo for the whole box, so validation is not optional here.
set -uo pipefail
CAP_LIB="/usr/local/lib/khaoss"
# ★ 2026-09-21 FIX: `sudo` sets HOME=/root, so this read /root/.khaoss-privileged and reported
#   "nothing queued" while the wizard's queue sat in the real user's home. Under sudo we must resolve
#   the INVOKING user's home, not root's — SUDO_USER is the operator who ran it.
REAL_USER="${SUDO_USER:-$(id -un)}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
[ -n "$REAL_HOME" ] || REAL_HOME="$HOME"
Q="$REAL_HOME/.khaoss-privileged"
[ -d "$Q" ] || { echo "nothing queued ($Q absent)"; exit 0; }

# ── the target of an entry: the first field after NOPASSWD: ────────────────────────────────────────
target_of() {   # $1 = pending file -> the path this entry confers root on ('' if none)
  sed -n 's/^[^#][^:]*NOPASSWD:[[:space:]]*\([^[:space:]]*\).*/\1/p' "$1" | head -1
}

preamble_of() { # $1 = pending file, $2 = key -> value of "# khaoss-<key> ..." ('' if absent)
  sed -n "s/^#[[:space:]]*khaoss-$2[[:space:]]\{1,\}\(.*\)$/\1/p" "$1" | head -1
}

# Is $1 writable by the invoking account? 0=yes 1=no 2=CANNOT TELL (and 2 is NOT "safe").
writable_by_invoker() {
  local p="$1" uid gid mode
  # ★★★ MECHANICAL, 2026-10-12. The previous version asked the KERNEL as the user
  # (`sudo -n -u`, then `runuser`). Both are subprocesses that can fail for reasons
  # that have nothing to do with writability — an expired sudo timestamp, a
  # restricted sudoers entry, runuser absent — and the function then returned 2
  # ("CANNOT TELL") for every path, which refused all ten grants on a healthy box.
  # ★ THE ANSWER DOES NOT DEPEND ON WHO ASKS, SO DO NOT ASK: read the mode and compare
  # it against the account's uid and its REAL group list.
  uid=$(stat -c '%u' "$p" 2>/dev/null) || return 2
  gid=$(stat -c '%g' "$p" 2>/dev/null) || return 2
  mode=$(stat -c '%a' "$p" 2>/dev/null) || return 2
  [ -n "$uid" ] && [ -n "$gid" ] && [ -n "$mode" ] || return 2
  local tu tg
  tu=$(id -u "$REAL_USER" 2>/dev/null) || return 2
  # owner-write
  if [ "$uid" = "$tu" ]; then
    case "$mode" in ?[2367]????|??[2367]???|???[2367]??) return 0;; esac
    # owner digit is the first of the 3-4 digit octal
    local m3="${mode: -3}"
    case "${m3:0:1}" in 2|3|6|7) return 0;; esac
    return 1
  fi
  # group-write: is the file's group one of the account's groups?
  if id -G "$REAL_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$gid"; then
    local m3="${mode: -3}"
    case "${m3:1:1}" in 2|3|6|7) return 0;; esac
    return 1
  fi
  # other-write
  local m3="${mode: -3}"
  case "${m3:2:1}" in 2|3|6|7) return 0;; esac
  return 1
}

# How is $1 unfit to hold bytes that run as root? Prints one reason per line; empty output = safe.
chain_problems() {
  local t="$1" d w nd
  if [ -e "$t" ]; then
    writable_by_invoker "$t"; w=$?
    [ "$w" = "0" ] && echo "the file $t is writable by $REAL_USER"
    [ "$w" = "2" ] && echo "CANNOT DETERMINE whether the file $t is writable by $REAL_USER"
  fi
  d=$(dirname "$t")
  while :; do
    writable_by_invoker "$d"; w=$?
    [ "$w" = "0" ] && echo "the directory $d is writable by $REAL_USER -- a root-owned file inside it can be unlinked and replaced"
    [ "$w" = "2" ] && echo "CANNOT DETERMINE whether $d is writable by $REAL_USER"
    nd=$(dirname "$d"); [ "$nd" = "$d" ] && break; d="$nd"
  done
}

# One place that decides whether an entry may be installed, so --validate-only and the root run can
# never disagree. Prints the reasons; returns 1 if the entry must be refused.
refuse_entry() {   # $1 = pending file  -> 0 = acceptable, 1 = REFUSE (reasons on stdout)
  local p="$1" tgt probs pin src
  tgt=$(target_of "$p")
  if [ -z "$tgt" ]; then
    echo "names NO target after NOPASSWD: -- refusing rather than guessing what it grants"
    return 1
  fi
  if [ ! -e "$tgt" ] && [ ! -e "$(dirname "$tgt")" ]; then
    echo "names $tgt, and $(dirname "$tgt") does not exist -- the entry would confer root on a path that is not there"
    return 1
  fi
  probs=$(chain_problems "$tgt")
  if [ -n "$probs" ]; then
    echo "confers root on a script the invoking account can REWRITE ($REAL_USER):"
    echo "$probs" | sed 's/^/       . /'
    echo "     a NOPASSWD entry naming a writable path is a PASSWORDLESS ROUTE TO ROOT: rewrite the body, run it as root."
    return 1
  fi
  src=$(preamble_of "$p" source)
  pin=$(preamble_of "$p" sha256)
  if [ -n "$src" ] && [ -n "$pin" ]; then
    if [ ! -f "$src" ]; then
      echo "declares source $src, which does not exist -- refusing to install a pin for a file I cannot read"
      return 1
    fi
    if [ "$(sha256sum "$src" | cut -d' ' -f1)" != "$pin" ]; then
      echo "the SOURCE CHANGED after this grant was rendered ($src no longer matches the pinned sha256)."
      echo "     re-issue the grant: capability-grant.py issue <capability>. Refusing to install bytes nobody reviewed."
      return 1
    fi
  fi
  return 0
}

# ★ 2026-09-21 — `--validate-only`: run the SAME checks over the queue WITHOUT root and WITHOUT
#   installing. Two reasons, and the second is the real one:
#     (1) the operator can see exactly what would be installed before granting root;
#     (2) ★ WITHOUT IT THE SAFETY MECHANISM WAS UNTESTABLE. "It refuses a malformed file before it can
#         break sudo for the whole box" sat behind the root check, so it was asserted, never exercised —
#         FA-17 again, in the one script where being wrong is most expensive.
#   ★ 2026-10-12: the TARGET check joined it for the same reason, and it is the check that matters for
#     PRIV-2 — a guard against a passwordless root path that can only run as root is a guard nobody
#     can test.
VALIDATE_ONLY=0
[ "${1:-}" = "--validate-only" ] && VALIDATE_ONLY=1

if [ "$VALIDATE_ONLY" = "1" ]; then
  n=0; bad=0
  for p in "$Q"/*.pending; do
    [ -e "$p" ] || continue
    case "$p" in *revoke.pending) continue ;; esac
    name=$(basename "$p" .pending); n=$((n+1))
    tgt=$(target_of "$p")
    if ! visudo -cf "$p" >/dev/null 2>&1; then
      echo "  ✗ REJECT  $name  (fails visudo; would NOT be installed and sudo is left intact)"
      bad=$((bad+1)); continue
    fi
    reasons=$(refuse_entry "$p")
    if [ -n "$reasons" ]; then
      echo "  ✗ REJECT  $name  (visudo OK, but the grant is not safe to install)"
      echo "$reasons" | sed 's/^/       /'
      bad=$((bad+1)); continue
    fi
    echo "  ✓ ACCEPT  $name  (target $tgt is not writable by $REAL_USER)"
  done
  if [ -e "$Q/revoke.pending" ]; then
    echo "  · withdrawals queued: $(sort -u "$Q/revoke.pending" | grep -c . )"
  fi
  echo "  checked $n install(s), $bad rejected. Nothing was installed (validate-only)."
  [ "$bad" = "0" ] && exit 0 || exit 1
fi
[ "$(id -u)" = "0" ] || { echo "REFUSED: run with sudo."; exit 1; }

applied=0; removed=0; refused=0

# 1 — withdrawals first (fail safe: lose privilege before gaining it)
if [ -s "$Q/revoke.pending" ]; then
  while read -r f; do
    [ -n "$f" ] || continue
    if [ -e "$f" ]; then rm -f "$f" && echo "  REMOVED $f" && removed=$((removed+1)); fi
  done < <(sort -u "$Q/revoke.pending")
  : > "$Q/revoke.pending"
fi

# 2 — installs. Order matters and is the fix: the ROOT-OWNED COPY IS INSTALLED FIRST, and only then is
#     an entry naming it written. An entry is never installed before the bytes it names exist and are
#     root-owned, so there is no window in which the entry names something the grantee could swap.
for p in "$Q"/*.pending; do
  [ -e "$p" ] || continue
  case "$p" in *revoke.pending) continue ;; esac
  name=$(basename "$p" .pending)
  dest="/etc/sudoers.d/$name"

  # (a) THE REFUSAL, before anything is written anywhere. Same function --validate-only uses.
  reasons=$(refuse_entry "$p")
  if [ -n "$reasons" ]; then
    echo "  ✗ REFUSED: $name — NOT installed (and sudo is left intact)."
    echo "$reasons" | sed 's/^/       /'
    refused=$((refused+1)); continue
  fi

  # (b) INSTALL THE ROOT-OWNED COPY, and verify the bytes that landed are the bytes we hashed.
  tgt=$(target_of "$p")
  src=$(preamble_of "$p" source)
  if [ -n "$src" ] && [ "$tgt" != "$src" ]; then
    install -d -o root -g root -m 0755 "$(dirname "$tgt")" || {
      echo "  ✗ REFUSED: $name — could not create $(dirname "$tgt")"; refused=$((refused+1)); continue; }
    if ! install -o root -g root -m 0755 "$src" "$tgt"; then
      echo "  ✗ REFUSED: $name — could not install $src -> $tgt"; refused=$((refused+1)); continue
    fi
    a=$(sha256sum "$src" | cut -d' ' -f1); b=$(sha256sum "$tgt" | cut -d' ' -f1)
    if [ "$a" != "$b" ]; then
      echo "  ✗ REFUSED: $name — the installed copy does not match the source ($a vs $b). Nothing installed."
      refused=$((refused+1)); continue
    fi
    echo "  ✓ INSTALLED $tgt  (root:root 0755, sha256 ${a:0:16}...)"
  fi

  # (c) THE SUDOERS ENTRY, validated, then installed atomically.
  tmp=$(mktemp /etc/sudoers.d/.staging.XXXXXX)
  cp "$p" "$tmp"
  if ! visudo -cf "$tmp" >/dev/null 2>&1; then
    echo "  ✗ REFUSED: $name fails visudo validation — NOT installed (and sudo is left intact)."
    rm -f "$tmp"; refused=$((refused+1)); continue
  fi
  chmod 440 "$tmp"; chown root:root "$tmp"
  mv "$tmp" "$dest"
  rm -f "$p"
  echo "  ✓ INSTALLED $dest"; applied=$((applied+1))
done

echo "  applied=$applied removed=$removed refused=$refused"
echo "  ledger:  ~/boardwalk/bin/capability-grant.py status"
