from dataclasses import dataclass

from z3 import *


@dataclass
class Params:
    w2: int   # L2 ways (shared by both designs)
    w3: int   # NINE L3 ways
    v: int    # victim cache ways

    N: int    # trace length (bounded horizon)
    K: int    # number of distinct line labels available to the trace

    l2: int   # L2 lookup latency
    l3: int   # L3 / victim-cache lookup latency (equal by design choice)
    ld: int   # DRAM latency


def fresh_cache(name, num_ways, timestep):
    return [Int(f"{name}_t{timestep}_slot{i}") for i in range(num_ways)]


# Cold start: every slot holds a distinct negative sentinel, so no slot can
# match a real line label (which are non-negative) until something is inserted.
def init_empty(cache_state):
    return [slot == -(i + 1) for i, slot in enumerate(cache_state)]


# The trace is the only free variable Z3 searches over: each access is an
# unconstrained line label in [0, K). No assumption about workload shape.
def constrain_trace(access_sequence, K):
    return [And(0 <= access, access < K) for access in access_sequence]


def is_present(cache_state, line_label):
    return Or([slot == line_label for slot in cache_state])


def lru_line(cache_state):
    return cache_state[-1]


# Strict-LRU update: remove line_to_find if present, and place line_to_insert
# at the MRU position. If line_to_find is absent, the LRU entry is evicted.
def updated_cache(cache_state, line_to_find, line_to_insert):
    new_state = [line_to_insert]
    found_above = BoolVal(False)
    for k in range(1, len(cache_state)):
        found_above = Or(found_above, cache_state[k - 1] == line_to_find)
        new_state.append(If(found_above, cache_state[k], cache_state[k - 1]))
    return new_state
