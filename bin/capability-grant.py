#!/usr/bin/env python3
"""capability-grant.py — the wizard side of TIMED, AUDITED, EXPIRING root grants.

WHY THIS EXISTS: the box already has static sudoers grants (`khaoss-tier1`, `khaoss-tier2`,
`kali-grant-root`, `khaoss-drive`). They are real privilege with **no TTL, no ledger, no provenance,
no revocation**. Meanwhile the capability ledger has exactly those properties but is NOT on any
operational path. So the capability system and the actual privilege are two halves that never met.

This connects them, using the pattern the operator already canonised in `khaoss-access.json`
(`restriction_lifts`: "a restriction BLOCKS by default — that is canon; this ledger holds only the
EXCEPTIONS, timed"). Root is now the same shape:

    root BLOCKS by default. A grant is a narrow sudoers entry scoped to ONE script plus its allowed
    arguments, with a TTL, recorded in the ONE store with provenance. Expiry is MECHANICAL —
    `sweep` removes the sudoers file whether or not anyone polled it, so an elevation cannot outlive
    its grant even unobserved (the ledger's own ttl property, now actually wired).

TWO HALVES, deliberately:
  * this tool RUNS AS THE USER — it edits the ledger and renders sudoers CONTENT. No root needed.
  * `capability-grant-apply.sh` RUNS AS ROOT — the only privileged thing, and the single command the
    operator bootstraps ONCE. After that the wizard issues and expires grants without him touching sudo.

Usage: capability-grant.py {status|propose|issue|sweep|revoke|render}
Prove-fail: CAPGRANT_LIE=expired|future|notime
"""
import json, os, sys, datetime, hashlib, tempfile, getpass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCESS = os.path.expanduser(os.environ.get("KHAOSS_ACCESS") or "~/.config/boardwalk/khaoss-access.json")
os.makedirs(os.path.dirname(ACCESS), exist_ok=True)

# ★★★ 2026-09-21 INCIDENT AND FIX. The lie-injection fixtures below build a SYNTHETIC cfg and hand it
#   to cmd_sweep(), which calls save(). That wrote the fixture over the REAL registry: it went from
#   ~16 KB carrying surfaces/agents/_shaman/restriction_lifts/struck to 49 bytes holding only
#   {"privileged_operations": {"grants": {}}}. Restored from backup and recorded as
#   `khaoss-access.json.BROKEN-BY-LIEMODE-20260921`.
#   THE RULE THAT CAME OUT OF IT: a test fixture must never be able to reach a real store. When the
#   lie flag is set, the store path is redirected to a throwaway file, so an injecting run physically
#   cannot write the registry even if a future edit reintroduces save() on the fixture path.
_LIE = os.environ.get("CAPGRANT_LIE")
if _LIE:
    ACCESS = os.path.join(tempfile.gettempdir(), "khaoss-access.LIEMODE-throwaway.json")
    if not os.path.exists(ACCESS):
        with open(ACCESS, "w") as _f:
            _f.write('{"privileged_operations": {"grants": {}}}')
SUDOERS_DIR = "/etc/sudoers.d"
NOW = datetime.datetime.now(datetime.timezone.utc)

# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  ★★★ PRIV-2 — THE PRIVILEGE MODEL WAS A NAMING SCHEME, NOT A BOUNDARY (2026-10-12, CRITICAL).
#
#  THE CHARGE: the sudoers line rendered below names `{spec['script']}` — a path under
#  `~/bin/`, owned <user>:<user>, mode 775. Measured on this box:
#      bin/cloudflare-tunnel-service.sh    <user>:<user>  -rwxrwxr-x
#      bin/capability-grant-apply.sh       <user>:<user>  -rwxrwxr-x
#      bin/lid-ignore-install.sh           <user>:<user>  -rwxrwxr-x
#      bin/aide-check-fix.sh               <user>:<user>  -rwxrwxr-x
#  So `NOPASSWD:` on those paths was decoration: the account that may run them WITHOUT A PASSWORD
#  could rewrite them first. Editing the body and running it as root is a path to root.
#
#  ★ THE SEVERITY, STATED PRECISELY — NEITHER OVERSTATED NOR UNDERSTATED:
#    · THE OPERATOR GAINS NOTHING. <user> is in the `sudo` group with password-sudo; he already has root
#      whenever he wants it. Nothing about his position changed.
#    · THE AGENT GAINS EVERYTHING. An agent running as <user> reaches root with NO PASSWORD by editing a
#      file it can write. THE AGENT IS THE THREAT MODEL, so the boundary this system claimed to draw
#      was not drawn against the only thing it exists to constrain.
#    · AND THE CATALOGUE'S OWN SENTENCE WAS TRUE OF THE LINE AND FALSE OF THE TARGET: "one script, no
#      arguments, no shell, no wildcard" describes the sudoers entry exactly, and is worth nothing if
#      the script it names is editable by the grantee.
#
#  ★★ THE FIX, AND WHY IT IS SHAPED THIS WAY: the sudoers entry must name a copy that the invoking
#  account CANNOT WRITE. Two sub-points decide the design, and both are measured, not assumed:
#
#    (1) CHMOD CANNOT FIX THIS. The scripts are owner-writable because <user> OWNS them; clearing the
#        group and other write bits leaves the owner bit and changes nothing. The account that owns
#        a file can always write it.
#    (2) ROOT-OWNING THE FILE IN PLACE CANNOT FIX THIS EITHER, and this is the trap: `bin/` is
#        drwxrwxr-x user:user, and **unlink is governed by the DIRECTORY, not the file**. A root-owned
#        script inside a directory <user> can write is a root-owned script <user> can delete and replace.
#        (Measured: `[ -w ~/bin ]` is TRUE for <user>.) So the trusted bytes must
#        live in a directory <user> cannot write, which is why this file names `CAP_LIB` and not `bin/`.
#
#  ★★★ AND THE CLONE, NOT A RECONSTRUCTION: `/usr/local/lib/khaoss/` ALREADY EXISTS on this box,
#  root:root, drwxr-xr-x, holding root-owned 0755 scripts (`access-grant`, `maintain-zram`,
#  `read-ollama-env`, `access-expire`). The estate installs them exactly this way and says so in its
#  own reports:
#      sudo install -d -o root -g root -m 0755 /usr/local/lib/khaoss
#      sudo install -o root -g root -m 0755 <repo source> /usr/local/lib/khaoss/<name>
#      printf '<user> ALL=(root) NOPASSWD: /usr/local/lib/khaoss/<name>\n' | sudo tee /etc/sudoers.d/...
#  That working configuration is what this tool now renders. It was not invented here.
#
#  ★ WHAT THE APPLIER MUST THEREFORE DO, and does: copy the repo source to CAP_LIB with
#  `install -o root -g root -m 0755` BEFORE installing the sudoers entry, and REFUSE any queued entry
#  whose target is writable by the invoking account — including entries this file did not render, and
#  including the entries already installed on this box, which name the repo path and stay open until
#  they are re-issued. The renderer here is advice; the applier's check is the enforcement, because
#  the renderer runs as the account being constrained and can be edited by it.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
CAP_LIB = "/usr/local/lib/khaoss"
CAP_LIB_RULE = ("the sudoers entry names a ROOT-OWNED copy under /usr/local/lib/khaoss, not the repo "
                "file; the repo file is the SOURCE, and installing it is the privileged act")


def _invoking_user():
    try:
        return getpass.getuser()
    except Exception:                                        # noqa: BLE001
        return f"uid {os.getuid()}"


def unsafe_chain(path):
    """Every reason `path` is not a safe home for bytes that will run as root. '' when safe.

    *** THE FILE AND EVERY DIRECTORY ABOVE IT. Unlink is governed by the directory, so a root-owned
    file inside a user-writable directory is a root-owned file the user can replace. Checking only
    the file would reproduce the exact reasoning error PRIV-2 is about.

    Answers for the account running THIS process -- which is the account the threat model is about,
    because this tool runs as the user. The root-time enforcement is in the applier, deliberately.
    """
    out = []
    user = _invoking_user()
    p = os.path.abspath(path)
    if os.path.exists(p) and os.access(p, os.W_OK):
        out.append(f"the file {p} exists and is writable by {user}")
    d = p if os.path.isdir(p) else os.path.dirname(p)
    while True:
        if os.access(d, os.W_OK):
            out.append(f"the directory {d} is writable by {user} -- a root-owned file inside it can "
                       f"be unlinked and replaced")
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return out


def installed_path(spec):
    """Where the ROOT-OWNED copy of this capability's script lives. Named by the sudoers entry."""
    return os.path.join(CAP_LIB, os.path.basename(spec["script"]))


def source_sha256(spec):
    """The pin: the bytes as they stand in the repo AT RENDER TIME. Recorded for drift detection --
    NOT as authorisation. The applier hashes what it actually installs; a user-supplied hash that
    authorised root would be a user-supplied way to authorise root."""
    try:
        with open(spec["script"], "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""

# ── the catalogue: a capability is a NAME bound to ONE script and its permitted argument set ──────────
# Adding a capability is a code change reviewed like any other — NOT something a wizard can invent.
CATALOGUE = {
    # ★ THIS IS A RELEASE EXAMPLE, NOT THE OPERATOR'S CATALOGUE. The original catalogue named this
    #   estate's own scripts, services and domains; none of that ships. A capability is a NAME bound
    #   to ONE script and its exact argument set, added by a code change reviewed like any other --
    #   the wizard can never invent one. Replace this entry (or add more) with your own scripts and
    #   review the rendered sudoers line before you grant anything.
    "example.echo": {
        "script": os.path.join(ROOT, "bin", "example-capability.sh"),
        "allowed_args": [],
        "sudoers": "boardwalk-example",
        "why": "A no-op example: prints its argv as the invoking user and exits 0. It changes nothing "
               "on the machine. Use it to see the whole grant flow, then point a real capability at "
               "a script you have reviewed.",
        "default_ttl_minutes": 15,
    },
}

# ★★★ THE CAPABILITIES THAT CONFERS NO PRIVILEGE — 2026-10-12 (SEAT LANE 7).
#   `root` DEFAULTS TO TRUE, so every entry above is unchanged and no existing grant changes meaning.
#   An entry opts OUT explicitly with `"root": False`, and that single key is what stops
#   `sudoers_body()` from rendering a `NOPASSWD:` line for an act that must run as the user.
#   ★ IT IS A KEY ON THE ENTRY AND NOT A SECOND CATALOGUE. One catalogue, one name space, one TTL
#     model, one ledger -- a parallel "unprivileged capabilities" table would be two stores for one
#     fact and the operator would have to know which one to look in.
def confers_root(spec):
    """True when this capability hands out root. Absent key means True -- the safe default for a
    catalogue whose whole purpose is privileged acts, and the reason no existing entry moved."""
    return bool((spec or {}).get("root", True))


def load():
    if not os.path.exists(ACCESS):
        # ★ A FRESH INSTALL HAS NO LEDGER. Return the same shape the live store has, empty, rather
        #   than raising FileNotFoundError before the first `api`/`status` ever runs. Nothing is
        #   granted by it; the first save() writes the file.
        return {"_doc": "BoardWalk release ledger — empty until the first grant is issued.",
                "host": os.uname().nodename, "agents": {}, "struck": [],
                "restriction_lifts": {}, "privileged_operations": {"grants": {}}}
    with open(ACCESS) as f:
        return json.load(f)


def save(cfg):
    tmp = ACCESS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, ACCESS)


def parse_ts(s):
    if not s:
        return None
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def grants(cfg):
    return (cfg.get("privileged_operations") or {}).get("grants") or {}


def expires_at(g):
    """MECHANICAL expiry: derived from granted_at + ttl_minutes, never from a stored flag someone
    has to remember to flip. A grant with no parseable clock is treated as ALREADY EXPIRED — the
    safe direction for privilege."""
    t0 = parse_ts(g.get("granted_at"))
    ttl = g.get("ttl_minutes")
    if t0 is None or not isinstance(ttl, (int, float)):
        return None                      # -> expired (fail closed)
    return t0 + datetime.timedelta(minutes=ttl)


def is_expired(g, now=None):
    now = now or NOW
    exp = expires_at(g)
    return exp is None or exp <= now


def sudoers_body(cap, spec):
    """The narrowest line that still works: exact script path, exact argument list, no wildcards.
    `NOPASSWD:` on a specific script+arg pair is the whole grant — nothing else is conferred.

    ★★★ PRIV-2, FIXED HERE 2026-10-12: THE PATH IS NOW THE ROOT-OWNED INSTALLED COPY, NOT THE REPO
    FILE. This single substitution is the boundary. It used to name `spec['script']` — a <user>:writable
    file under a <user>-owned directory — which made `NOPASSWD:` decoration: the grantee could rewrite
    the body and then run it as root. It now names `/usr/local/lib/khaoss/<name>`, a directory owned
    root:root, de-drwxr-xr-x, that <user> cannot write or unlink from. The applier creates that copy.
    ★ THE SENTENCE BELOW IS STILL TRUE OF THIS LINE, and it is now ALSO true of the thing it names:
    one script, those exact arguments, no shell, no wildcard -- and a script the grantee cannot edit.

    ★★★ A CAPABILITY THAT CONFERS NO PRIVILEGE RENDERS A SENTENCE, NOT A LINE -- 2026-10-12.
    `root: False` entries (`launch.dsh-web`) must never produce a `NOPASSWD:` body, because there is
    no sudoers file for them to be installed into and rendering one would invite the applier to
    install root for an act that has to run as the user. The sentence is returned in the SAME slot so
    `propose` still answers the question it always answered -- "what does this confer?" -- honestly."""
    if not confers_root(spec):
        return ("NOTHING -- this capability confers no privilege. It runs as you, as a user-level act, "
                "and installs no sudoers file.\n")
    # ★ A DROPPED TRAILING SPACE, deliberately: the old body ended "...{script} {args}\n" and rendered
    #   "script \n" when allowed_args was empty. Harmless to sudo, but the applier parses this line to
    #   find the target, and a field-splitting parser should not have to know about a trailing blank.
    return getpass.getuser() + " ALL=(root) NOPASSWD: " + " ".join([installed_path(spec)] + list(spec["allowed_args"])) + "\n"


def _unmanaged_now():
    """khaoss-* sudoers files with no ledger entry behind them: permanent root nobody tracks.
    Read from the DIRECTORY (listable unprivileged); contents need root and are not required."""
    try:
        return sorted(n for n in os.listdir(SUDOERS_DIR) if n.startswith("khaoss"))
    except Exception:
        return []


def cmd_status(cfg):
    gs = grants(cfg)
    # ★ `or ""` BECAUSE A NO-ROOT GRANT'S `sudoers_file` IS `None` -- 2026-10-12. `os.path.basename(None)`
    #   raises TypeError, so one `launch.dsh-web` grant in the ledger would have taken `status` down
    #   entirely and the operator would have lost the read-only view of EVERY grant. A capability that
    #   installs nothing must not be able to break the command that lists what is installed.
    unmanaged = [n for n in _unmanaged_now() if n not in
                 {os.path.basename(g.get("sudoers_file") or "") for g in gs.values()}]
    if not gs:
        print("  no MANAGED privileged-operation grants — ROOT IS BLOCKED BY DEFAULT (that is canon)")
        if unmanaged:
            print(f"  ⚠ but {len(unmanaged)} UNMANAGED khaoss sudoers entr(y/ies) exist with no ledger "
                  f"behind them and no TTL: {', '.join(unmanaged)}")
            print("     Permanent root nobody is tracking. Run `reconcile`.")
        # ★ THE AUDIT RUNS ON THIS PATH TOO. An empty ledger is exactly when unmanaged entries matter
        #   most, and the root-of-trust question does not depend on a ledger row existing.
        _root_of_trust(cfg, gs)
        return 0
    live = exp = 0
    print(f"  {'capability':26} {'expires':22} {'state':9} sudoers")
    for gid, g in sorted(gs.items()):
        e = expires_at(g)
        stale = is_expired(g)
        exp += stale; live += (not stale)
        # ★ THE NO-ROOT ROW SAYS SO IN THE COLUMN THAT WOULD OTHERWISE NAME A FILE. Printing an empty
        #   string there would read as "a file whose name we did not bother to resolve"; `(none - runs
        #   as user)` is the fact. Falls back to the ledger key for rows written before this column.
        _sf = g.get("sudoers_file") or ("" if g.get("confers_root", True) else "(none - runs as user)")
        print(f"  {g.get('capability','?'):26} "
              f"{(e.isoformat(timespec='minutes') if e else 'NO CLOCK'):22} "
              f"{'EXPIRED' if stale else 'active':9} {_sf}")
    print(f"\n  {live} active · {exp} expired-and-awaiting-sweep")
    if exp:
        print("  ★ run `capability-grant.py sweep` — an expired grant must not keep holding root.")
    _root_of_trust(cfg, gs)
    return 0


def _root_of_trust(cfg, gs):
    """★★★ PRIV-2 AUDIT — READ-ONLY, UNPRIVILEGED, AND IT NAMES THE GAP INSTEAD OF IMPLYING NONE.

    THE PROBLEM THIS SOLVES: the contents of /etc/sudoers.d are root-only readable (mode 440), so this
    tool CANNOT read back which path an INSTALLED entry names. It can, however, answer the question
    that matters without them: is any script in the catalogue still sitting in a location the invoking
    account can rewrite? A `yes` there means that no matter how good the rendering is now, an entry
    naming the repo path is still a passwordless route to root for anything running as this account.
    ★ THAT IS THE STATE OF THIS BOX RIGHT NOW: four grants were installed BEFORE this fix and name repo
    paths. This audit is how the operator finds that out without taking anyone's word for it.
    """
    print("\n  ── ROOT OF TRUST: is the script the entry names still writable by this account? ──")
    risky = []
    for cap, spec in sorted(CATALOGUE.items()):
        if not confers_root(spec):
            continue
        src = spec["script"]
        src_bad = unsafe_chain(src)
        # The repo source is EXPECTED to be writable (it is the source, it is under version control).
        # What matters is (a) whether the INSTALLED copy is safe, and (b) whether entries installed
        # before this fix name the source directly -- which is exactly the case where src is writable.
        tgt = installed_path(spec)
        tgt_bad = unsafe_chain(tgt)
        installed_exists = os.path.exists(tgt)
        if src_bad:
            risky.append((cap, src, installed_exists))
        mark = "installed" if installed_exists else "NOT INSTALLED"
        print(f"    {cap:26} target {tgt}  [{mark}]"
              + ("" if not tgt_bad else "  ★★ TARGET IS WRITABLE"))
    if risky:
        # *** THE FIRST VERSION OF THIS SENTENCE OVERCLAIMED, AND IT WAS CAUGHT BY REREADING IT: it said
        #     these capabilities' "sudoers entries name a path in this repository". THAT IS NOT KNOWABLE
        #     HERE. /etc/sudoers.d is mode 440 root:root, so this unprivileged audit CANNOT read back
        #     what an installed entry names -- it can only say which scripts would be dangerous to name.
        #     Asserting the former from the latter is the same class of error as the charge this lane
        #     exists to fix: a reader that disagrees with the thing it reads and fails silently.
        print(f"\n  ★★★ {len(risky)} catalogue script(s) are WRITABLE BY {_invoking_user()}:")
        for cap, src, inst in risky:
            print(f"      {cap:26} {src}")
        print("  ★ ANY sudoers entry that names one of those paths is a PASSWORDLESS ROUTE TO ROOT for "
              "anything running as this account: rewrite the body, run it as root. That includes every "
              "entry installed before 2026-10-12, which named the repo file directly.")
        print("  ★ WHAT THIS AUDIT CANNOT SEE: the contents of /etc/sudoers.d need root to read, so it "
              "cannot tell you whether a given INSTALLED entry names one of these paths. It names the "
              "scripts that would be dangerous to name -- check the installed entries with "
              "`sudo cat /etc/sudoers.d/khaoss-*` if you need the other half.")
        print(f"  ★ AND THE FIX IS AN INSTALL, NOT AN EDIT: chmod cannot help (the account OWNS the "
              f"file) and chown-in-place cannot help either (this account owns the DIRECTORY, so it "
              f"can unlink and replace). The bytes that run as root must live in {CAP_LIB}.")
        print(f"  ★ TO CLOSE IT: re-issue each grant, then `sudo "
              f"{os.path.join(ROOT, 'bin', 'capability-grant-apply.sh')}` -- the applier installs the root-owned "
              f"copy and refuses any entry whose target this account can write.")
    else:
        print(f"  ✓ no catalogue script is writable by {_invoking_user()} at a path an entry names")


def cmd_propose(cfg, capability, ttl):
    spec = CATALOGUE.get(capability)
    if not spec:
        print(f"REFUSED: '{capability}' is not in the catalogue. A wizard cannot invent privilege.")
        return 2
    ttl = ttl or spec["default_ttl_minutes"]
    exp = NOW + datetime.timedelta(minutes=ttl)
    print("  ── PROPOSED GRANT (nothing written) ──────────────────────────")
    print(f"  capability : {capability}")
    print(f"  what it does: {spec['why']}")
    print(f"  confers    : {sudoers_body(capability, spec).rstrip()}")
    if confers_root(spec):
        print(f"  sudoers file: {SUDOERS_DIR}/{spec['sudoers']}")
        # ★★★ PRIV-2: THE PROPOSAL MUST NOT IMPLY A BOUNDARY THAT IS NOT THERE. This is the line the
        #   operator reads before granting root, so the ownership of the target is stated here, not
        #   left to be discovered. If the target is unsafe the proposal says so instead of reassuring.
        problems = unsafe_chain(installed_path(spec))
        if problems:
            print("  ★★ TARGET IS NOT SAFE -- this grant would confer passwordless root on a script the "
                  "invoking account can rewrite:")
            for pr in problems:
                print(f"     · {pr}")
        else:
            print(f"  ★ the entry names the ROOT-OWNED copy at {installed_path(spec)} -- a directory "
                  f"the invoking account cannot write -- so editing the repo file does not change what "
                  f"runs as root. Installing that copy is the applier's job, not the wizard's.")
    else:
        print("  sudoers file: NONE -- this capability is recorded in the ledger with a TTL and "
              "installs nothing")
    print(f"  TTL        : {ttl} minutes  ->  expires {exp.isoformat(timespec='minutes')}")
    if confers_root(spec):
        print("  ★ It confers ONLY that script with those exact arguments. Not a shell, not a wildcard.")
    else:
        print("  ★ It confers NO root: it runs that one script as you, with those exact arguments. "
              "Not a shell, not a wildcard.")
    print("  ★ At expiry, `sweep` removes the sudoers file mechanically — you do not have to remember.")
    print(f"\n  Apply with:  capability-grant.py issue {capability} --ttl {ttl}")
    return 0


def cmd_issue(cfg, capability, ttl, reason):
    spec = CATALOGUE.get(capability)
    if not spec:
        print(f"REFUSED: '{capability}' is not in the catalogue.")
        return 2
    # ★★★ PRIV-2: REFUSE TO QUEUE ROOT FOR A TARGET THE GRANTEE CAN REWRITE. Checked BEFORE anything
    #   is recorded or rendered, so a refusal leaves no ledger row and no pending file behind -- a
    #   half-issued grant would be a grant that exists in the ledger and cannot be installed.
    #   The check covers the file AND its whole directory chain, because unlink is governed by the
    #   directory. On this box it passes for CAP_LIB and would have FAILED for every repo path.
    if confers_root(spec):
        problems = unsafe_chain(installed_path(spec))
        if problems:
            print("REFUSED -- the script this grant would name is not safe to run as root:")
            for pr in problems:
                print(f"  · {pr}")
            print(f"  ★ AGENTS.md/the capability doctrine: {CAP_LIB_RULE}")
            print(f"  ★ FIX: install the source root-owned into {CAP_LIB} first "
                  f"(`sudo install -d -o root -g root -m 0755 {CAP_LIB}`), which is what the applier "
                  f"does. A NOPASSWD entry naming a file the grantee can edit is a passwordless route "
                  f"to root for anything running as {_invoking_user()}.")
            return 3
    ttl = ttl or spec["default_ttl_minutes"]
    gid = hashlib.sha256(f"{capability}{NOW.isoformat()}".encode()).hexdigest()[:12]
    _root = confers_root(spec)
    g = {
        "capability": capability, "script": spec["script"],
        # ★★★ PRIV-2: THE LEDGER NOW CARRIES BOTH PATHS AND THE PIN, so the difference between "the
        #   source in the repo" and "the bytes that run as root" is a fact in the ONE store rather
        #   than a convention in someone's head. `sudoers_file` names a file whose TARGET is
        #   `installed`; `source_sha256` is the pin, recorded for DRIFT DETECTION and not as
        #   authorisation (the applier hashes what it installs; a user-supplied hash that conferred
        #   root would be a user-supplied way to confer root).
        "installed": (installed_path(spec) if _root else None),
        "source_sha256": (source_sha256(spec) if _root else None),
        "allowed_args": spec["allowed_args"],
        # ★ A NO-ROOT CAPABILITY RECORDS `sudoers_file: None` RATHER THAN A PATH. There is no file to
        #   name, so naming one would be a ledger row pointing at a thing that must never exist -- and
        #   `cmd_status` reads this key back. `None` is the honest answer and it is falsifiable.
        "sudoers_file": (f"{SUDOERS_DIR}/{spec['sudoers']}" if _root else None),
        "confers_root": _root,
        "granted_by": "operator", "granted_at": NOW.isoformat(),
        "ttl_minutes": ttl, "reason": reason or "operator granted via the capability wizard",
        "provenance": f"capability-grant.py issue @ {NOW.isoformat(timespec='seconds')}",
    }
    cfg.setdefault("privileged_operations", {}).setdefault("grants", {})[gid] = g
    save(cfg)
    exp = expires_at(g)
    print(f"  ✅ recorded grant {gid}: {capability} (TTL {ttl}m, expires {exp.isoformat(timespec='minutes')})")
    if not _root:
        # ★★★ THE ENTIRE POINT OF THIS BRANCH: NOTHING IS RENDERED, SO NOTHING CAN BE INSTALLED.
        #   `~/.khaoss-privileged/` is the queue the ONE privileged applier reads. Writing a body there
        #   for a capability that confers nothing would put a file in front of root's hands for no
        #   reason, and the applier's job is to install what is queued. The grant is live the moment it
        #   is in the ledger -- there is no second step, because there is no privilege to install.
        print("  confers no privilege -- NO sudoers file is rendered and there is nothing to apply.")
        print(f"  the grant is live in the ledger now ({ACCESS}); it expires at "
              f"{exp.isoformat(timespec='minutes')} and `sweep` retires it mechanically.")
        return 0
    # render the sudoers content for the ONE privileged applier
    d = os.path.expanduser("~/.khaoss-privileged")
    os.makedirs(d, mode=0o700, exist_ok=True)
    p = os.path.join(d, f"{spec['sudoers']}.pending")
    with open(p, "w") as f:
        # ★★★ THE PREAMBLE IS MACHINE-READABLE AND IS WHAT MAKES THE APPLIER ABLE TO DO ITS JOB.
        #   `visudo` accepts `#` comments as sudoers syntax -- verified on this box, and a malformed
        #   entry is still REJECTED (both directions checked 2026-10-12), so adding these four lines
        #   cannot weaken the validation the applier already performs. The applier reads:
        #     khaoss-source   the repo file to install FROM
        #     khaoss-install  the root-owned path to install TO, and the path the entry names
        #     khaoss-sha256   the pin, so the applier can report a mismatch rather than install blind
        f.write(f"# khaoss-capability {capability}\n")
        f.write(f"# khaoss-source {spec['script']}\n")
        f.write(f"# khaoss-install {installed_path(spec)}\n")
        f.write(f"# khaoss-sha256 {source_sha256(spec)}\n")
        f.write(sudoers_body(capability, spec))
    os.chmod(p, 0o600)
    print(f"  rendered -> {p}")
    print(f"  ★ the entry names the ROOT-OWNED copy: {installed_path(spec)}")
    print(f"    the applier installs {os.path.basename(spec['script'])} there root:root 0755 first.")
    print(f"  now apply it:  sudo {os.path.join(ROOT, 'bin', 'capability-grant-apply.sh')}")
    return 0


def cmd_sweep(cfg):
    """Remove every expired grant from the ledger AND queue its sudoers file for removal. Mechanical:
    it does not depend on anyone having polled the grant while it was alive."""
    gs = grants(cfg)
    dead = {k: v for k, v in gs.items() if is_expired(v)}
    if not dead:
        print("  nothing expired — no root to withdraw.")
        return 0
    d = os.path.expanduser("~/.khaoss-privileged")
    os.makedirs(d, mode=0o700, exist_ok=True)
    malformed = []
    for k, v in dead.items():
        sf = v.get("sudoers_file")
        if not sf:
            # a grant with no sudoers_file still must not survive: drop it from the ledger and SAY so
            malformed.append(k)
        else:
            with open(os.path.join(d, "revoke.pending"), "a") as f:
                f.write(sf + "\n")
        del gs[k]
    if malformed:
        print(f"  ⚠ {len(malformed)} malformed grant(s) had no sudoers_file to withdraw — dropped from "
              f"the ledger: {', '.join(malformed[:3])}. VERIFY no stray file remains in {SUDOERS_DIR}.")
    save(cfg)
    print(f"  swept {len(dead)} expired grant(s) from the ledger; revocation queued.")
    print(f"  now apply it:  sudo {os.path.join(ROOT, 'bin', 'capability-grant-apply.sh')}")
    return 0


def cmd_revoke(cfg, gid):
    gs = grants(cfg)
    if gid not in gs:
        print(f"  no such grant: {gid}"); return 1
    v = gs.pop(gid); save(cfg)
    d = os.path.expanduser("~/.khaoss-privileged"); os.makedirs(d, mode=0o700, exist_ok=True)
    with open(os.path.join(d, "revoke.pending"), "a") as f:
        f.write(v["sudoers_file"] + "\n")
    print(f"  revoked {gid} ({v.get('capability')}); revocation queued — apply with the applier.")
    return 0


def cmd_render(cfg):
    for gid, g in sorted(grants(cfg).items()):
        if not is_expired(g):
            print(sudoers_body(g["capability"], g))
    return 0


KHAOSS_MANAGED = ("khaoss-drive", "khaoss-drive-part")


def cmd_reconcile(cfg):
    """★ A ledger that says one thing while sudo enforces another is the invisible-cards bug in a
    SECURITY surface. Prove they agree, or NAME the gap. Any khaoss-* sudoers file with no ledger
    entry behind it is PERMANENT ROOT nobody is tracking — report it, do not quietly adopt it."""
    gs = grants(cfg)
    on_disk = {}
    try:
        for n in os.listdir(SUDOERS_DIR):
            if n.startswith("khaoss") or n in KHAOSS_MANAGED:
                fp = os.path.join(SUDOERS_DIR, n)
                try:
                    on_disk[n] = open(fp).read().strip()
                except PermissionError:
                    on_disk[n] = "(unreadable without root)"
    except PermissionError:
        print("  cannot read /etc/sudoers.d without root — reconcile needs sudo; reporting only the ledger")
        return 1
    led = {os.path.basename(g.get("sudoers_file") or ""): g for g in gs.values()}
    unmanaged = [n for n in on_disk if n not in led]
    orphaned = [n for n in led if n not in on_disk]
    print(f"  ledger grants: {len(led)} · on-disk khaoss entries: {len(on_disk)}")
    if unmanaged:
        print(f"  ⚠ UNMANAGED (permanent root, no TTL, no ledger): {', '.join(sorted(unmanaged))}")
        print("     These are root that nobody is tracking. Adopt each with `issue`, or remove it.")
    if orphaned:
        print(f"  ⚠ ORPHANED ledger entries (no file on disk): {', '.join(sorted(orphaned))}")
    if not unmanaged and not orphaned:
        print("  ✅ ledger and sudo agree exactly")
    return 1 if (unmanaged or orphaned) else 0


def main():
    a = sys.argv[1:]
    if os.environ.get("CAPGRANT_LIE") == "expired":
        cfg = {"privileged_operations": {"grants": {"x": {"capability": "c", "granted_at": "2020-01-01T00:00:00+00:00", "ttl_minutes": 1, "sudoers_file": "/etc/sudoers.d/__lie__"}}}}
    elif os.environ.get("CAPGRANT_LIE") == "notime":
        cfg = {"privileged_operations": {"grants": {"x": {"capability": "c", "granted_at": None, "ttl_minutes": None, "sudoers_file": "/etc/sudoers.d/__lie__"}}}}
    else:
        cfg = load()
    if os.environ.get("CAPGRANT_LIE") == "future":
        cfg = {"privileged_operations": {"grants": {"x": {"capability": "c", "granted_at": (NOW + datetime.timedelta(minutes=5)).isoformat(), "ttl_minutes": 60, "sudoers_file": "/etc/sudoers.d/__lie__"}}}}
    if not a:
        return cmd_status(cfg)
    cmd = a[0]
    ttl = None
    if "--ttl" in a:
        ttl = int(a[a.index("--ttl") + 1])
    reason = a[a.index("--reason") + 1] if "--reason" in a else None
    rest = [x for x in a[1:] if not x.startswith("--") and x not in (str(ttl), str(reason or ""))]
    # ── JSON API for the wizard UI (the panel calls this; it decides nothing itself) ────────────
    if cmd == "api":
        out = {
            "catalogue": {k: {"why": v["why"], "script": v["script"],
                              "allowed_args": v["allowed_args"],
                              # ★ `confers_root` IS PUBLISHED SO A CONSUMER CANNOT MISREAD THE ENTRY
                              #   -- 2026-10-12. `launch.dsh-web` renders no sudoers line and has no
                              #   `sudoers` key at all, so a UI that assumed every catalogue row was a
                              #   root grant would draw a Grant button for an act that takes no
                              #   privilege and then be unable to name a file it never installs.
                              "confers_root": confers_root(v),
                              "default_ttl_minutes": v["default_ttl_minutes"]}
                          for k, v in CATALOGUE.items()},
            "grants": [{"id": gid, "capability": g.get("capability"),
                        "expires_at": (expires_at(g).isoformat(timespec="minutes") if expires_at(g) else None),
                        "expired": is_expired(g), "ttl_minutes": g.get("ttl_minutes"),
                        "sudoers_file": g.get("sudoers_file"), "reason": g.get("reason")}
                       for gid, g in sorted(grants(cfg).items())],
            "unmanaged": [n for n in _unmanaged_now()
                          if n not in {os.path.basename(g.get("sudoers_file") or "") for g in grants(cfg).values()}],
            "scopes": ["host-root", "agent-local", "agent-cloud", "agent-remote"],
            "note": "host-root grants are issued here; agent-local/cloud/remote are recorded for the "
                    "capability ledger and shown, not silently applied",
        }
        print(json.dumps(out))
        return 0
    if cmd == "status":   return cmd_status(cfg)
    if cmd == "propose":  return cmd_propose(cfg, rest[0] if rest else "", ttl)
    if cmd == "issue":    return cmd_issue(cfg, rest[0] if rest else "", ttl, reason)
    if cmd == "sweep":    return cmd_sweep(cfg)
    if cmd == "revoke":   return cmd_revoke(cfg, rest[0] if rest else "")
    if cmd == "render":   return cmd_render(cfg)
    if cmd == "reconcile": return cmd_reconcile(cfg)
    print("usage: capability-grant.py {status|propose|issue|sweep|revoke|render|reconcile}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
