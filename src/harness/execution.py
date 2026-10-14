from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionCapabilities:
    docker: bool
    gvisor_runsc: bool
    pi: bool
    bwrap: bool

    @classmethod
    def detect(cls) -> ExecutionCapabilities:
        docker = shutil.which("docker") is not None
        return cls(
            docker=docker,
            gvisor_runsc=shutil.which("runsc") is not None,
            pi=docker,
            bwrap=shutil.which("bwrap") is not None,
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "docker": self.docker,
            "gvisor_runsc": self.gvisor_runsc,
            "pi": self.pi,
            "bwrap": self.bwrap,
        }
