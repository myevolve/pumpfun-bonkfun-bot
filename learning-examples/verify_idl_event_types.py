# ruff: noqa: S101, SLF001, PLR2004
"""Offline regression for native event alignment and bounded Borsh decoding.

Run: uv run --no-sync learning-examples/verify_idl_event_types.py
Uses only the retained Geyser receipt and local IDLs. No network or wallet calls.
"""

import base64
import hashlib
import json
import struct
import sys
from pathlib import Path

import base58

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from geyser.generated import geyser_pb2  # noqa: E402
from utils.idl_parser import IDLParser  # noqa: E402


def check_native_receipt() -> None:
    fixture = json.loads(
        (
            ROOT
            / "learning-examples/token-lifecycles/raw_trade_receipt_from_geyser.json"
        ).read_text()
    )
    raw = base64.b64decode(fixture["protobuf_base64"], validate=True)
    assert hashlib.sha256(raw).hexdigest() == fixture["raw_sha256"]
    update = geyser_pb2.SubscribeUpdate.FromString(raw)
    assert update.transaction.slot == fixture["slot"]
    assert (
        base58.b58encode(update.transaction.transaction.signature).decode()
        == fixture["signature"]
    )
    logs = update.transaction.transaction.meta.log_messages
    for idl_name, event_name, expected in (
        (
            "pump_fun_idl.json",
            "TradeEvent",
            {
                "shareholders": [],
                "quote_mint": "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",
                "quote_amount": 2237729961,
                "virtual_quote_reserves": 1630816618814,
                "real_quote_reserves": 632677083930,
            },
        ),
        (
            "pump_swap_idl.json",
            "SellEvent",
            {
                "quote_amount_out": 7763353,
                "virtual_quote_reserves": 0,
                "can_boost": False,
                "base_supply": 834226443590011857,
            },
        ),
    ):
        parser = IDLParser(str(ROOT / "idl" / idl_name))
        discriminator = parser.get_event_discriminators()[event_name]
        payloads = [
            base64.b64decode(log.removeprefix("Program data: "), validate=True)
            for log in logs
            if log.startswith("Program data: ")
        ]
        (data,) = [payload for payload in payloads if payload[:8] == discriminator]
        decoded = parser.decode_event_data(data, event_name)
        assert decoded is not None
        fields = decoded["fields"]
        assert set(fields) == {
            field["name"] for field in parser.types[event_name]["type"]["fields"]
        }, fields
        assert {name: fields[name] for name in expected} == expected, fields
        if event_name == "TradeEvent":
            # Independently located native vector prefix: byte 299, then pubkey + 3 u64s.
            assert struct.unpack_from("<I", data, 299)[0] == 0
            assert struct.unpack_from("<QQQ", data, 335) == (
                2237729961,
                1630816618814,
                632677083930,
            )
            shareholder = data[303:335] + struct.pack("<H", 10000)
            nonempty = data[:299] + struct.pack("<I", 1) + shareholder + data[303:]
            expected_fields = fields | {
                "shareholders": [
                    {"address": expected["quote_mint"], "share_bps": 10000}
                ]
            }
            assert (
                parser.decode_event_data(nonempty, event_name)["fields"]
                == expected_fields
            )
        else:
            # i128 at byte 392, bool at 408, u64 at 409; extra wire tails are permitted.
            assert data[392:408] == bytes(16)
            assert struct.unpack_from("<Q", data, 409)[0] == expected["base_supply"]
        print(f"PASS native {event_name}: {expected}")


def check_boundaries() -> None:
    parser = IDLParser(str(ROOT / "idl/pump_fun_idl.json"))
    discriminator = b"BOUNDARY"
    signed = [-(1 << 100) + 7, (1 << 100) + 9]
    unsigned = (1 << 128) - 1
    pieces = [
        ("prefix", "u8", b"\x07", 7),
        (
            "values",
            {"vec": "i128"},
            struct.pack("<I", 2)
            + b"".join(value.to_bytes(16, "little", signed=True) for value in signed),
            signed,
        ),
        ("unsigned", "u128", unsigned.to_bytes(16, "little"), unsigned),
        ("label", "string", struct.pack("<I", 3) + b"abc", "abc"),
        ("owner", "pubkey", bytes(32), "11111111111111111111111111111111"),
        ("after", "u8", b"\x09", 9),
    ]
    fields = [{"name": name, "type": typ} for name, typ, _, _ in pieces]
    parser.events[discriminator] = {"name": "Boundary"}
    parser.types["Boundary"] = {"type": {"kind": "struct", "fields": fields}}
    wire = discriminator + b"".join(encoded for _, _, encoded, _ in pieces)
    ends, end = {}, len(discriminator)
    for name, _, encoded, value in pieces:
        end += len(encoded)
        ends[name] = (end, value)
    # Every truncation must return only fully decoded fields, never a later value.
    for length in range(8, len(wire) + 1):
        expected = {name: value for name, (end, value) in ends.items() if end <= length}
        assert parser.decode_event_data(wire[:length])["fields"] == expected, length
    assert parser._calculate_type_min_size({"vec": "i128"}) == 4
    assert parser._calculate_type_min_size({"array": [{"vec": "u128"}, 2]}) == 8
    assert parser._calculate_type_min_size("i128") == 16
    assert parser._calculate_type_min_size("u128") == 16

    # Unsupported types and zero-progress vectors must stop before the next field.
    for bad_type, encoded in (
        ("unsupported", b"\x00\x09"),
        ({"vec": {"array": ["u8", 0]}}, struct.pack("<I", 1) + b"\x09"),
        ({"vec": "u8"}, struct.pack("<I", 0xFFFFFFFF) + b"\x09"),
    ):
        fields[1]["type"] = bad_type
        assert parser.decode_event_data(discriminator + b"\x07" + encoded)[
            "fields"
        ] == {"prefix": 7}
    print(
        "PASS exact 128-bit values, vector bounds, minimum sizes and every field truncation"
    )


if __name__ == "__main__":
    check_native_receipt()
    check_boundaries()
