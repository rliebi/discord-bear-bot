from dataclasses import dataclass
from typing import Tuple, Optional


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


def compute_kingshot(
    g: GuildConfig,
    total_archers: int,
    march_count: int,
    is_caller: bool,
    override_march_archers: Optional[int] = None,
    total_march_size: Optional[int] = None,
) -> MarchResult:
    if march_count <= 0:
        raise ValueError("march_count must be > 0")
    
    # Use override or server settings for max troop size
    max_troop_size = total_march_size if total_march_size is not None else g.max_troop_size
    if max_troop_size <= 0:
        raise ValueError("Server Max Troop Size not configured")
    if g.infantry_amount < 0 or g.max_archers_amount < 0:
        raise ValueError("Invalid server settings")

    # Per-joiner archer value baseline
    divisor = march_count + (1 if is_caller else 0)
    base = total_archers // max(1, divisor)
    
    if override_march_archers is not None:
        capped = override_march_archers
    else:
        capped = min(base, g.max_archers_amount)
        
    # Rounding rule: only round down to nearest 1000 if the user IS the caller.
    # If not calling, do not round the joining march archers.
    caller_archer_value = ((capped // 1000) * 1000) if is_caller else capped

    # Joining march values (rounded to 1000 only when calling)
    joining_archers = caller_archer_value
    joining_infantry = g.infantry_amount
    joining_cavalry = max(0, max_troop_size - joining_archers - joining_infantry)

    # Calling march values (no 1000 rounding on caller march)
    if is_caller:
        # Use the rounded joining value for remaining archers calc
        remaining_archers = total_archers - (caller_archer_value * march_count)
        remaining_archers = max(0, remaining_archers)
        calling_infantry = g.infantry_amount
        # Fit archers then cav into max troop size
        max_archers_slot = max(0, max_troop_size - calling_infantry)
        calling_archers = min(remaining_archers, max_archers_slot)
        calling_cavalry = max(0, max_troop_size - calling_infantry - calling_archers)
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
