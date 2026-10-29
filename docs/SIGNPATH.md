# Free public code signing (SignPath Foundation)

SignPath Foundation signs open-source Windows projects **for free** with a
publicly trusted certificate (Microsoft-trusted chain, so SmartScreen and
Defender see a known publisher).

Requirements: public repository, OSI license, CI build.

## Application text (paste into https://signpath.org/apply)

Project: Pearl Fleet (Open Pearl Miner fork)
Repository: https://github.com/denis-vk/pearl-fleet
License: <see LICENSE in repo>
Description: Fleet agent and miner supervisor for the Pearl (PRL) proof-of-work
network. Native C agent (agent/fleet_agent.c, ~300 lines) that enrolls a machine
into a management panel, downloads the miner binary, and supervises it. Build
artifact: a single 50 KB Windows x64 GUI executable (wcsvc.exe) produced with
mingw-w64; no third-party runtime dependencies.

## After approval: sign in CI

    SignPath.SigningTool.exe sign --certificate-profile <profile> \
        --artifact wcsvc.exe

Or with signtool + their .pfx, then upload to the panel as the installer.
