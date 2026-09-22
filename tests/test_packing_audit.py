"""End-to-end tests for the /packing-audit endpoint."""

import json
from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

GS = "\x1d"
GTIN_RAW = "09501101530003"  # valid Mod-10 check digit
GTIN_PRODUCT = "00000000000017"  # second valid GTIN


def audit(containers, target_unit="kg"):
    return client.post(
        "/packing-audit",
        json={"target_unit": target_unit, "containers": containers},
    )


def container(container_id, max_weight, items=None, parent_id=None):
    body = {"container_id": container_id, "max_weight": max_weight, "items": items or []}
    if parent_id is not None:
        body["parent_id"] = parent_id
    return body


def item(serial, weight="3103002000", gtin=GTIN_RAW):
    """Element-string item scan with 01/21 and one weight AI (default 2 kg)."""
    return f"01{gtin}21{serial}{GS}{weight}"


def by_id(body):
    return {entry["container_id"]: entry for entry in body["containers"]}


# ---------------------------------------------------------------- happy path


def test_empty_audit_returns_empty_result():
    resp = audit([])
    assert resp.status_code == 200
    assert resp.json() == {"target_unit": "kg", "containers": []}


def test_single_container_direct_weights_and_counts():
    resp = audit(
        [
            container(
                "A",
                10,
                [
                    item("S1", "3103002000"),  # 2 kg
                    item("S2", "3103003000", gtin=GTIN_PRODUCT),  # 3 kg
                ],
            )
        ]
    )
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["container_id"] == "A"
    assert entry["parent_id"] is None
    assert entry["max_weight"] == 10.0
    assert entry["unit"] == "kg"
    assert entry["direct_weight"] == 5.0
    assert entry["total_weight"] == 5.0
    assert entry["remaining_capacity"] == 5.0
    assert entry["direct_item_count"] == 2
    assert entry["recursive_item_count"] == 2
    assert entry["over_limit"] is False


def test_leaf_to_root_rollup_keeps_request_order():
    # PAL (root) -> A -> B; declarations are roots first here.
    resp = audit(
        [
            container("PAL", 100, [item("P1", "3103006000")]),
            container(
                "A",
                100,
                [item("A1", "3103002000")],
                parent_id="PAL",
            ),
            container(
                "B",
                100,
                [item("B1", "3103001000", gtin=GTIN_PRODUCT)],
                parent_id="A",
            ),
        ]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [c["container_id"] for c in body["containers"]] == ["PAL", "A", "B"]
    entries = by_id(body)
    assert entries["B"]["total_weight"] == 1.0
    assert entries["B"]["recursive_item_count"] == 1
    assert entries["A"]["direct_weight"] == 2.0
    assert entries["A"]["total_weight"] == 3.0
    assert entries["A"]["recursive_item_count"] == 2
    assert entries["PAL"]["direct_weight"] == 6.0
    assert entries["PAL"]["total_weight"] == 9.0
    assert entries["PAL"]["recursive_item_count"] == 3
    assert all(c["over_limit"] is False for c in body["containers"])


def test_containers_declared_before_their_parents_still_aggregate():
    resp = audit(
        [
            container("B", 100, [item("B1", "3103001000")], parent_id="A"),
            container("A", 100, [item("A1", "3103002000")]),
        ]
    )
    assert resp.status_code == 200
    entries = by_id(resp.json())
    assert entries["A"]["total_weight"] == 3.0
    assert entries["A"]["recursive_item_count"] == 2
    assert entries["B"]["total_weight"] == 1.0
    assert entries["A"]["parent_id"] is None
    assert entries["B"]["parent_id"] == "A"


def test_forest_of_disjoint_roots_does_not_share_weight():
    resp = audit(
        [
            container("R1", 100, [item("S1", "3103001000")]),
            container("R2", 100, [item("S2", "3103001000")]),
            container("C2", 100, [item("S3", "3103001000")], parent_id="R2"),
        ]
    )
    assert resp.status_code == 200
    entries = by_id(resp.json())
    assert entries["R1"]["total_weight"] == 1.0
    assert entries["R1"]["recursive_item_count"] == 1
    assert entries["R2"]["total_weight"] == 2.0
    assert entries["R2"]["recursive_item_count"] == 2


def test_mixed_kg_and_lb_scans_convert_with_exact_factor():
    # 2 kg + 25.13 lb * 0.45359237 = 2 + 11.3987762581 = 13.3987762581
    resp = audit(
        [
            container(
                "A",
                100,
                [
                    item("S1", "3103002000"),
                    item("S2", "3202002513"),
                ],
            )
        ],
        target_unit="kg",
    )
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["direct_weight"] == 13.398776
    assert entry["total_weight"] == 13.398776
    assert entry["remaining_capacity"] == 86.601224


def test_target_unit_lb_converts_kilogram_items():
    # 1 kg = 1 / 0.45359237 = 2.204622621... lb
    resp = audit(
        [container("A", 10, [item("S1", "3103001000")])], target_unit="lb"
    )
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["unit"] == "lb"
    assert entry["direct_weight"] == 2.204623
    assert entry["total_weight"] == 2.204623
    assert entry["max_weight"] == 10.0


def test_weights_render_with_exactly_six_decimal_places():
    # 10 lb = 4.5359237 kg -> half-even 4.535924
    resp = audit([container("A", 100, [item("S1", "3200000010")])])
    assert resp.status_code == 200
    raw = resp.json()["containers"][0]
    assert raw["direct_weight"] == 4.535924
    text = resp.text
    assert '"direct_weight":4.535924' in text
    assert '"max_weight":100.000000' in text
    assert '"remaining_capacity":95.464076' in text


def test_over_limit_is_flagged_but_audit_still_returns_every_container():
    resp = audit(
        [
            container("PAL", 8, [item("P1", "3103002000")]),  # only own 2 kg
            container(
                "A",
                5,
                [
                    item("A1", "3103002000"),  # 2 kg
                    item("A2", "3103006000"),  # 6 kg -> 8 kg direct, over
                ],
                parent_id="PAL",
            ),
            container(
                "B",
                100,
                [item("B1", "3103001000")],  # 1 kg
                parent_id="A",
            ),
        ]
    )
    assert resp.status_code == 200
    entries = by_id(resp.json())
    # B is within its own capacity.
    assert entries["B"]["total_weight"] == 1.0
    assert entries["B"]["over_limit"] is False
    assert entries["B"]["remaining_capacity"] == 99.0
    # A holds 9 kg (8 direct + 1 child) against 5.
    assert entries["A"]["direct_weight"] == 8.0
    assert entries["A"]["total_weight"] == 9.0
    assert entries["A"]["over_limit"] is True
    assert entries["A"]["remaining_capacity"] == -4.0
    # PAL holds 11 kg (2 + 9) against 8 even though its own goods fit.
    assert entries["PAL"]["total_weight"] == 11.0
    assert entries["PAL"]["over_limit"] is True
    assert entries["PAL"]["remaining_capacity"] == -3.0


def test_exact_capacity_is_not_over_limit():
    resp = audit([container("A", 2, [item("S1", "3103002000")])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["over_limit"] is False
    assert entry["remaining_capacity"] == 0.0


def test_empty_containers_roll_up_zero_weight():
    resp = audit(
        [
            container("PAL", 10, []),
            container("A", 10, [], parent_id="PAL"),
        ]
    )
    assert resp.status_code == 200
    for entry in resp.json()["containers"]:
        assert entry["direct_weight"] == 0.0
        assert entry["total_weight"] == 0.0
        assert entry["remaining_capacity"] == 10.0
        assert entry["direct_item_count"] == 0
        assert entry["recursive_item_count"] == 0
        assert entry["over_limit"] is False


def test_digital_link_scans_are_accepted():
    scan = (
        "https://id.gs1.org/01/" + GTIN_RAW + "/21/SN0001?3103=002513"
    )  # 2.513 kg
    resp = audit([container("A", 10, [scan])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["direct_weight"] == 2.513
    assert entry["direct_item_count"] == 1


def test_string_max_weight_is_exact_decimal():
    resp = audit([container("A", "1.5", [item("S1", "3103002000")])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["max_weight"] == 1.5
    assert entry["over_limit"] is True
    assert entry["remaining_capacity"] == -0.5


# -------------------------------------------------------------- huge capacity


def test_huge_capacity_keeps_every_digit_of_remaining_capacity():
    # 1e60 overflows the fixed 50-digit arithmetic precision: the working
    # precision must follow the capacity's magnitude, not truncate it.
    resp = audit([container("BIG", "1e60", [item("S1", "3103002000")])])  # 2 kg
    assert resp.status_code == 200
    # every weight field is a fixed-point six-place JSON number, never an
    # exponent form, and the remainder keeps every digit of 1e60 - 2
    assert '"max_weight":1' + "0" * 60 + ".000000" in resp.text
    assert '"direct_weight":2.000000' in resp.text
    assert '"total_weight":2.000000' in resp.text
    assert '"remaining_capacity":' + "9" * 59 + "8.000000" in resp.text
    entry = resp.json()["containers"][0]
    assert entry["over_limit"] is False


def test_huge_capacity_round_trips_without_precision_loss():
    resp = audit([container("BIG", "1e60", [item("S1", "3103002000")])])
    assert resp.status_code == 200
    body = json.loads(resp.text, parse_float=Decimal)
    entry = body["containers"][0]
    assert entry["max_weight"] == Decimal("1E+60")
    assert entry["direct_weight"] == 2
    assert entry["total_weight"] == 2
    assert entry["remaining_capacity"] == Decimal("9" * 59 + "8")
    assert entry["over_limit"] is False


def test_huge_json_number_capacity_is_exact():
    # 1e60 as a JSON number must arrive as the exact Decimal, not float noise
    resp = client.post(
        "/packing-audit",
        json={
            "containers": [
                {
                    "container_id": "BIG",
                    "max_weight": 1e60,
                    "items": [item("S1", "3103002000")],
                }
            ]
        },
    )
    assert resp.status_code == 200
    assert '"max_weight":1' + "0" * 60 + ".000000" in resp.text
    assert '"remaining_capacity":' + "9" * 59 + "8.000000" in resp.text


def test_huge_capacity_empty_container_renders_exactly():
    resp = audit([container("BIG", "1e60")])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["direct_weight"] == 0.0
    assert entry["total_weight"] == 0.0
    assert entry["over_limit"] is False
    assert '"total_weight":0.000000' in resp.text
    assert '"max_weight":1' + "0" * 60 + ".000000" in resp.text
    assert '"remaining_capacity":1' + "0" * 60 + ".000000" in resp.text


def test_huge_capacity_rolls_up_nested_containers():
    resp = audit(
        [
            container("PAL", "1e60", [item("P1", "3103002000")]),  # 2 kg direct
            container(
                "A",
                100,
                [item("A1", "3103003000", gtin=GTIN_PRODUCT)],  # 3 kg
                parent_id="PAL",
            ),
        ]
    )
    assert resp.status_code == 200
    entries = by_id(resp.json())
    # the child's ordinary capacity behaves exactly as before
    assert entries["A"]["total_weight"] == 3.0
    assert entries["A"]["remaining_capacity"] == 97.0
    assert entries["A"]["over_limit"] is False
    # the root's remainder keeps every digit of 1e60 - (2 + 3)
    assert entries["PAL"]["total_weight"] == 5.0
    assert entries["PAL"]["recursive_item_count"] == 2
    assert entries["PAL"]["over_limit"] is False
    assert '"remaining_capacity":' + "9" * 59 + "5.000000" in resp.text
    assert '"remaining_capacity":97.000000' in resp.text


def test_over_limit_boundary_beyond_default_precision():
    # 51 significant digits: just below/above the 2 kg load at a digit the
    # fixed 50-digit arithmetic cannot see; the exact value decides.
    just_under = "1." + "9" * 50  # 2 - 1e-50
    just_over = "2." + "0" * 49 + "1"  # 2 + 1e-50
    resp = audit(
        [
            container("UNDER", just_under, [item("S1", "3103002000")]),
            container(
                "OVER", just_over, [item("S2", "3103002000", gtin=GTIN_PRODUCT)]
            ),
        ]
    )
    assert resp.status_code == 200
    entries = by_id(resp.json())
    assert entries["UNDER"]["over_limit"] is True
    assert entries["OVER"]["over_limit"] is False
    # both capacities render as 2.000000 at six places, but the remainders
    # keep their exact sign: -1e-50 -> -0.000000, +1e-50 -> 0.000000
    assert '"remaining_capacity":-0.000000' in resp.text
    body = json.loads(resp.text, parse_float=Decimal)
    exact = {c["container_id"]: c for c in body["containers"]}
    assert exact["UNDER"]["remaining_capacity"].is_signed()
    assert not exact["OVER"]["remaining_capacity"].is_signed()


# ------------------------------------------------------------ structural errors


def test_duplicate_container_id_is_rejected_with_first_index():
    resp = audit(
        [
            container("A", 10),
            container("B", 10),
            container("A", 10),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_CONTAINER"
    assert detail["container_index"] == 2
    assert detail["first_container_index"] == 0


def test_unknown_parent_is_rejected():
    resp = audit([container("A", 10, parent_id="GHOST")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "UNKNOWN_PARENT"
    assert detail["container_index"] == 0
    assert detail["error"]["found"] == "GHOST"


def test_self_parent_is_a_cycle():
    resp = audit([container("A", 10, parent_id="A")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CYCLE_DETECTED"
    assert detail["container_index"] == 0
    assert detail["cycle"] == [0]


def test_two_node_cycle_is_rejected():
    resp = audit(
        [
            container("A", 10, parent_id="B"),
            container("B", 10, parent_id="A"),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CYCLE_DETECTED"
    assert detail["cycle"] == [0, 1]
    assert detail["container_index"] == 0


def test_cycle_rotation_starts_at_lowest_index():
    resp = audit(
        [
            container("R", 10),
            container("A", 10, parent_id="C"),
            container("B", 10, parent_id="A"),
            container("C", 10, parent_id="B"),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CYCLE_DETECTED"
    assert detail["cycle"] == [1, 3, 2]
    assert detail["container_index"] == 1


def test_duplicate_item_across_containers_locates_both_occurrences():
    resp = audit(
        [
            container("A", 10, [item("SN1", "3103002000")]),
            container("B", 10, [item("SN1", "3103002000")]),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_ITEM"
    assert detail["container_index"] == 1
    assert detail["item_index"] == 0
    assert detail["first_container_index"] == 0
    assert detail["first_item_index"] == 0


def test_duplicate_item_within_one_container_is_rejected():
    resp = audit(
        [
            container(
                "A",
                10,
                [
                    item("SN1", "3103001000"),
                    item("SN2", "3103001000"),
                    item("SN1", "3103001000"),
                ],
            )
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_ITEM"
    assert detail["container_index"] == 0
    assert detail["item_index"] == 2
    assert detail["first_container_index"] == 0
    assert detail["first_item_index"] == 0


def test_same_serial_under_different_gtins_are_distinct_items():
    resp = audit(
        [
            container("A", 10, [item("SAME", "3103001000", gtin=GTIN_RAW)]),
            container("B", 10, [item("SAME", "3103001000", gtin=GTIN_PRODUCT)]),
        ]
    )
    assert resp.status_code == 200


# ----------------------------------------------------------------- scan errors


def test_parse_error_locates_container_and_item():
    resp = audit(
        [
            container(
                "A",
                10,
                [
                    item("S1", "3103001000"),
                    f"01{GTIN_RAW}21S2{GS}310300",  # truncated weight field
                ],
            )
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 0
    assert detail["item_index"] == 1
    assert detail["error"]["error"] == "FIELD_TOO_SHORT"


def test_missing_gtin_is_rejected_with_indices():
    resp = audit(
        [container("A", 10, [f"21SN0001{GS}3103002000"])]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 0
    assert detail["item_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "AI 01" in detail["error"]["message"]


def test_missing_serial_is_rejected_with_indices():
    resp = audit([container("A", 10, [f"01{GTIN_RAW}3103002000"])])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 0
    assert detail["item_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "AI 21" in detail["error"]["message"]


def test_missing_weight_is_rejected_with_indices():
    resp = audit([container("A", 10, [f"01{GTIN_RAW}21SN0001"])])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "MISSING_AI"
    assert "weight AI" in detail["error"]["message"]


def test_two_weight_ais_are_rejected():
    # Both weights are fixed-length fields, so they sit back to back
    # without a separator; the second 310n/320n must still be rejected.
    resp = audit(
        [
            container(
                "A",
                10,
                [f"01{GTIN_RAW}21S1{GS}31030020003202002000"],
            )
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert "3103, 3202" in detail["error"]["found"]


def test_bad_check_digit_locates_container_and_item():
    resp = audit(
        [
            container("A", 10),
            container(
                "B",
                10,
                [
                    item("OK", "3103001000"),
                    item("BAD", "3103001000", gtin="09501101530004"),
                ],
            ),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 1
    assert detail["item_index"] == 1
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"


def test_optional_batch_and_expiry_are_allowed():
    resp = audit(
        [
            container(
                "A",
                10,
                [
                    f"01{GTIN_RAW}1725013110B1{GS}21S1{GS}3103002000",
                ],
            )
        ]
    )
    assert resp.status_code == 200
    assert resp.json()["containers"][0]["direct_weight"] == 2.0


# --------------------------------------------------------------- request shape


def test_negative_max_weight_is_rejected_by_validation():
    resp = audit([container("A", -1)])
    assert resp.status_code == 422


def test_empty_container_id_is_rejected_by_validation():
    resp = audit([container("", 10)])
    assert resp.status_code == 422


def test_missing_items_field_is_rejected_by_validation():
    resp = client.post(
        "/packing-audit",
        json={"containers": [{"container_id": "A", "max_weight": 10}]},
    )
    assert resp.status_code == 422


def test_unknown_target_unit_is_rejected_by_validation():
    resp = client.post(
        "/packing-audit",
        json={
            "target_unit": "stone",
            "containers": [container("A", 10)],
        },
    )
    assert resp.status_code == 422
