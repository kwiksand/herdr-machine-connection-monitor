from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ServerInfo:
    ip: Optional[str] = None
    hostname: Optional[str] = None
    port: int = 22
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TestConfig:
    type: str = "shell"  # "shell", "ping", "tcp", "herdr"
    online: Optional[str] = None
    timeout_seconds: float = 5.0
    port: Optional[int] = None


@dataclass
class MachineConfig:
    name: str
    server: ServerInfo = field(default_factory=ServerInfo)
    test: TestConfig = field(default_factory=TestConfig)
    poll_interval: Optional[float] = None
    max_retries: Optional[int] = None
    retry_delay: Optional[float] = None
    auto_disable: Optional[bool] = None
    auto_enable: Optional[bool] = None


@dataclass
class GlobalDefaults:
    max_retries: int = 3
    retry_delay_seconds: float = 5.0
    poll_interval_seconds: float = 30.0
    probe_timeout_seconds: float = 5.0
    auto_disable: bool = True
    auto_enable: bool = False
    herdr_bin: str = "herdr"


@dataclass
class HerdrMachineProfile:
    id: str
    label: str
    target: str
    session: str
    enabled: bool
    selected: bool = False


@dataclass
class MachineRuntimeState:
    name: str
    retry_count: int = 0
    is_online: Optional[bool] = None
    herdr_profile_id: Optional[str] = None
    herdr_enabled: Optional[bool] = None
    last_check_ts: Optional[float] = None
    last_success_ts: Optional[float] = None
    last_failure_ts: Optional[float] = None
    last_error_message: Optional[str] = None
    disabled_ts: Optional[float] = None
    next_check_ts: float = 0.0
