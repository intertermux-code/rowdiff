"""Report rendering: human readable text and the machine readable drift report.

The JSON document is the contract for CI gates; the text renderer is a view of
the same dictionary, so the two can never disagree.
"""

from __future__ import annotations

import json

from .engine import ERROR, MISMATCHED

INDENT = "    "


def render_json(report, indent=2):
    return json.dumps(report, indent=indent, sort_keys=False, default=str)


def render_text(report):
    lines = []
    lines.append("%s.%s" % (report["left"]["table"],
                            ",".join(report["left"]["primary_key"])))
    lines.append("  left   %s" % _side_line(report["left"]))
    lines.append("  right  %s" % _side_line(report["right"]))
    lines.append("")

    chunks = report["summary"]["chunks"]
    rows = report["summary"]["rows"]
    lines.append("chunks   %d planned, %d compared, %d cached"
                 % (chunks["planned"], chunks["compared"], chunks["cached"]))
    lines.append("         %d matched, %d mismatched, %d errors"
                 % (chunks["matched"], chunks["mismatched"], chunks["errors"]))
    lines.append("rows     %d left, %d right"
                 % (rows["left_total"], rows["right_total"]))
    lines.append("drift    %d added, %d removed, %d changed"
                 % (rows["added"], rows["removed"], rows["changed"]))
    lines.append("status   %s (%.3fs)"
                 % (report["status"], report["duration_seconds"]))

    interesting = [c for c in report["chunks"]
                   if c["status"] in (MISMATCHED, ERROR)]
    if interesting:
        lines.append("")
        lines.append("chunks with differences")
        for chunk in interesting:
            lines.extend(_chunk_lines(chunk, report))
    return "\n".join(lines)


def _side_line(side):
    return "%s  %s  %d columns" % (side["source"], side["table"],
                                   len(side["columns"]))


def _chunk_lines(chunk, report):
    pk = report["left"]["primary_key"]
    out = ["  %s chunk %d  %s" % (chunk["status"].upper(), chunk["index"],
                                  _range_label(chunk["pk_range"], pk))]
    left, right = chunk["left"], chunk["right"]
    if chunk["status"] == ERROR:
        out.append(INDENT + "error: " + chunk["error"]["message"])
        return out
    out.append(INDENT + "left  %d rows  %s" % (left["row_count"],
                                               left["checksum"]))
    out.append(INDENT + "right %d rows  %s" % (right["row_count"],
                                               right["checksum"]))
    detail = chunk.get("row_diff") or {}
    for entry in detail.get("removed", []):
        out.append(INDENT + "- " + _pk_label(entry, pk))
    for entry in detail.get("added", []):
        out.append(INDENT + "+ " + _pk_label(entry, pk))
    for entry in detail.get("changed", []):
        columns = ", ".join("%s %r -> %r" % (c["name"], c["left"], c["right"])
                            for c in entry["columns"])
        out.append(INDENT + "~ %s  %s" % (_pk_label(entry["pk"], pk), columns))
    if detail.get("truncated"):
        out.append(INDENT + "(list truncated; counts above are complete,"
                                          " raise --max-diff-rows to see more)")
    if chunk["cached"]:
        out.append(INDENT + "(cached from a previous run)")
    return out


def _range_label(rng, pk):
    return "%s %s .. %s" % (",".join(pk), _fmt_values(rng["from"]),
                            _fmt_values(rng["to"]) if rng["to"] is not None
                            else "inf")


def _fmt_values(values):
    return "(" + ", ".join(_one(v) for v in values) + ")"


def _one(value):
    if isinstance(value, str):
        return value
    if value is None:
        return "NULL"
    return str(value)


def _pk_label(values, pk):
    if len(pk) == 1:
        return "%s=%s" % (pk[0], _one(values[0]))
    return "(" + ", ".join("%s=%s" % (n, _one(v)) for n, v in zip(pk, values)) + ")"
