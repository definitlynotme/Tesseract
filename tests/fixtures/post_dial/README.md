# Post-dial observer fixtures

Small, real fixtures for `tests/test_runtime_post_dial.py` and
`tests/test_runtime_node_judge.py` (node-judgement design v3, P1-2 and P2-1).
They were cut from the fallback-harness runs of 2026-09-26
(`%TEMP%/claude/fallback-harness/runs/`, real Mihomo v1.19.27, fake SOCKS5
nodes, headless Edge) by `build_fixtures.py` in this directory:

    .venv/Scripts/python.exe tests/fixtures/post_dial/build_fixtures.py [<runs_dir> [<out_dir>]]

Every file is gzip-compressed JSON and at most 150 KB.  The builder asserts
the size limit.

`replay.py` holds the fake-clock helpers the tests share: a fake
`/connections` source, the driver that runs the §18.4 loop body with the
observer core's own poll schedule, and the fixture loaders.  It is loaded by
path because `tests/fixtures` is not a package.  Nothing in it sleeps, opens a
socket or starts a thread.

## Hostnames and privacy

Only public hostnames of the harness test targets (listed in
`PUBLIC_TARGETS` in the builder, for example `go.dev` and `www.google.com`),
RFC 2606 `.test` names and documentation addresses remain.  Every other
destination host in the V-proto runs (Edge background and sub-resource hosts)
was replaced by a stable alias `host-NNN.invalid` before anything was written.
The aliasing keeps token equality classes intact.  Leaf names are the fake
nodes (`n1`..`n3`, `A`..`C`) or the conformance test names.  No HMAC key from a
run is stored.  The builder uses each run's `eval_hmac_key.hex` only in memory,
to map the prototype's tokens to lines.

## Files

### `conformance_lines.json.gz`

All 703 INFO `[TCP]` stdout lines captured by the conformance runs
(`runs/conformance/*/logs/mihomo_stdout.jsonl`, excluding the `invalid-*`
runs).  These are the lines behind Conf-2 (698 lines at the time, 0 failures
for the Conf-2 regex).  They cover:

- names with CJK, emoji, ZWJ, `"`, `\`, TAB, `[..]` and `(..)`;
- a process suffix whose name contains parentheses;
- system rules as Mihomo logs them (`Domain(h)`, `IPCIDR(a/n)`, IP-CIDR6 as `IPCIDR`);
- DIRECT, REJECT and GLOBAL modes;
- private `source-*` listeners (`using FINAL-EXIT[leaf]` with no `match`);
- IPv6 destinations;
- the Mihomo-internal `[TCP] mihomo --> ...` line.

Shape: `{"source": str, "lines": [raw logrus line, ...]}`.

### `x1_R1.json.gz` .. `x1_R5.json.gz`

X1 runs R1–R5 (`X1-zombie-signals/<run>`), in the order of the facts table:

| Tag | Run |
|---|---|
| R1 | `info-google-20260926-072140` |
| R2 | `info-google-d400-20260926-072747` |
| R3 | `debug-google-20260926-073408` |
| R4 | `info-cpcf-d400-20260926-074043` |
| R5 | `local-slow40-d80-20260926-074904` |

Each file contains:

- `lines`: every stdout line with `msg="[TCP] "` (INFO opens and the dial-error warnings), as `[receive_ts, raw_line]`;
- `conns` and `polls`: the real `poll_100` `/connections` recorder, compacted.
  - `conns[i] = [sourcePort, host, destinationPort, chains[0], start]` (`start` is Mihomo's original RFC3339 string).
  - `polls[k] = [t0_offset_in_0.1ms, [[conn_index, upload, download], ...]]`.
  - `replay.x1_snapshots()` rebuilds `/connections` bodies from these.
- `rows`: the X1 ground truth, `[line_index, category]` per client request (from `x1_rows.jsonl`); `line_index` is -1 when the request never produced an INFO line.
- `expected`: the X1 table cell `poll_100, X=0, T=1.0` (tp, fp, fn, precision, recall), from `x1_analysis.json`.

The test replays every recorded poll (continuous 100 ms) and checks two things:

- The §3.2 classifier without its poll-gap guard (FAILED_FAST plus UNKNOWN that would have been FAILED_FAST) reproduces the table within ±0.02.
- The first FAST trigger reproduces the X1 "2 in 1 s" latency and never fires on a healthy leaf.

### `vproto_<tag>.json.gz`

One file per V-proto run (`V-proto/<run>`, reps selected by a directory glob):

| Tag | Run | Reps | Scenario |
|---|---|---|---|
| P1 | `P1-20260926-113522` | 6 | fast zombie |
| P3 | `P3-20260926-114633` | 6 | black hole |
| P5 | `P5-20260926-124303` | 5 × 10 min | healthy browsing |
| P4 | `P4-20260926-115230` | 10 (`freeze-vid2-{del,put}`) | frozen h2 session, new tunnels black-holed |
| P6 | `P6-20260926-120007` | 5 (`P6stall`) | every node stalls (facility) |
| P6b | `P6-b-20260926-120711` | 3 `P6down` + 3 `P6zombie` | every node down / every node a zombie |
| P7 | `P7-20260926-121556` | 5 `P7fin` + 5 `P7stall` | one site down on every node (FIN at greeting / stall) |
| IDLE | `IDLE-20260926-122913` | 3 | 3 min idle, then a zombie |

The P7fin reps ran with the prototype's P3 destination probes (`A_D0`,
`S_D0`); the tests use only their contract probes.  The prototype's
`STALLED` outcomes (P4, one P7fin line) are not kept: v3 replaced that rule
with the WAITING shadow candidate (prototype deviation 4).

**The V-proto harness recorded no `/connections` polls.**  The prototype
polled on its own schedule and logged only its conclusions.  The Mihomo
connection state is therefore reconstructed from the fake nodes' own
per-connection records (`harness/logs/nodes/*.jsonl`):

- Each INFO line is joined to the node connection on the same leaf with the same `target`, whose `open` event is nearest the line time (−0.05..+0.5 s, each node connection used once).
- Presence runs from the line time until the node's `close` time + 1 ms.  Connections still open at the end stay present.
- Upload ramps linearly from `first_up_ms` to `eof_up_ms` up to `bytes_up + dropped_up`.  Mihomo counts what it wrote to the node, including bytes the stall and zombie modes read and dropped.
- The SNI-inspecting modes (`zombie_*`, `zombie_deny`) read the ClientHello (`first_bytes_len`) without counting it in either field.  When both are 0, upload is a step of `first_bytes_len` at `first_up_ms`, since Mihomo wrote those bytes.  Without this, the P7stall connections read as upload 0 and were never AGED (v3's AGED requires upload > 0; the prototype's did not).  The step changed `vproto_P1` (41 `zombie_fin` records) but none of its outcomes.
- Download ramps from `first_down_ms` to `eof_down_ms` up to `bytes_down`.

Per rep:

- `lines[i] = [receive_ts, raw_line, schedule, prototype_outcome]`.
  - `schedule = [t_close, t_first_up, t_eof_up, bytes_up, t_first_down, t_eof_down, bytes_down]`.
  - `prototype_outcome` is the prototype's non-legacy `outcome` (`FAILED_FAST` / `AGED`) for that line's open, mapped by (leaf, HMAC token under the run's key, receive time within 0.25 s).
- `proto` holds the prototype's `judge.jsonl` facts: `triggers`, `probe_starts`, `probe_results` (name, leaf, class, ms, ts), `judge_state`, `verdicts`, `n_outcomes`, and `outcomes_unmatched` (empty for every rep).
- `members`, `t_attach`, `t_exit` and `t_fault` come from the prototype and the run metadata.

The tests drive the observer core with its own schedule over this state and
check these things:

- The FAILED_FAST/AGED set per line equals the prototype's in 48 of 49 reps.  The one exception is in P6zombie-r0: a connection whose first download came 3.039 s after its open.  The prototype's +3.02 s poll saw 0 bytes, but the replay's poll landed at +3.05 s (after the last fast poll plus POLL_FAST_S).  The test allows only that poll-phase race.
- The first act trigger has the prototype's kind (FAST; BLACKHOLE becomes AGED), leaf and time (±0.1 s).  P5 and P6down have no trigger, and the core gives none.
- P1 and P3 replay identically with the perf clock on its own epoch (perf ≠ wall).
- The §18.6 scheduler is fed the prototype's measured probe classes and durations.  A probe the prototype never ran stays unanswered.  The v3 verdicts are:
  - P1, P3, P4, IDLE: CONVICT_CONTRACT on `n2`.  In P3 and P4, S_C0 now starts at +2.0 s rather than at t0, and the verdict still lands at A_C's 5.0 s reply.
  - P6: INDETERMINATE at the evidence deadline, then a late S_C0 timeout sets the FACILITY hold (PF7).
  - P6zombie: EXHAUSTED with two probes (the cap is 1 for three members).
  - P7: NOT_REPRODUCED, with one probe when A_C answers inside 2.0 s.
- PF6 on P7fin: exonerating the first trigger's tokens keeps the second burst from being judged.  The result is one probe per rep, where the prototype judged twice.

Because presence and byte timing are reconstructed, these fixtures fix the
classifier's endings (vanished / aged / answered).  They are not evidence for
byte-pattern rules (WAITING, stream watch), so P4's frozen session is not
checked for WAITING closes.
