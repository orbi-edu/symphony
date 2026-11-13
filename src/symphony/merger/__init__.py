"""symphony-merger: 100% agent review and a deterministic merge train (docs/merger.md).

Stage 1 (`review`) reviews each ready PR with reviewer lanes and independent oracles, typed
through omp host tools, and either hands it to the train or sends it back to its author. Stage 2
(`train`) rebases, waits for CI and merges, one step at a time behind a Redis lease. Everything a
crash could interrupt is a Redis record written before the effect (`store`).
"""
