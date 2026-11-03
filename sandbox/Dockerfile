# The general sandbox image: many languages in one place, for the coding agents to compile, run and test in.
#
# Build it ONCE (this is the only time it needs the internet):
#     docker build -t sera-sandbox:latest SeraFunctionality/sandbox
# After that the bot notices it on its own (agent_sandbox.GENERAL_IMAGE) and uses it. Until it exists the agents get the plain Python image.
#
# At run time every container started from it has NO network, runs as nobody, with a read-only filesystem and a throwaway scratch space
# (see agent_sandbox.run_command), so what is installed here is all they will ever have: no pip install, no npm install, no crates, no CRAN.
# To add a language later, add its package here and build again.
#
#   C, C++      gcc g++ make cmake          Go           golang-go            Rust        rustc cargo
#   C#          mono-complete (mcs, mono)   Java         default-jdk-headless R           r-base-core
#   JavaScript  nodejs                      TypeScript   node-typescript      Ruby        ruby
#   PHP         php-cli                     Lua          lua5.4               Perl        perl (already here)
#   Fortran     gfortran                    Python       python3 (already here)           SQL         sqlite3
#   HTML/CSS/JS in a real browser: chromium, driven with the `render` helper below; node-jsdom for quick DOM tests without a browser
#   Tools       curl (for a server started inside the same container), git, jq, tidy (HTML checker)
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ make cmake libc6-dev \
        nodejs node-typescript node-jsdom \
        golang-go \
        rustc cargo \
        default-jdk-headless \
        mono-complete \
        r-base-core \
        ruby \
        php-cli \
        lua5.4 \
        gfortran \
        sqlite3 curl git jq tidy \
        chromium \
    && ln -sf /usr/bin/lua5.4 /usr/local/bin/lua \
    && rm -rf /var/lib/apt/lists/*

# render FILE.html: load a page in headless Chromium, let its scripts run, print the final DOM and any console messages / errors.
# (--no-sandbox is needed because the container runs without capabilities; the container itself is the sandbox.)
RUN printf '%s\n' '#!/bin/sh' \
      'f="$1"; [ -f "$f" ] || { echo "render: no such file: $f"; exit 2; }' \
      'exec chromium --headless --no-sandbox --disable-gpu --disable-dev-shm-usage --disable-extensions --no-first-run \' \
      '  --user-data-dir=/tmp/chrome-profile --virtual-time-budget=5000 --enable-logging=stderr --v=0 --dump-dom "file://$(realpath "$f")" 2>&1' \
    > /usr/local/bin/render && chmod 755 /usr/local/bin/render
