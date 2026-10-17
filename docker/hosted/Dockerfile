# Managed-evaluation runner image: the platform pulls this single image and
# starts it in an isolated sandbox with BENCHMARK_* and the model credential
# injected. Everything the run needs - Harness, Kali toolset, Node/Pi - lives
# in this one layer stack; no nested Docker, no public network access.
ARG KALI_BASE_IMAGE=kalilinux/kali-last-release@sha256:3ea545e38849417fc933514e117014b98aa42fd87d239177fa4c9874dcb73d5f
FROM ${KALI_BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG PI_VERSION=1.0.4
# Kali mirror base URL (overridable for fast regional mirrors; must be http://
# because the base image ships without CA certificates).
ARG KALI_MIRROR=http://http.kali.org/kali

RUN find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) \
        -exec sed -i \
            -e "s#http://http.kali.org/kali#${KALI_MIRROR}#g" \
            -e "s#https://http.kali.org/kali#${KALI_MIRROR}#g" \
            -e "s#http://kali.download/kali#${KALI_MIRROR}#g" \
            -e 's/kali-rolling/kali-last-snapshot/g' {} + \
    && grep -Rqs 'kali-last-snapshot' /etc/apt \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        kali-linux-core \
        ca-certificates \
        curl \
        wget \
        git \
        jq \
        ripgrep \
        file \
        procps \
        iproute2 \
        iputils-ping \
        dnsutils \
        whois \
        netcat-openbsd \
        socat \
        openssh-client \
        nmap \
        masscan \
        tcpdump \
        ffuf \
        gobuster \
        dirb \
        wordlists \
        nikto \
        whatweb \
        sqlmap \
        hydra \
        binutils \
        gdb \
        strace \
        ltrace \
        gcc \
        g++ \
        make \
        python3 \
        python3-pip \
        python3-venv \
        python3-pwntools \
        smbclient \
        impacket-scripts \
        enum4linux \
        snmp \
        default-mysql-client \
        redis-tools \
        sqlite3 \
        john \
        hashcat \
        binwalk \
        exiftool \
        steghide \
        unzip \
        xxd \
        php-cli \
        traceroute \
        iptables \
        python3-requests \
        python3-bs4 \
        python3-lxml \
        python3-pil \
        python3-pycryptodome \
        python3-paramiko \
        nodejs \
        npm \
    && node -e 'const [M,m]=process.versions.node.split(".").map(Number); if (M < 22 || (M === 22 && m < 19)) { console.error("Pi requires Node >= 22.19"); process.exit(1) }' \
    && test -s /usr/share/wordlists/dirb/common.txt \
    && gunzip -kf /usr/share/wordlists/rockyou.txt.gz \
    && npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && pi --version \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.npm

# Crypto/solver Python stack: pycryptodome, sympy, gmpy2, z3 (the four
# workhorses for CTF crypto challenges) - the sandbox has no network at
# runtime, so they must be baked in.
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        pycryptodome sympy gmpy2 z3-solver \
    && python3 -c "import Crypto, sympy, gmpy2, z3; print('solver stack ok')"

# Kali's nmap carries file capabilities (net_bind_service+net_admin+net_raw).
# The managed sandbox grants only the default container set (no NET_ADMIN), so
# executing the file-capability binary would fail with EPERM before nmap even
# starts. Strip them: root plus the default NET_RAW is enough for scanning,
# and masscan ships without file capabilities.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcap2-bin \
    && test -x /usr/lib/nmap/nmap \
    && setcap -r /usr/lib/nmap/nmap \
    && nmap --version | head -1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work

# Install the Harness itself from the build context (must run from the
# repository root: docker build -f docker/hosted/Dockerfile -t <tag> .).
# The [tsec] extra bundles the TSec Benchmark SDK - the runner has no public
# network at runtime, so the SDK must already be present in the image.
COPY pyproject.toml README.md /tmp/harness-build/
COPY src /tmp/harness-build/src
RUN python3 -m pip install --no-cache-dir --break-system-packages '/tmp/harness-build[tsec]' \
    && python3 -c "from tsec_benchmark import TSecBenchmarkAsync; print('tsec SDK ok')" \
    && rm -rf /tmp/harness-build

COPY agents /opt/red-harness/agents
COPY prompts /opt/red-harness/prompts
COPY skills /opt/red-harness/skills
COPY docker/hosted/entrypoint.sh /opt/red-harness/entrypoint.sh

# Fail the build when a hosted agent references a file the image does not
# carry (a missing system prompt only surfaced two uploads into the sandbox).
RUN python3 - <<'EOF'
from pathlib import Path
import yaml

for spec_path in Path("/opt/red-harness/agents").rglob("*.yaml"):
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    reference = (spec.get("pi") or {}).get("system_prompt_file")
    if reference and not Path(reference).is_file():
        raise SystemExit(f"{spec_path}: system prompt file missing in image: {reference}")
print("hosted agent assets ok")
EOF

ENTRYPOINT ["/opt/red-harness/entrypoint.sh"]
