# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report them privately through GitHub instead: on this repository, go to the
**Security** tab and choose **Report a vulnerability**. This opens a private
security advisory that only the maintainers can see.

Please include what you found, how to reproduce it, and which version or
commit you tested. You will get an answer as soon as possible, and we will
agree on a disclosure date together once a fix is ready.

## Threat model

Shellside is a browser front end to a real shell. Before you report or deploy,
keep in mind what it is designed to protect, and what it is not:

- **Anyone who gets past the gate has a full shell** as the user running
  Shellside, with all of that user's permissions. It is not a sandbox. Run it
  as an account whose access you are willing to hand to whoever signs in.
- **The default bind address is `127.0.0.1`.** Shellside is only reachable
  from the machine it runs on unless you start it with `--host 0.0.0.0` or
  change the bind address in Settings.
- **Local connections are trusted without a password** while Shellside is
  bound to loopback only, you have not signed out, and tunnel mode is off.
  Requests that carry reverse-proxy headers, or whose `Host` is not a local
  name, never get this trust. This means other users and processes on the
  same machine can reach your shell through it: do not run it bound to
  loopback on a shared, multi-user machine without signing out or turning on
  tunnel mode.
- **On the LAN a password is required.** Requests from other machines need a
  session from the LAN password (or an enrolled device), and Shellside fails
  closed: with no password set, nobody on the LAN gets in. The only way to
  drop the password is a deliberate "allow the LAN with no password" switch.
  Login attempts are throttled and rate limited per IP. Cross-site requests
  and websockets from other origins are refused.
- **Never expose Shellside directly to the internet.** Put it behind an
  authenticating proxy (for example Cloudflare Access) or a VPN, so nobody
  reaches the Shellside login page without first authenticating to something
  else. Its internet password and device enrollment are an extra layer, not a
  replacement for that.
- Shellside serves plain HTTP. Anything beyond your own machine or a trusted
  LAN should reach it over HTTPS through such a proxy or tunnel.

Issues that require an attacker to already have a shell as the Shellside
user, or to control the machine it runs on, are out of scope.
