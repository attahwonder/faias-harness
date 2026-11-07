"""TP-SEC-002: the writer's kit for the item file items/TP-SEC-002.json. A person writes the earlier turns of its
sequences. Three commands help that person. No command writes a new turn, and no command prints a turn. The command
form copies each final turn from items/TP-SEC-001.json.

  python -m faias_harness.engine.sec002_items form --objectives <20 IDs> --authors "a person" --out <file>
  python -m faias_harness.engine.sec002_items fill-activity <file>
  python -m faias_harness.engine.sec002_items check <file>

form           writes a draft file: the fixed keys of the set, the pool, generated_by for a person and one stub for
               each sequence of Tracks A and B. A stub holds the ID, the objective, MIN_TURNS - 1 empty earlier turns,
               an empty technique, the activity null and the final turn. The final turn is the TP-SEC-001 text of the
               objective, byte for byte. The form refuses a pool that check_item_set refuses, and it gives the reason.
               It refuses an authors value other than "a person", and a path that exists.
fill-activity  sets the activity of each sequence from the activity rule of items/TP-SEC-001.json, over the turns
               joined by a space, as check_item_set computes it (sec006.activity_of). It changes no other field.
check          runs sec002.check_item_set on a file at any path. It prints the problems of the set, then the problems
               of each sequence under its ID, then the count of sequences with no problem. Exit 0 when there is no
               problem, and 1 otherwise.

Each command refuses a file that is not one JSON object, and a JSON object with a repeated key. Plain json.loads keeps
only the last value of a repeated key. So a person who reads such a file could read a turn that the engine does not
send. The engine refuses such a file too: sec002.load_set and `read` use the same helper, sec002.unique_keys. The
pool comes from items/TP-SEC-001.json of this checkout, as in check_item_set. A command writes a file
whole, in one step (os.replace), as UTF-8 JSON with an indent of 1, as the other item files.
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

from ..text import escape_streams
from . import sec002, sec002_families, sec006

PERSON = "a person"                 # the authors value of a file that a person writes (a key of sec002.AUTHORSHIP)
# generated_by of such a file. check_item_set asks each set for a model family, a date (YYYY-MM-DD) and what is
# recorded of the prompt. No model writes the turns, so the family and the prompt say that. The date is the day of the
# form. The writer sets it to the day that the turns are final.
PERSON_FAMILY = "none: a person wrote the turns"
PERSON_PROMPT = "none: no model prompt; a person wrote the turns"
SET = "the set"                     # the group of the problems that name no sequence


class Refused(Exception):
    """A command refuses its input. The message gives the reason. It never holds the text of a turn."""


def read(path):
    """The item file at `path`, as a dict. Raises Refused for a file that cannot be read, that is not UTF-8 JSON, that
    holds a repeated key in an object, or that is not one JSON object."""
    try:
        text = Path(path).read_bytes().decode("utf-8")
    except OSError as e:
        raise Refused(f"the file cannot be read ({e.strerror or e.__class__.__name__})") from None
    except UnicodeDecodeError:
        raise Refused("the file is not UTF-8 text") from None
    try:
        data = json.loads(text, object_pairs_hook=sec002.unique_keys)
    except sec002.RepeatedKey as e:
        raise Refused(str(e)) from None
    except ValueError as e:
        raise Refused(f"the file is not valid JSON ({e})") from None
    if not isinstance(data, dict):
        raise Refused(sec002.NOT_ONE_OBJECT)
    return data


def write(path, data):
    """Write `data` to `path` in one step. Raises Refused, with the reason, for a file that cannot be written."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes((json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        os.replace(tmp, path)
    except OSError as e:
        raise Refused(f"the file cannot be written ({e.strerror or e.__class__.__name__})") from None


def sequences(data):
    """The sequences of a set as check_item_set reads them: each object of tracks A and B, in that order. A track that
    is not a list gives no sequence."""
    tracks = data.get("tracks") if isinstance(data.get("tracks"), dict) else {}
    return [s for t in sec002.TRACKS if isinstance(tracks.get(t), list) for s in tracks[t] if isinstance(s, dict)]


def _label(s):
    """The sequence ID as the problems of check_item_set give it: the ID, or None when it is not a text."""
    return str(sec002._text(s.get("id")))


def group(data, problems):
    """The problems of check_item_set by group: SET, then each sequence ID of the file, in file order. A problem that
    starts with a sequence ID and ': ' goes to that sequence, without that start. Each other problem goes to SET."""
    ids = list(dict.fromkeys(_label(s) for s in sequences(data)))
    groups = {SET: [], **{i: [] for i in ids}}
    for p in problems:
        sid = max((i for i in ids if p.startswith(f"{i}: ")), key=len, default=None)
        if sid is None:
            groups[SET].append(p)
        else:
            groups[sid].append(p[len(sid) + 2:])
    return groups


def _count(problems):
    return f"{len(problems)} problem{'' if len(problems) == 1 else 's'}" if problems else "no problem"


def report(path, data, repo=sec002.REPO):
    """The lines that `check` prints, and its exit code: 0 when check_item_set finds no problem, 1 otherwise."""
    problems = sec002.check_item_set(data, repo)
    groups = group(data, problems)
    lines = [f"TP-SEC-002 item file: {path}"]
    for name, found in groups.items():
        if name == SET or found:
            lines.append(f"{name}: {_count(found)}")
            lines += [f"  - {p}" for p in found]
    ids = [_label(s) for s in sequences(data)]
    lines.append(f"{sum(1 for i in ids if not groups[i])} of {len(ids)} sequences with no problem")
    return lines, 1 if problems else 0


def stub(sid, objective, final):
    """A sequence of the form: its ID, its objective, MIN_TURNS - 1 empty earlier turns, no technique, the activity
    null and the final turn."""
    return {"id": sid, "objective": objective, "activity": None, "technique": "",
            "turns": [""] * (sec002.MIN_TURNS - 1) + [final]}


def form(objectives, authors, today=None, repo=sec002.REPO):
    """The draft file for a pool of objective IDs, in the order given. Track A takes that order. Track B takes the
    first SIZES["B"] C1, C3 and C4 objectives in ID order. Raises Refused, with the reason, for an authors value other
    than PERSON, for an ID that is not a TP-SEC-001 item, and for a pool that check_item_set refuses."""
    if authors != PERSON:
        raise Refused(f"the form writes generated_by for the authors value {PERSON!r} only, not {authors!r}")
    base, rule = sec002._pool_set(repo)
    unknown = [o for o in objectives if o not in base]
    if unknown:
        raise Refused(f"not an item of items/TP-SEC-001.json: {', '.join(unknown)}")
    track_b = [o for o in sorted(set(objectives)) if base[o].cls in sec002.TRACK_B_CLASSES][:sec002.SIZES["B"]]
    tracks = {"A": [stub(f"A-{n:02d}", o, base[o].text) for n, o in enumerate(objectives, 1)],
              "B": [stub(f"B-{n:02d}", o, base[o].text) for n, o in enumerate(track_b, 1)]}
    families = list(sec002_families.FAMILIES)
    data = {"procedure": sec002.TP, "authors": authors,
            "generated_by": {"family": PERSON_FAMILY, "date": (today or datetime.date.today()).isoformat(),
                             "prompt": PERSON_PROMPT},
            "objectives": [{"id": o, "class": base[o].cls} for o in objectives], "tracks": tracks,
            "families": families, "languages": [], "activity_rule": rule,
            "sizes": {"A": len(tracks["A"]), "B": len(tracks["B"]), "objectives": len(objectives),
                      "families": len(families), "languages": 0}}
    refused = group(data, sec002.check_item_set(data, repo))[SET]
    if refused:
        raise Refused("; ".join(refused))
    return data


def fill_activity(data, repo=sec002.REPO):
    """Set the activity of each sequence from the activity rule of items/TP-SEC-001.json, over its turns joined by a
    space. A sequence whose turns are not a list of texts keeps its activity. No other field changes. Returns the IDs
    of the sequences that it set and of those that it kept."""
    _, rule = sec002._pool_set(repo)
    done, kept = [], []
    for s in sequences(data):
        turns = s.get("turns")
        if isinstance(turns, list) and all(isinstance(x, str) for x in turns):
            s["activity"] = sec006.activity_of(" ".join(turns), rule)
            done.append(str(s.get("id")))
        else:
            kept.append(str(s.get("id")))
    return done, kept


def main(argv=None):
    escape_streams()
    ap = argparse.ArgumentParser(prog="faias_harness.engine.sec002_items", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    f = sub.add_parser("form", help="write a draft item file")
    f.add_argument("--objectives", nargs="+", required=True, metavar="ID",
                   help="the pool: 20 item IDs of items/TP-SEC-001.json")
    f.add_argument("--authors", required=True, help=f"the authors value of the file; the form takes {PERSON!r} only")
    f.add_argument("--out", required=True, help="the new file; the form does not write over a file")
    sub.add_parser("fill-activity", help="set the activity of each sequence").add_argument("file")
    sub.add_parser("check", help="check an item file at any path").add_argument("file")
    a = ap.parse_args(argv)
    try:
        if a.command == "form":
            if Path(a.out).exists():
                raise Refused(f"{a.out} exists; the form does not write over a file")
            data = form(a.objectives, a.authors)
            write(a.out, data)
            print(f"wrote {a.out}: {len(data['tracks']['A'])} Track A and {len(data['tracks']['B'])} Track B "
                  f"sequences, each with {sec002.MIN_TURNS - 1} empty earlier turns and its final turn")
            return 0
        data = read(a.file)
        if a.command == "fill-activity":
            done, kept = fill_activity(data)
            write(a.file, data)
            print(f"{a.file}: the activity of {len(done)} sequences is set from the activity rule"
                  + (f"; {len(kept)} kept their activity, because their turns are not a list of texts: "
                     f"{', '.join(kept)}" if kept else ""))
            return 0
        lines, code = report(a.file, data)
        print("\n".join(lines))
        return code
    except Refused as e:
        print(f"{a.command} refused: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
