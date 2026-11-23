# Browser sandbox configuration

`seccomp.json` is from Microsoft Playwright v1.64.1:
https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json

Original SHA-256: cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849

It is the Docker default-deny syscall profile with additional unprivileged user
namespace support for Chromium. This copy additionally makes `clone3` return
`ENOSYS` so modern glibc can fall back to the allowed `clone` syscall. It permits
the `chroot` syscall independently of the container's (empty) capability set,
because Chromium uses it after entering its own user namespace to drop filesystem
access. Kernel capability checks still apply; no container capability is added.
See https://chromium.googlesource.com/chromium/src/+/main/sandbox/linux/services/credentials.cc . Chromium's
own user-namespace and renderer seccomp sandboxes remain enabled. No SYS_ADMIN,
privileged mode, host IPC or unconfined seccomp fallback is used.

Playwright is distributed under Apache-2.0; see LICENSE.playwright.
