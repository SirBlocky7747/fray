#!/usr/bin/env python3
"""release_sign.py — checksum, provenance and signature for a fray release archive.

Writes three things next to the archive in dist/:

  SHA256SUMS       standard `sha256sum` format, verifiable with `sha256sum -c`
  PROVENANCE.json  a SLSA v0.2 provenance attestation about the archive
  PROVENANCE.md    the same facts, readable by a person
  SHA256SUMS.asc   a detached ASCII-armour signature over SHA256SUMS, when a
                   signing key is configured

Signing is not optional by accident: with no key configured this exits non-zero
unless --allow-unsigned is passed, so a release can never ship an unsigned
checksum file that looks signed.

Usage:
  python3 tools/release_sign.py --archive dist/fray-0.1.0-linux-x86_64.tar.gz
  python3 tools/release_sign.py --archive ... --allow-unsigned

Signing keys are discovered, never created:
  * --signer gpg    (default when a GPG secret key is usable)
  * --signer ssh    signs with `ssh-keygen -Y sign`, key from --signing-key
  * --signer none   --allow-unsigned

Environment:
  FRAY_SIGNING_KEY   GPG key id/uid, or (with --signer ssh) the private key path
  FRAY_SIGNING_PASSPHRASE
                     passphrase for a protected GPG key, read on stdin rather
                     than from argv so it does not show up in `ps`
  FRAY_GNUPGHOME     use an alternate GnuPG home (for tests)
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_SLUG = "SirBlocky7747/fray"
PREDICATE_TYPE = "https://slsa.dev/provenance/v0.2"
BUILD_TYPE = f"https://github.com/{REPO_SLUG}/releases/tag/{{tag}}"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def git(*args: str, cwd: Path) -> str:
    r = run(["git", *args], cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else ""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tool_version(argv: list[str]) -> str:
    exe = shutil.which(argv[0])
    if not exe:
        return "not found"
    r = run([exe, *argv[1:]])
    out = (r.stdout or r.stderr).strip().splitlines()
    return out[0] if out else "unknown"


# The same candidate order bin/frayc uses, so the attestation records the
# emitter the chain would actually pick rather than "not found" on a machine
# that only has a versioned llc.
LLC_CANDIDATES = ["llc-22", "llc-21", "llc-20", "llc-19", "llc-18",
                  "llc-17", "llc-16", "llc-15", "llc-14", "llc"]


def object_emitter() -> str:
    for cand in LLC_CANDIDATES:
        if shutil.which(cand):
            return f"{cand} — {tool_version([cand, '--version'])}"
    return "not found"


def collect_provenance(repo: Path, archive: Path, digest: str) -> dict:
    """Facts about this build, every one of them read rather than assumed."""
    commit = git("rev-parse", "HEAD", cwd=repo)
    tag = git("describe", "--tags", "--exact-match", cwd=repo) or ""
    version = git("describe", "--tags", "--abbrev=0", cwd=repo).removeprefix("v")
    try:
        dirty = bool(git("status", "--porcelain", cwd=repo))
    except Exception:
        dirty = False

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    return {
        "schemaVersion": 1,
        "predicateType": PREDICATE_TYPE,
        "subject": [
            {
                "name": archive.name,
                "digest": {"sha256": digest},
            }
        ],
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE.format(tag=tag or "untagged"),
                "externalParameters": {
                    "version": version,
                    "tag": tag,
                    "platform": "linux-x86_64",
                    "repository": f"https://github.com/{REPO_SLUG}",
                },
                "internalParameters": {
                    "packagingScript": "tools/package_release.sh",
                    "signingScript": "tools/release_sign.py",
                },
                "resolvedDependencies": [
                    {
                        "uri": f"git+https://github.com/{REPO_SLUG}@{commit}",
                        "digest": {"gitCommit": commit},
                    }
                ],
            },
            "runDetails": {
                "builder": {"id": "local-workstation"},
                "metadata": {
                    "invocationId": digest[:32],
                    "startedOn": now,
                    "sourceWasDirty": dirty,
                    "toolchain": {
                        "objectEmitter": object_emitter(),
                        "cCompiler": tool_version(["cc", "--version"]),
                    },
                },
            },
        },
    }


def render_markdown(prov: dict, digest: str, archive_name: str) -> str:
    bd = prov["predicate"]["buildDefinition"]
    rd = prov["predicate"]["runDetails"]
    meta = rd["metadata"]
    dep = bd["resolvedDependencies"][0]
    tc = meta["toolchain"]
    lines = [
        f"# Provenance — {archive_name}",
        "",
        f"Generated by `tools/release_sign.py` using the SLSA v0.2 predicate",
        f"(`{PREDICATE_TYPE}`). The machine-readable form is `PROVENANCE.json`.",
        "",
        "## Subject",
        "",
        f"- `{archive_name}`",
        f"- sha256: `{digest}`",
        "",
        "## Source",
        "",
        f"- repository: <https://github.com/{REPO_SLUG}>",
        f"- commit: `{dep['digest']['gitCommit']}`",
        f"- tag: `{bd['externalParameters']['tag'] or '(none)'}`",
        f"- version: `{bd['externalParameters']['version'] or '(unknown)'}`",
        f"- working tree was dirty when packaged: **{meta['sourceWasDirty']}**",
        "",
        "## Build",
        "",
        f"- builder: `{rd['builder']['id']}`",
        f"- packaging script: `{bd['internalParameters']['packagingScript']}`",
        f"- started: {meta['startedOn']}",
        "",
        "## Toolchain observed at packaging time",
        "",
        f"- object emitter: {tc['objectEmitter']}",
        f"- C compiler: {tc['cCompiler']}",
        "",
        "## Verifying",
        "",
        "```sh",
        f"sha256sum -c SHA256SUMS",
        "gpg --verify SHA256SUMS.asc SHA256SUMS",
        "```",
        "",
    ]
    return "\n".join(lines)


def gpg_usable(env: dict, key: str | None) -> str | None:
    """Return the key id to sign with, or None when gpg cannot sign."""
    if not shutil.which("gpg"):
        return None
    if key:
        return key
    r = run(["gpg", "--list-secret-keys", "--with-colons"], env=env)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("sec:"):
            parts = line.split(":")
            return parts[4] if len(parts) > 4 else None
    return None


def gpg_passphrase() -> tuple[list[str], str | None]:
    """Args for signing with a passphrase-protected key.

    `gpg --batch` never prompts: on a protected key it just fails. Loopback
    mode with the passphrase arriving on stdin is the non-interactive way to
    sign one. The passphrase is kept out of argv, where `ps` would show it.
    """
    pw = os.environ.get("FRAY_SIGNING_PASSPHRASE")
    if not pw:
        return [], None
    return ["--pinentry-mode", "loopback", "--passphrase-fd", "0"], pw


def sign_gpg(sums: Path, sig: Path, key: str, env: dict) -> tuple[str, bool]:
    pw_args, pw = gpg_passphrase()
    r = subprocess.run(
        ["gpg", "--batch", "--yes", "--armor", "--detach-sign",
         "--local-user", key, "--output", str(sig), *pw_args, str(sums)],
        env=env, input=pw, capture_output=True, text=True,
    )
    if r.returncode != 0:
        err = (r.stderr or "").lower()
        hint = ""
        # gpg reports a locked key several ways depending on version and
        # whether an agent is reachable: "Operation cancelled", "no pinentry",
        # or an explicit passphrase complaint.
        if not pw and any(w in err for w in
                          ("passphrase", "operation cancelled", "pinentry",
                           "secret key is not available", "locked")):
            hint = ("\n  This key is passphrase-protected and could not be unlocked:"
                    "\n    export FRAY_SIGNING_PASSPHRASE=<passphrase>"
                    "\n  or use a release key with no passphrase.")
        raise SystemExit(f"gpg signing failed:\n{r.stderr.strip()}{hint}")
    r2 = run(["gpg", "--batch", "--verify", str(sig), str(sums)], env=env)
    if r2.returncode == 0:
        return "verified", True
    return f"SIGNED BUT DID NOT VERIFY: {r2.stderr.strip()}", False


def ssh_identity(key_path: str) -> str:
    """ssh-keygen -Y sign takes its identity from the key's comment, so the
    verifier has to use the same string. Read it back from the .pub file."""
    pub = Path(key_path + ".pub")
    if not pub.is_file():
        sys.exit(f"release_sign.py: no public key at {pub}")
    parts = pub.read_text().strip().split(None, 2)
    if len(parts) < 3:
        sys.exit(f"release_sign.py: {pub} has no comment to use as an identity")
    return parts[2].strip()


def sign_ssh(sums: Path, sig: Path, key_path: str, allowed: str,
             identity: str) -> tuple[str, bool]:
    # ssh-keygen -Y sign writes "<file>.sig" next to the input.
    r = run([
        "ssh-keygen", "-Y", "sign",
        "-n", "file", "-f", key_path, str(sums),
    ])
    produced = sums.with_name(sums.name + ".sig")
    if r.returncode != 0 or not produced.exists():
        raise SystemExit(f"ssh-keygen signing failed:\n{r.stderr.strip()}")
    os.replace(produced, sig)

    # -Y verify reads the signed data on stdin. Passing it as an argument makes
    # ssh-keygen block forever waiting for input that never arrives.
    with sums.open("rb") as fh:
        r2 = subprocess.run(
            ["ssh-keygen", "-Y", "verify",
             "-f", allowed, "-I", identity, "-n", "file", "-s", str(sig)],
            stdin=fh, capture_output=True, text=True,
        )
    if r2.returncode == 0:
        return "verified", True
    publine = Path(key_path + ".pub").read_text().strip()
    hint = (f"\n  'ssh-keygen -Y sign' stamps the identity from the signing key's"
            f" comment, so {identity!r} has to appear as a principal in {allowed}:"
            f"\n    {identity} {publine}")
    return (f"SIGNED BUT DID NOT VERIFY: "
            f"{(r2.stderr or r2.stdout).strip()}{hint}"), False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", required=True, type=Path)
    ap.add_argument("--signer", choices=("gpg", "ssh", "none", "auto"), default="auto")
    ap.add_argument("--signing-key", default=os.environ.get("FRAY_SIGNING_KEY", ""))
    ap.add_argument("--allowed-signers", default="",
                    help="path to allowed_signers file (ssh signing verification)")
    ap.add_argument("--signing-identity", default="",
                    help="ssh identity to verify against; defaults to the "
                         "comment on the signing key's .pub file")
    ap.add_argument("--allow-unsigned", action="store_true",
                    help="emit an unverified checksum file instead of failing")
    args = ap.parse_args()

    # package_release.sh sets this in the environment rather than passing a
    # flag, so the two scripts stay parseable as plain sh.
    if os.environ.get("FRAY_ALLOW_UNSIGNED") == "1":
        args.allow_unsigned = True

    archive: Path = args.archive
    if not archive.is_file():
        sys.exit(f"release_sign.py: no such archive: {archive}")
    repo = Path(__file__).resolve().parent.parent
    dist = archive.parent

    env = dict(os.environ)
    if os.environ.get("FRAY_GNUPGHOME"):
        env["GNUPGHOME"] = os.environ["FRAY_GNUPGHOME"]

    digest = sha256(archive)
    print(f"archive  {archive.name}  {archive.stat().st_size} bytes")
    print(f"sha256   {digest}")

    # 1. Checksum file, standard format so `sha256sum -c` just works.
    sums = dist / "SHA256SUMS"
    sums.write_text(f"{digest}  {archive.name}\n")
    print(f"wrote    {sums.name}")

    # 2. Provenance.
    prov = collect_provenance(repo, archive, digest)
    (dist / "PROVENANCE.json").write_text(json.dumps(prov, indent=2) + "\n")
    (dist / "PROVENANCE.md").write_text(render_markdown(prov, digest, archive.name))
    print(f"wrote    PROVENANCE.json  PROVENANCE.md")

    commit = prov["predicate"]["buildDefinition"]["resolvedDependencies"][0]["digest"]["gitCommit"]
    print(f"commit   {commit[:12]}  tag {prov['predicate']['buildDefinition']['externalParameters']['tag'] or '(none)'}")

    # 3. Signature.
    sig = dist / "SHA256SUMS.asc"
    if sig.exists():
        sig.unlink()

    signer = args.signer
    if signer == "auto":
        signer = "gpg" if gpg_usable(env, args.signing_key or None) else "none"

    if signer == "none":
        msg = (
            "release_sign.py: no signing key configured, so SHA256SUMS is "
            "UNSIGNED.\n"
            "  This is expected on a machine without a release key, but do not "
            "publish\n"
            "  an unsigned checksum file as though it were signed.\n"
            "  To sign: configure a GPG secret key or pass "
            "--signer ssh --signing-key PATH."
        )
        if not args.allow_unsigned:
            print(msg, file=sys.stderr)
            return 2
        print(msg, file=sys.stderr)
        print("  continuing anyway because --allow-unsigned was passed")
        return 0

    if signer == "gpg":
        key = gpg_usable(env, args.signing_key or None)
        if not key:
            sys.exit("release_sign.py: --signer gpg but no usable secret key found")
        state, ok = sign_gpg(sums, sig, key, env)
        print(f"wrote    {sig.name}  (gpg key {key}, {state})")
    else:
        if not args.signing_key:
            sys.exit("release_sign.py: --signer ssh needs --signing-key / $FRAY_SIGNING_KEY")
        allowed = args.allowed_signers or f"{args.signing_key}.pub"
        identity = args.signing_identity or ssh_identity(args.signing_key)
        state, ok = sign_ssh(sums, sig, args.signing_key, allowed, identity)
        print(f"wrote    {sig.name}  (ssh key {args.signing_key}, "
              f"identity {identity!r}, {state})")

    # A signature that does not verify is a failure, not a warning: shipping it
    # would leave a file that looks signed but is not.
    if not ok:
        print("\nrelease_sign.py: the signature did not verify. Refusing to "
              "present it as a valid signature.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())