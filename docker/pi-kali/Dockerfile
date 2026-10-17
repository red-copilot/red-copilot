ARG KALI_BASE_IMAGE=kalilinux/kali-last-release@sha256:3ea545e38849417fc933514e117014b98aa42fd87d239177fa4c9874dcb73d5f
FROM ${KALI_BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG PI_VERSION=1.0.4
# Kali mirror base URL (overridable for fast regional mirrors, e.g.
# https://mirrors.tuna.tsinghua.edu.cn/kali or https://mirrors.ustc.edu.cn/kali).
ARG KALI_MIRROR=http://http.kali.org/kali

# The package list is the Agent's tool surface, chosen from what runs actually
# reached for (2026-10-16 sweep): the image already shipped ten Kali tools and
# used three, `unzip`/`xxd`/`php`/`traceroute` were missing outright, and the
# Python stack below had to be installed offline because `pip install` cannot
# run under the offline profiles.
#
# `setcap -r /usr/lib/nmap/nmap` is deliberate: Kali ships that binary with file
# capabilities (cap_net_bind_service,cap_net_admin,cap_net_raw=eip) and the
# kernel refuses the exec under `--cap-drop ALL` plus `no-new-privileges` with
# EPERM, which broke reconnaissance in 28 results of the same sweep. Stripping
# the file capabilities restores nmap and removes a privilege-escalation vector
# instead of relaxing the sandbox: -sT works unprivileged and -sS uses the
# container's own NET_RAW when the agent declares it.

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
        make \
        python3 \
        python3-pip \
        python3-venv \
        nodejs \
        npm \
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
    && node -e 'const [M,m]=process.versions.node.split(".").map(Number); if (M < 22 || (M === 22 && m < 19)) { console.error("Pi requires Node >= 22.19"); process.exit(1) }' \
    && test -s /usr/share/wordlists/dirb/common.txt \
    && npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && pi --version \
    && setcap -r /usr/lib/nmap/nmap \
    && test -z "$(getcap /usr/lib/nmap/nmap)" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.npm

ENV PI_TELEMETRY=0 \
    PI_SKIP_VERSION_CHECK=1

WORKDIR /run/harness

CMD ["pi", "--version"]
