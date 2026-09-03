"""Spreadsheet ingest: the operator's intent, validated before anything touches the network."""

from anzorlist.ingest.row import ListingRow
from anzorlist.ingest.schema import COLUMNS, RowError
from anzorlist.ingest.workbook import ReadResult, read_workbook, write_template

__all__ = [
    "COLUMNS",
    "ListingRow",
    "ReadResult",
    "RowError",
    "read_workbook",
    "write_template",
]
