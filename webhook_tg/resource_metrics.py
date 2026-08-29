from __future__ import annotations

import shutil
from dataclasses import dataclass

import psutil


def human_gb(bytes_: int) -> str:
    return f"{bytes_ / 1024 / 1024 / 1024:.1f} GB"


@dataclass(frozen=True)
class ResourceSnapshot:
    disk_path: str
    disk_used_pct: int
    disk_free: int
    disk_total: int
    memory_used_pct: int
    memory_available: int
    memory_total: int
    cpu_pct: int


def collect_resource_snapshot(*, disk_path: str = "/", cpu_interval: float = 1) -> ResourceSnapshot:
    disk = shutil.disk_usage(disk_path)
    memory = psutil.virtual_memory()
    return ResourceSnapshot(
        disk_path=disk_path,
        disk_used_pct=int((disk.used / disk.total) * 100),
        disk_free=disk.free,
        disk_total=disk.total,
        memory_used_pct=int(memory.percent),
        memory_available=memory.available,
        memory_total=memory.total,
        cpu_pct=int(psutil.cpu_percent(interval=cpu_interval)),
    )


def resource_report_text(snapshot: ResourceSnapshot, *, title: str = "📊 Ресурсы сервера") -> str:
    return (
        f"{title}\n\n"
        "<b>maksonchik.ru</b>\n"
        f"RAM: <b>{snapshot.memory_used_pct}%</b> "
        f"(доступно {human_gb(snapshot.memory_available)} / "
        f"всего {human_gb(snapshot.memory_total)})\n"
        f"CPU: <b>{snapshot.cpu_pct}%</b>\n"
        f"Диск {snapshot.disk_path}: <b>{snapshot.disk_used_pct}%</b> "
        f"(свободно {human_gb(snapshot.disk_free)} / "
        f"всего {human_gb(snapshot.disk_total)})"
    )
