"""Backchannel uses the same timed stimulus/recording engine as interruption."""

from benchmark.interruption import run_interruption_case


async def run_backchannel_case(factory, **kwargs):
    if kwargs["scenario"].category != "backchannel":
        raise ValueError("backchannel scenario required")
    return await run_interruption_case(factory, **kwargs)
