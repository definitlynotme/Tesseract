"""Archived Claude decision for test-only paired policy comparisons.

Captured from the September 18 change; not a production resource policy.
"""


def claude_zero_run_is_conclusive(fast_completions: int, fast_net_new: int,
                                 deep_completions: int) -> bool:
    if fast_completions < 32:
        return True
    if fast_net_new <= 0:
        return False
    return fast_net_new / fast_completions * deep_completions >= 3.0
