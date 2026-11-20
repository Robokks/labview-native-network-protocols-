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
    got = parse_record(bytes.fromhex("0600000077770000abcd"), 0)
    assert isinstance(got, nw.RawRecord)
    assert got == nw.RawRecord(0x7777, bytes.fromhex("abcd"))
    assert got.kind == 0x7777 and got.data == bytes.fromhex("abcd")


def test_undecodable_known_kind_is_raw_record():
    # a String record (0x1005) whose length field runs past the data: kept as RawRecord
    got = parse_record(bytes.fromhex("0600000005100000ff"), 0)
    assert isinstance(got, nw.RawRecord) and got.kind == 0x1005


def test_raw_record_is_frozen_and_hashable():
    import dataclasses
    r = nw.RawRecord(1, b"x")
    assert hash(r) == hash(nw.RawRecord(1, b"x"))
    try:
        r.kind = 2
        raise AssertionError("RawRecord must be frozen")
    except dataclasses.FrozenInstanceError:
        pass
    assert not isinstance(r, tuple)        # no longer the ("raw", kind, data) tuple
