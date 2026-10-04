"""This source is embedded into the exact-version bootstrap release asset."""
def bootstrap(version, public_key):
    supported_host()
    if os.geteuid() != 0:
        raise RuntimeError("Run bootstrap as root")
    root = Path("/opt/exocortex/preza")
    trust = Path("/etc/exocortex/release-trust/preza.pem")
    wrapper = Path("/usr/local/bin/preza-install")
    if root.exists() or root.is_symlink():
        raise RuntimeError("Preza is already prepared. Use preza-install or preza-install update VERSION.")
    if wrapper.exists() or wrapper.is_symlink():
        raise RuntimeError("Existing /usr/local/bin/preza-install was not overwritten")
    for parent in (root.parent, trust.parent):
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
            raise RuntimeError("Installation/trust parents must be root-owned directories")
    with tempfile.TemporaryDirectory(prefix="preza-bootstrap-") as temporary:
        temporary = Path(temporary)
        embedded = temporary / "preza.pem"
        embedded.write_text(public_key)
        check_public_key(embedded)
        if trust.is_symlink():
            raise RuntimeError("Release trust file must not be a symlink")
        if trust.exists() and (trust.stat().st_uid != 0 or trust.stat().st_mode & 0o022):
            raise RuntimeError("Release trust file must be root-owned and not writable by other users")
        if trust.exists() and trust.read_bytes() != embedded.read_bytes():
            raise RuntimeError("Release key differs from installed trust. Explicit key rotation is required.")
        if not trust.exists():
            pending = trust.parent / (".preza-" + str(os.getpid()) + ".tmp")
            pending.write_bytes(embedded.read_bytes())
            pending.chmod(0o644)
            try:
                os.link(pending, trust)
            except FileExistsError:
                if trust.read_bytes() != embedded.read_bytes():
                    raise RuntimeError("Concurrent bootstrap installed a different release key")
            finally:
                pending.unlink(missing_ok=True)
        extracted, manifest = fetch_release(version, trust, temporary / "bundle")
        root.mkdir(mode=0o755)
        (root / "releases").mkdir()
        target = root / "releases" / version
        shutil.move(str(extracted), target)
        (root / "current").symlink_to(target, target_is_directory=True)
        subprocess.run(["python3", str(target / "scripts/install.py"), "prepare"], check=True)
        pending = wrapper.parent / (".preza-install-" + str(os.getpid()))
        pending.write_text('#!/bin/sh\nexec python3 /opt/exocortex/preza/current/scripts/install.py "$@"\n')
        pending.chmod(0o755)
        pending.replace(wrapper)
    print("Verified release prepared:", version)
    print("Edit only OPERATOR INPUT:", root / ".env")
    print("Then run: sudo chmod 600 /opt/exocortex/preza/.env && sudo preza-install")
    print("Verify: sudo preza-install status")
    print("Nginx remains server-managed; enable its vhost after loopback health succeeds.")
