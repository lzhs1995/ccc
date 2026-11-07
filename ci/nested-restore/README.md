# Nested Herdr restore regression

A restored Herdr surface can lose its saved process identity during the interval before the new shell exposes the Herdr process. Its automatic restore selector then sees the nested agent record and rejects the mismatch. The patch preserves the approved outer identity through this startup interval and applies topology admission to non-agent startup input.

The CI lane applies a behavioral test commit to the fixed upstream revision before applying the repair commit. It requires all 11 selected cases to run: the old revision is expected to fail six cases, and the repaired revision must pass all 11. Six app boundary cases, four existing binding cases, and six package cases must also pass. Setup failures, skipped or missing tests, and interrupted runs do not establish regression proof.

After validation it retains a separately tagged, signed app built from the repaired source, with archive and source receipts. This lane does not install a client or prove complete nine-session cold recovery, real provider login, or physical power-loss recovery. Those remain separate acceptance work. Failed original attempts are preserved and are not rerun.

The console wrapper is invoked with a subprocess-only source workspace. A real console-hop path preflight runs before compilation. The original regression/fix patches, typed result requirements and budgets remain unchanged.
