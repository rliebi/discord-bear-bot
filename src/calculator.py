from dataclasses import dataclass
from typing import Tuple


def round_down_to_1000(n: int) -> int:
    if n <= 0:
        return 0
    return (n // 1000) * 1000


@dataclass
class GuildConfig:
    max_troop_size: int
    infantry_amount: int
    max_archers_amount: int


@dataclass
class MarchResult:
    joining_archers: int
    joining_infantry: int
    joining_cavalry: int
    calling_archers: int
    calling_infantry: int
    calling_cavalry: int


def compute_kingshot(g: GuildConfig, total_archers: int, march_count: int, is_caller: bool) -> MarchResult:
    if march_count <= 0:
        raise ValueError("march_count must be > 0")
    if g.max_troop_size <= 0:
        raise ValueError("Server Max Troop Size not configured")
    if g.infantry_amount < 0 or g.max_archers_amount < 0:
        raise ValueError("Invalid server settings")

    # Caller archer value for joining marches
    divisor = march_count + (1 if is_caller else 0)
    base = total_archers // max(1, divisor)
    base = round_down_to_1000(base)
    caller_archer_value = min(base, g.max_archers_amount)

    # Joining march values
    joining_archers = caller_archer_value
    joining_infantry = g.infantry_amount
    joining_cavalry = max(0, g.max_troop_size - joining_archers - joining_infantry)
    joining_cavalry = round_down_to_1000(joining_cavalry)

    # Calling march values
    if is_caller:
        remaining_archers = total_archers - (caller_archer_value * march_count)
        remaining_archers = max(0, remaining_archers)
        calling_infantry = g.infantry_amount
        # Fit archers then cav into max troop size, also keep 1000-step rounding
        max_archers_slot = max(0, g.max_troop_size - calling_infantry)
        calling_archers = round_down_to_1000(min(remaining_archers, max_archers_slot))
        calling_cavalry = max(0, g.max_troop_size - calling_infantry - calling_archers)
        calling_cavalry = round_down_to_1000(calling_cavalry)
    else:
        calling_archers = 0
        calling_infantry = 0
        calling_cavalry = 0

    return MarchResult(
        joining_archers=joining_archers,
        joining_infantry=joining_infantry,
        joining_cavalry=joining_cavalry,
        calling_archers=calling_archers,
        calling_infantry=calling_infantry,
        calling_cavalry=calling_cavalry,
    )
