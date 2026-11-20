"""Shared Variable value records captured from LabVIEW 2026 (library 'TypeTest Variables',
written by TypeTest SV Writer.vi, 2026-11-20): each decodes to the known value and
re-encodes byte for byte."""
import json
from pathlib import Path

import pytest

import nwstreams as nw
from nwstreams.sharedvariable import encode_record, parse_record

from type_rows import ROWS, same

RECORDS = {r["name"]: bytes.fromhex(r["raw"])
           for r in json.loads((Path(__file__).parent / "fixtures" / "sv_type_records.json").read_text())}


@pytest.mark.parametrize("name,dtype,value", ROWS, ids=[r[0] for r in ROWS])
def test_record_decodes_and_reencodes(name, dtype, value):
    raw = RECORDS[name]
    got = parse_record(raw, 0)
    assert same(got, value), got
    assert encode_record(value, dtype) == raw


def test_ints_come_back_as_int():
    assert type(parse_record(RECORDS["i16"], 0)) is int
    assert type(parse_record(RECORDS["sgl"], 0)) is float


def test_unknown_kind_stays_raw():
    assert parse_record(bytes.fromhex("0600000077770000abcd"), 0)[0] == "raw"
