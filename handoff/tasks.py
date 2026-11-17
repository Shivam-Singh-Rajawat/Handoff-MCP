"""Small, synthetic Python data transformation bugs with held-out assertions."""
from dataclasses import dataclass
from textwrap import dedent


@dataclass(frozen=True)
class Task:
    name: str
    requirement: str
    buggy: str
    fixed: str
    examples: tuple


def task(name, requirement, buggy, fixed, *examples):
    return Task(name, requirement, dedent(buggy).strip(), dedent(fixed).strip(), examples)


TASKS = (
    task("zero-is-data", "Drop None, preserve all other values and their order.",
         "def transform(xs): return [x for x in xs if x]",
         "def transform(xs): return [x for x in xs if x is not None]",
         ([None, 0, 2], [0, 2]), (["", None, False], ["", False]), ([], [])),
    task("stable-dedup", "Deduplicate hashable values in first-seen order.",
         "def transform(xs): return sorted(set(xs))",
         "def transform(xs): return list(dict.fromkeys(xs))",
         (["z", "a", "z"], ["z", "a"]), ([2, 1, 2, 3], [2, 1, 3]), ([], [])),
    task("count-categories", "Return exact category counts as a dictionary.",
         "def transform(xs): return {x: 1 for x in xs}",
         """\
         def transform(xs):
             counts = {}
             for x in xs:
                 counts[x] = counts.get(x, 0) + 1
             return counts
         """,
         (["a", "a"], {"a": 2}), (["b", "a", "b"], {"b": 2, "a": 1}), ([], {})),
    task("column-aliases", "Strip surrounding whitespace and lowercase each header.",
         "def transform(xs): return [x.lower() for x in xs]",
         "def transform(xs): return [x.strip().lower() for x in xs]",
         ([" Name "], ["name"]), ([" A", "B\t", "c"], ["a", "b", "c"]), ([], [])),
    task("coalesce", "For each row pick 'value' if present and non-None, else 'fallback'.",
         "def transform(xs): return [r.get('value') or r['fallback'] for r in xs]",
         "def transform(xs): return [r['fallback'] if r.get('value') is None else r['value'] for r in xs]",
         ([{"value": 0, "fallback": 3}], [0]),
         ([{"fallback": 8}, {"value": None, "fallback": 9}], [8, 9]), ([], [])),
    task("sort-dates", "Sort unpadded M/D/YYYY strings chronologically, preserving original spelling.",
         "def transform(xs): return sorted(xs)",
         """\
         def transform(xs):
             from datetime import datetime
             return sorted(xs, key=lambda x: datetime.strptime(x, '%m/%d/%Y'))
         """,
         (["10/1/2024", "2/1/2024"], ["2/1/2024", "10/1/2024"]),
         (["1/1/2025", "12/31/2024", "12/01/2024"], ["12/01/2024", "12/31/2024", "1/1/2025"]),
         ([], [])),
    task("running-total", "Return a running sum including the current value.",
         "def transform(xs): return [sum(xs[:i]) for i in range(len(xs))]",
         """\
         def transform(xs):
             from itertools import accumulate
             return list(accumulate(xs))
         """,
         ([4], [4]), ([2, -3, 1], [2, -1, 0]), ([], [])),
    task("zip-columns", "Return paired values, padding the shorter column with None.",
         "def transform(xs): return list(zip(*xs))",
         """\
         def transform(xs):
             from itertools import zip_longest
             return list(zip_longest(*xs))
         """,
         (([1, 2], ["a"]), [(1, "a"), (2, None)]),
         (([], ["a", "b"]), [(None, "a"), (None, "b")]), (([], []), [])),
    task("percentage-change", "Return percentage change from old to new, or None when old is zero.",
         "def transform(xs): return [(new-old)/new*100 for old,new in xs]",
         "def transform(xs): return [None if old == 0 else (new-old)/old*100 for old,new in xs]",
         ([(2, 4)], [100.0]), ([(3, 0), (0, 7)], [-100.0, None]), ([], [])),
    task("inclusive-range", "Keep values between low and high inclusive, preserving order.",
         "def transform(xs): return [x for x in xs[0] if xs[1] < x < xs[2]]",
         "def transform(xs): return [x for x in xs[0] if xs[1] <= x <= xs[2]]",
         (([0, 1, 2, 3], 1, 2), [1, 2]), (([3, 3, 4], 3, 3), [3, 3]), (([], 1, 2), [])),
    task("last-record", "For each key, keep the last value from (key, value) pairs.",
         """\
         def transform(xs):
             latest = {}
             for key, value in xs:
                 latest.setdefault(key, value)
             return latest
         """,
         "def transform(xs): return dict(xs)",
         ([("a", 1), ("a", 2)], {"a": 2}),
         ([("a", 1), ("b", 2), ("a", None)], {"a": None, "b": 2}), ([], {})),
    task("complete-csv-fields", "Read one CSV record with standard CSV quoting and return its fields.",
         "def transform(xs): return xs.split(',')",
         """\
         def transform(xs):
             import csv
             return next(csv.reader([xs]))
         """,
         ('"last, first",7', ["last, first", "7"]),
         ('"a""b",,c', ['a"b', "", "c"]), ("a,b", ["a", "b"])),
)
