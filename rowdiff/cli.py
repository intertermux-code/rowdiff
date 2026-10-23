"""``rowdiff`` command line."""

from __future__ import annotations

import argparse
import platform
import sys

from . import __version__
from .engine import DEFAULT_STATE_FILE, Options, run
from .errors import RowdiffError
from .report import render_json, render_text

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130

DESCRIPTION = """Compare two tables row by row without pulling them into \
Python: chunk both sides by primary key, let each database checksum its own \
chunks, and only read the rows of a chunk when its checksums disagree."""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="rowdiff", description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version",
                        version="rowdiff %s (Python %s)"
                                % (__version__, platform.python_version()))
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    diff = sub.add_parser("diff", help="compare two tables",
                          description=DESCRIPTION,
                          formatter_class=argparse.RawDescriptionHelpFormatter)
    diff.add_argument("--left", required=True, metavar="DSN",
                      help="left data source URI, e.g. sqlite:///warehouse_old.db")
    diff.add_argument("--right", required=True, metavar="DSN",
                      help="right data source URI")
    diff.add_argument("--table", metavar="NAME",
                      help="table name used for both sides")
    diff.add_argument("--left-table", metavar="NAME", help="left table name")
    diff.add_argument("--right-table", metavar="NAME", help="right table name")
    diff.add_argument("--pk", action="append", metavar="COLUMN",
                      help="primary key column, repeatable for composite keys"
                           " (default: the declared primary key)")
    diff.add_argument("--columns", action="append", metavar="COLUMN",
                      help="restrict the comparison to these columns,"
                           " repeatable (default: every column)")
    diff.add_argument("--chunk-size", type=int, default=5000, metavar="N",
                      help="distinct keys per chunk (default: 5000)")
    diff.add_argument("--where", metavar="PREDICATE",
                      help="raw SQL predicate applied to both sides"
                           " (e.g. \"tenant_id = 42\")")
    diff.add_argument("--algorithm", choices=("md5", "sha256"), default="md5",
                      help="digest used for chunk checksums (default: md5)")
    diff.add_argument("--state", default=DEFAULT_STATE_FILE, metavar="PATH",
                      help="SQLite file holding per chunk results"
                           " (default: %s)" % DEFAULT_STATE_FILE)
    diff.add_argument("--no-resume", action="store_false", dest="resume",
                      default=True, help="recompare every chunk instead of"
                                         " reusing finished ones")
    diff.add_argument("--no-pushdown", action="store_false", dest="pushdown",
                      default=True, help="checksum chunks client side, after"
                                         " fetching their rows")
    diff.add_argument("--max-diff-rows", type=int, default=50, metavar="N",
                      help="row details listed per mismatched chunk;"
                           " counts stay exact (default: 50)")
    diff.add_argument("--format", choices=("text", "json"), default="text",
                      help="output format (default: text)")
    diff.add_argument("--output", metavar="PATH",
                      help="write the report to PATH instead of stdout")
    diff.add_argument("--quiet", action="store_true",
                      help="do not report progress on stderr")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    left_table = args.left_table or args.table
    right_table = args.right_table or args.table
    if not left_table or not right_table:
        parser.error("give --table, or both --left-table and --right-table")

    try:
        options = Options(left=args.left, left_table=left_table,
                          right=args.right, right_table=right_table,
                          pk=args.pk, columns=args.columns,
                          chunk_size=args.chunk_size, algorithm=args.algorithm,
                          where=args.where,
                          state_file=args.state, resume=args.resume,
                          pushdown=args.pushdown,
                          max_diff_rows=args.max_diff_rows)
        report = run(options, progress=_progress(not args.quiet))
    except RowdiffError as exc:
        sys.stderr.write("rowdiff: %s\n" % exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        sys.stderr.write("rowdiff: interrupted; finished chunks stay in %s\n"
                         % args.state)
        return EXIT_INTERRUPTED

    text = (render_json(report) if args.format == "json"
            else render_text(report))
    if args.output:
        try:
            with open(args.output, "w") as handle:
                handle.write(text + "\n")
        except OSError as exc:
            sys.stderr.write("rowdiff: cannot write %s: %s\n"
                             % (args.output, exc))
            return EXIT_ERROR
    else:
        sys.stdout.write(text + "\n")
    if report["summary"]["chunks"]["errors"]:
        return EXIT_ERROR
    return EXIT_DRIFT if report["drift"] else EXIT_OK


def _progress(enabled):
    """One stderr line per chunk; stdout is reserved for the report."""
    if not enabled:
        return None

    def show(done, total, chunk):
        sys.stderr.write("chunk %d/%d %s\n" % (done, total, chunk["status"]))
        sys.stderr.flush()

    return show
