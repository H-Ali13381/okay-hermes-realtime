# Controller Singleton Lock

## Goal

Guarantee that only one Okay Hermes Realtime controller process can own a user's runtime at a time, regardless of whether it is launched by systemd, manually, by a diagnostic command, or through a stale launcher.

The duplicate must fail before constructing `RuntimeService` or touching shared sockets, health markers, browser state, media state, or signal handlers.

## Problem

The systemd user unit already has single-unit semantics, but it cannot prevent the installed controller executable from being launched outside that unit. Today, duplicate detection occurs through the activation Unix socket and later through the HTTP port. Those checks happen after `RuntimeService` and its owned components have been constructed.

A losing controller can therefore enter partial-start cleanup. This previously allowed a failed secondary instance to remove the live controller's shared health marker, leaving the tray in `Starting` indefinitely even though the real controller and wake listener remained healthy.

The health-marker ownership bug is fixed independently. This design moves duplicate rejection to the beginning of the production entrypoint so future cleanup paths cannot affect the primary instance.

## Existing process relationships

```text
systemd --user
├── okay-hermes-realtime-controller
│   ├── dedicated Brave process group
│   └── owned mpv process group
├── okay-hermes-realtime-wakeword
│   └── one-shot activation handlers
└── okay-hermes-realtime-tray
```

The controller, listener, and tray remain separate systemd-managed processes. The wake listener keeps its existing `Requires=`, `PartOf=`, and `After=` relationship with the controller. Those relationships coordinate lifecycle but do not enforce process exclusivity.

Browser and media descendants remain owned through exact process handles and process groups. No component is restructured into a literal child merely to enforce singleton behavior.

## Decision

Use an advisory non-blocking `flock` held by the controller production entrypoint for its complete lifetime.

The lock path is:

```text
$XDG_RUNTIME_DIR/okay-hermes-realtime/controller.lock
```

If `XDG_RUNTIME_DIR` is unavailable, use the same per-user runtime-root fallback already used for the activation socket:

```text
/run/user/<uid>/okay-hermes-realtime/controller.lock
```

The lock directory is mode `0700`; the lock file is mode `0600`.

## Lock component

Add a small focused runtime component, `ControllerInstanceLock`, responsible only for:

1. Resolving and validating an absolute lock path.
2. Creating the private parent directory.
3. Opening the lock file without following a final symlink.
4. Marking the descriptor non-inheritable/close-on-exec.
5. Acquiring `LOCK_EX | LOCK_NB`.
6. Holding the descriptor until explicit release or process exit.
7. Converting contention and unsafe-path failures into bounded controller-start errors.

The file is not deleted during release. File existence does not mean ownership; the kernel lock does. Leaving the inode avoids unlink-and-recreate races between competing launchers.

The component is idempotently releasable and supports context-manager use. It never searches for, signals, or kills another process.

## Entrypoint ordering

The production entrypoint changes from:

```text
load settings → construct RuntimeService → run
```

to:

```text
resolve lock path
  → acquire singleton lock
  → load settings
  → construct RuntimeService
  → run
  → release lock during final process cleanup
```

Only path resolution and lock acquisition happen before exclusivity is established. No controller-owned runtime object is constructed first.

Direct construction of `RuntimeService` remains available to isolated unit tests and embedders. The installed `okay-hermes-realtime-controller` entrypoint is the enforced production boundary.

## Contention behavior

When another process owns the lock:

- Print one concise error: `Okay Hermes Realtime controller is already running.`
- Exit nonzero before runtime initialization.
- Do not probe, replace, or unlink the activation socket.
- Do not bind the HTTP port.
- Do not write or remove health markers.
- Do not initialize browser, media, task-event, or controller state.
- Do not signal the existing owner.

The existing activation-socket probe and HTTP bind remain defense in depth for configuration drift or a future entrypoint that bypasses the production lock.

## Crash and restart behavior

`flock` ownership is tied to the open file description. Normal exit, exceptions, SIGTERM, SIGKILL, and crashes close the descriptor and release the lock automatically.

The descriptor must not be inherited by Brave, `mpv`, activation handlers, or any other exec'd process. Python descriptors are non-inheritable by default, and the implementation will request close-on-exec explicitly as defense in depth.

A systemd restart remains serial: the old controller exits and releases its lock before the new process acquires it. If a manually launched controller owns the lock when systemd starts the unit, the unit reports a clear startup failure rather than disturbing the manual owner.

## Security boundaries

- The lock lives in a private per-user runtime directory, not `/tmp`.
- The lock path must be absolute.
- The final lock path must not be followed through a symlink.
- No PID read from disk is trusted.
- No shell command is used.
- No process is killed based on a PID file.
- The lock file contains no credentials, tokens, or runtime metadata.

## Tests

### Unit contract

- First lock acquisition succeeds.
- A second lock object targeting the same path fails while the first is held.
- Acquisition succeeds after release.
- Repeated release is safe.
- Relative paths and a symlink at the lock path fail closed.
- The descriptor is non-inheritable.

### Entrypoint regression

- Hold the singleton lock and invoke `main()` with injected factories.
- Verify the entrypoint exits nonzero.
- Verify settings loading and `RuntimeService` construction were never called.
- Verify the primary lock remains held and no shared runtime file changes.

### Subprocess integration

- Start a small holder subprocess using the real lock implementation.
- Verify a second process receives the bounded already-running failure.
- Terminate the holder without deleting the lock file.
- Verify a new process can acquire the same lock immediately.

### Existing gates

- Service lifecycle and health-marker regression tests remain green.
- Activation-socket ownership tests remain green.
- Full Python, web, native tray, package, and systemd verification remains green.
- Live deployment verifies one controller PID, one listener PID, one tray PID, `controller-health=ready`, `capture-health=healthy`, and tray tooltip `Okay Hermes Realtime: ON`.

## Alternatives rejected

### Parent/child restructuring

Rejected because process ancestry does not guarantee uniqueness. One parent may create multiple children, and a second launcher outside that tree remains possible. Existing systemd and process-group ownership already provide the required lifecycle behavior.

### Existing activation socket and HTTP port only

Rejected as the primary guard because both checks occur after partial runtime initialization. They remain defense in depth.

### PID file

Rejected because stale PID files, PID reuse, cleanup races, and process-signaling logic are less reliable than a kernel-released file lock.

### systemd socket activation

Deferred. It would require the activation socket to be owned by a `.socket` unit and passed into the Python runtime, while manual entrypoints would still need an exclusivity policy. That complexity is not justified for this bounded fix.

## Deployment

The change ships in the Python controller package. Rebuild and reinstall the wheel into the deployed controller virtual environment, then restart the controller and wakeword units. The tray binary does not require a code change for singleton ownership, but the live system must be checked for one current tray process and coherent health markers.

## Approved scope

- One per-user controller singleton lock.
- Early rejection before `RuntimeService` construction.
- Kernel-managed release with no PID-file cleanup.
- Existing systemd relationships and process-group ownership retained.
- Activation socket and HTTP port retained as secondary guards.
