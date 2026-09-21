# -*- coding: utf-8 -*-
"""
DS3 Enemy Database Builder v7
============================

Construye un all_enemies.json completo a partir de tres piezas ya verificadas:

    all_enemies.json
        -> enemigos colocados en MSB3

    ds3_reference_index.json
        -> NPC_PARAM_ST
        -> NPC_THINK_PARAM_ST
        -> ITEMLOT_PARAM_ST
        -> rutas y referencias por enemigo

    data0_original.json
        -> metadatos/huella del Data0 original usado para producir el índice

Esta versión NO intenta redescubrir ParamDef ni reinterpretar el binario de
PARAM durante la composición final. El índice de referencia ya contiene las
filas resueltas y sus offsets; aquí solo se conectan las piezas de manera
estricta y se valida cada enlace antes de escribir el resultado.

Regla principal de seguridad:
    si las piezas no pertenecen exactamente al mismo snapshot, el proceso
    ABORTA y no reemplaza ningún JSON.

Salida:
    - all_enemies_complete.json (siempre)
    - all_enemies.json (solo con --replace; se conserva backup)
    - all_enemies_v7_report.txt

Uso:
    python ds3_enemy_database_builder_v7.py
    python ds3_enemy_database_builder_v7.py --game-root "D:\\...\\Game"
    python ds3_enemy_database_builder_v7.py --replace --game-root "D:\\...\\Game"

El script usa solamente la biblioteca estándar de Python.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import ds3_engine_config as engine_config


VERSION = "7.0-reference-composer"

PARTS_TYPE_ENEMY = 2

ITEM_CATEGORY_NAMES = {
    0xFFFFFFFF: "Empty",
    0x00000000: "Weapon",
    0x10000000: "Armor",
    0x20000000: "Ring",
    0x40000000: "Good",
}

EXPECTED_OUTPUT_TOP_LEVEL_FIELDS = [
    "Part_Name",
    "NPC_ID",
    "NPCParamID",
    "ThinkParamID",
    "TalkID",
    "Model_Name",
    "MSB_File_Offset",
    "Entity_Data_Offset",
    "Type_Data_Offset",
    "MSB_Part_Type",
    "MSB_Local_ID",
    "Model_Index",
    "Map_ID",
    "Map_File",
    "NPCParam",
    "ThinkParam",
    "Souls",
    "Drop_Tables",
]

NPC_FIELDS = [
    "BehaviorVariationId",
    "AiThinkId",
    "disableRespawn",
    "humanityLotId",
    "ItemLotId1",
    "ItemLotId2",
    "ItemLotId3",
    "ItemLotId4",
    "ItemLotId5",
    "ItemLotId6",
]

THINK_FIELDS = [
    "logicId",
    "battleGoalId",
    "nearDist",
    "midDist",
    "farDist",
]

ITEMLOT_SLOTS = range(1, 9)
NPC_LOT_SLOTS = range(1, 7)


class BuildError(RuntimeError):
    """Error that must stop the build before replacing any user file."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> Any:
    if not path.is_file():
        raise BuildError(f"Archivo no encontrado: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BuildError(f"JSON inválido: {path}: {exc}") from exc


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    temp.replace(path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def display_numeric(value: Any) -> Any:
    """Convención heredada de v6: None -> error, 0 -> Empty."""
    if value is None:
        return "Null - error"
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
        return "Empty"
    return value


def display_optional_id(value: Any) -> Any:
    if value is None:
        return "Null - error"
    if value in (0, -1):
        return "Empty"
    return value


def category_name(value: Any) -> str:
    if value is None:
        return "Null - error"
    return ITEM_CATEGORY_NAMES.get(value, "Unknown")


def enable_luck_from_raw(raw: Any, slot: int) -> Any:
    """
    ItemLotParam tiene ocho enableLuck como bitfields de un u16 compartido.
    enableLuck01 ocupa el bit 0, ..., enableLuck08 el bit 7.
    """
    if raw is None or not isinstance(raw, int):
        return "Null - error"
    return bool(raw & (1 << (slot - 1)))


def relative_chance(base_points: list[Any], slot_index: int) -> Any:
    if slot_index < 0 or slot_index >= len(base_points):
        return "Null - error"

    current = base_points[slot_index]
    if not isinstance(current, int) or current <= 0:
        return "Empty"

    total = sum(
        x for x in base_points
        if isinstance(x, int) and x > 0
    )
    if total <= 0:
        return "Null - error"

    return round(current * 100.0 / total, 6)


def make_enemy_key(record: dict[str, Any]) -> str:
    map_file = record.get("Map_File")
    part_name = record.get("Part_Name")
    if not isinstance(map_file, str) or not isinstance(part_name, str):
        raise BuildError("Enemy sin Map_File/Part_Name válidos.")
    return f"{map_file}|{part_name}"


def displayed_matches_raw(displayed: Any, raw: Any, empty_values: tuple[Any, ...] = (0, -1)) -> bool:
    if displayed == "Empty":
        return raw in empty_values
    return displayed == raw


def compare_displayed_to_raw(label: str, displayed: Any, raw: Any, errors: list[str], empty_values: tuple[Any, ...] = (0, -1)) -> None:
    if not displayed_matches_raw(displayed, raw, empty_values):
        errors.append(f"{label}: output={displayed!r} raw={raw!r}")


def compare_values(label: str, got: Any, expected: Any, errors: list[str]) -> None:
    if got != expected:
        errors.append(f"{label}: got={got!r} expected={expected!r}")


def resolve_paths(game_root: str | None = None, mod_directory: str | None = None):
    try:
        return engine_config.get_paths(
            game_root=game_root,
            mod_directory=mod_directory,
            script_dir=Path(__file__).resolve().parent,
        )
    except Exception as exc:
        raise BuildError(str(exc)) from exc


def locate_reference_file(script_dir: Path, filename: str, enemy_tables: Path) -> Path:
    candidates = [
        enemy_tables / filename,
        script_dir / filename,
        script_dir / "reference" / filename,
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise BuildError(
        f"No se encontró {filename}. Buscado en:\n" +
        "\n".join(f"  {p}" for p in candidates)
    )


def prepare_source_snapshot(paths) -> tuple[Path, str]:
    """
    Garantiza una fuente MSB estable para que --replace nunca utilice por
    accidente el all_enemies.json que ya contiene datos enriquecidos.
    """
    source = paths.all_enemies_source
    current = paths.all_enemies
    seed = paths.seed_all_enemies

    paths.enemy_tables.mkdir(parents=True, exist_ok=True)

    if source.is_file():
        return source, "all_enemies.source.json (snapshot existente)"

    if current.is_file():
        shutil.copy2(current, source)
        return source, "all_enemies.json -> all_enemies.source.json (snapshot creado)"

    if seed.is_file():
        shutil.copy2(seed, current)
        shutil.copy2(seed, source)
        return source, "all_enemies_seed.json -> EnemyTables (snapshot inicial)"

    raise BuildError(
        "No existe una fuente de enemigos.\n"
        f"Falta: {current}\n"
        f"Seed esperado: {seed}"
    )


def validate_source_hashes(
    all_path: Path,
    all_enemies: list[dict[str, Any]],
    reference: dict[str, Any],
    data0: dict[str, Any],
    data0_json_path: Path,
) -> str:
    errors: list[str] = []

    ref_sources = reference.get("sources", {})
    ref_data0 = ref_sources.get("data0_source", {})
    expected_all_hash = str(ref_sources.get("msb_enemies_json_sha256", "")).lower()
    expected_data0_raw_hash = str(ref_data0.get("sha256", "")).lower()
    expected_data0_json_hash = str(ref_sources.get("data0_json_sha256", "")).lower()

    actual_all_hash = sha256_file(all_path).lower()
    actual_data0_json_hash = sha256_file(data0_json_path)

    embedded_data0_raw_hash = str(
        data0.get("source", {}).get("sha256", "")
    ).lower()

    if not expected_all_hash:
        errors.append("El índice no contiene msb_enemies_json_sha256.")
    elif actual_all_hash != expected_all_hash:
        errors.append(
            "SHA256 de la fuente MSB no coincide con ds3_reference_index.json: "
            f"actual={actual_all_hash} referencia={expected_all_hash}"
        )

    if not expected_data0_raw_hash:
        errors.append("El índice no contiene el SHA256 del Data0 raw.")
    elif embedded_data0_raw_hash != expected_data0_raw_hash:
        errors.append(
            "SHA256 del Data0 raw no coincide entre data0_original.json e índice: "
            f"actual={embedded_data0_raw_hash} referencia={expected_data0_raw_hash}"
        )

    if expected_data0_json_hash and actual_data0_json_hash:
        if actual_data0_json_hash != expected_data0_json_hash:
            errors.append(
                "SHA256 del archivo data0_original.json no coincide con el índice: "
                f"actual={actual_data0_json_hash} referencia={expected_data0_json_hash}"
            )

    expected_count = reference.get("maps", {}).get("enemy_count")
    if not isinstance(expected_count, int):
        errors.append("El índice no contiene maps.enemy_count válido.")
    elif len(all_enemies) != expected_count:
        errors.append(
            f"Enemy count no coincide: input={len(all_enemies)} reference={expected_count}"
        )

    if errors:
        raise BuildError("\n".join(errors))

    return "exact_sha256"


def select_all_enemies_source(
    all_path: Path,
    backup_path: Path,
    reference: dict[str, Any],
) -> tuple[Path, str]:
    expected_hash = str(
        reference.get("sources", {}).get("msb_enemies_json_sha256", "")
    ).lower()

    if backup_path.is_file() and expected_hash:
        actual = sha256_file(backup_path).lower()
        if actual == expected_hash:
            return backup_path, "all_enemies.source.json (SHA256 exacto)"

    if all_path.is_file() and expected_hash:
        actual = sha256_file(all_path).lower()
        if actual == expected_hash:
            return all_path, "all_enemies.json (SHA256 exacto)"

    raise BuildError(
        "No se encontró una fuente MSB compatible con el índice de referencia.\n"
        f"Fuente estable: {backup_path}\n"
        f"all_enemies: {all_path}\n"
        f"SHA256 esperado: {expected_hash}"
    )


def validate_enemy_alignment(
    all_enemies: list[dict[str, Any]],
    reference: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    ref_enemies = reference.get("maps", {}).get("enemies")
    if not isinstance(ref_enemies, list):
        raise BuildError("ds3_reference_index.json no contiene maps.enemies como lista.")

    ref_by_key: dict[str, dict[str, Any]] = {}
    for entry in ref_enemies:
        if not isinstance(entry, dict):
            raise BuildError("maps.enemies contiene una entrada no válida.")
        key = entry.get("enemy_key")
        if not isinstance(key, str):
            raise BuildError("maps.enemies contiene enemy_key inválido.")
        if key in ref_by_key:
            raise BuildError(f"enemy_key duplicado en referencia: {key}")
        ref_by_key[key] = entry

    input_keys: set[str] = set()
    errors: list[str] = []
    msb_fields = [
        "EntityID",
        "NPCParamID",
        "ThinkParamID",
        "TalkID",
        "Model_Name",
        "MSB_File_Offset",
        "Entity_Data_Offset",
        "Type_Data_Offset",
        "MSB_Part_Type",
        "MSB_Local_ID",
    ]

    for index, enemy in enumerate(all_enemies):
        key = make_enemy_key(enemy)
        if key in input_keys:
            errors.append(f"enemy_key duplicado en input: {key}")
        input_keys.add(key)

        ref = ref_by_key.get(key)
        if ref is None:
            errors.append(f"enemy_key del input ausente en referencia: {key}")
            continue

        if ref.get("enemy_index") != index:
            errors.append(
                f"{key}: enemy_index {ref.get('enemy_index')} != posición input {index}"
            )

        ref_map = ref.get("map", {})
        if ref_map.get("Part_Name") != enemy.get("Part_Name"):
            errors.append(f"{key}: Part_Name no coincide con referencia")
        if ref_map.get("Map_File") != enemy.get("Map_File"):
            errors.append(f"{key}: Map_File no coincide con referencia")
        if ref_map.get("Map_ID") != enemy.get("Map_ID"):
            errors.append(f"{key}: Map_ID no coincide con referencia")

        ref_msb = ref.get("MSB", {})
        for field in msb_fields:
            if enemy.get(field if field != "EntityID" else "NPC_ID") != ref_msb.get(field):
                actual_field = "NPC_ID" if field == "EntityID" else field
                errors.append(
                    f"{key}: {actual_field}={enemy.get(actual_field)!r} "
                    f"!= reference {field}={ref_msb.get(field)!r}"
                )

    if input_keys != set(ref_by_key):
        missing = sorted(set(ref_by_key) - input_keys)
        extra = sorted(input_keys - set(ref_by_key))
        if missing:
            errors.append(f"enemy_key faltantes en input: {missing[:10]}")
        if extra:
            errors.append(f"enemy_key extra en input: {extra[:10]}")

    if errors:
        raise BuildError(
            "La base MSB no coincide exactamente con el índice de referencia:\n" +
            "\n".join(errors[:100])
        )

    return ref_by_key


def build_npc_snapshot(npc_id: int, row: dict[str, Any]) -> dict[str, Any]:
    fields = row.get("fields", {})
    if not isinstance(fields, dict):
        raise BuildError(f"NPC_PARAM_ST row {npc_id}: fields inválidos.")

    snapshot = {
        "Param_Row_ID": npc_id,
        "BehaviorVariationId": display_numeric(fields.get("BehaviorVariationId")),
        "AiThinkId": display_numeric(fields.get("AiThinkId")),
        "disableRespawn": fields.get("disableRespawn", "Null - error"),
        "humanityLotId": display_optional_id(fields.get("humanityLotId")),
    }
    for i in NPC_LOT_SLOTS:
        snapshot[f"ItemLotId{i}"] = display_optional_id(fields.get(f"ItemLotId{i}"))
    return snapshot


def build_think_snapshot(think_id: int, row: dict[str, Any]) -> dict[str, Any]:
    fields = row.get("fields", {})
    if not isinstance(fields, dict):
        raise BuildError(f"NPC_THINK_PARAM_ST row {think_id}: fields inválidos.")

    return {
        "Param_Row_ID": think_id,
        "logicId": display_numeric(fields.get("logicId")),
        "battleGoalId": display_numeric(fields.get("battleGoalId")),
        "nearDist": display_numeric(fields.get("nearDist")),
        "midDist": display_numeric(fields.get("midDist")),
        "farDist": display_numeric(fields.get("farDist")),
    }


def build_drop_table(
    item_lot_id: int,
    npc_lot_slot: int,
    row: dict[str, Any],
) -> dict[str, Any]:
    fields = row.get("fields", {})
    if not isinstance(fields, dict):
        raise BuildError(f"ITEMLOT_PARAM_ST row {item_lot_id}: fields inválidos.")

    base_points = [
        fields.get(f"LotItemBasePoint0{i}")
        for i in ITEMLOT_SLOTS
    ]
    enable_luck_raw = fields.get("EnableLuckRaw")

    drops: list[dict[str, Any]] = []
    for i in ITEMLOT_SLOTS:
        item_id = fields.get(f"ItemLotId{i}")
        category_id = fields.get(f"LotItemCategory0{i}")
        base_point = fields.get(f"LotItemBasePoint0{i}")
        item_count = fields.get(f"LotItemNum{i}")

        drops.append({
            "Drop_Slot": i,
            "Item_ID": display_optional_id(item_id),
            "Item_Category_ID": (
                "Null - error" if category_id is None else category_id
            ),
            "Item_Category_Name": category_name(category_id),
            "Base_Point": display_numeric(base_point),
            "Relative_Chance_Percent": relative_chance(base_points, i - 1),
            "Item_Count": display_numeric(item_count),
            "Enable_Luck": enable_luck_from_raw(enable_luck_raw, i),
        })

    return {
        "Npc_ItemLot_Slot": npc_lot_slot,
        "DROP_ID": item_lot_id,
        "Drops": drops,
    }


def build_database(
    all_enemies: list[dict[str, Any]],
    reference: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = reference.get("params", {})
    npc_section = params.get("NPC_PARAM_ST", {})
    think_section = params.get("NPC_THINK_PARAM_ST", {})
    itemlot_section = params.get("ITEMLOT_PARAM_ST", {})

    npc_rows = {
        row["id"]: row
        for row in npc_section.get("rows", [])
        if isinstance(row, dict) and isinstance(row.get("id"), int)
    }
    think_rows = {
        row["id"]: row
        for row in think_section.get("rows", [])
        if isinstance(row, dict) and isinstance(row.get("id"), int)
    }
    itemlot_rows = {
        row["id"]: row
        for row in itemlot_section.get("rows", [])
        if isinstance(row, dict) and isinstance(row.get("id"), int)
    }

    ref_by_key = validate_enemy_alignment(all_enemies, reference)
    output: list[dict[str, Any]] = []

    stats = {
        "enemy_count": len(all_enemies),
        "npc_resolved": 0,
        "npc_missing": 0,
        "think_resolved": 0,
        "think_missing": 0,
        "itemlot_resolved": 0,
        "itemlot_missing": 0,
        "drop_tables": 0,
        "drop_slots": 0,
    }

    for enemy in all_enemies:
        key = make_enemy_key(enemy)
        ref = ref_by_key[key]
        references = ref.get("references", {})

        # Copia controlada: se conserva exactamente la información MSB del input.
        record = dict(enemy)
        record.pop("NPCParam", None)
        record.pop("ThinkParam", None)
        record.pop("Souls", None)
        record.pop("Drop_Tables", None)

        npc_ref = references.get("NPCParam")
        npc_id = enemy.get("NPCParamID")
        npc_row = npc_rows.get(npc_id) if isinstance(npc_id, int) else None

        if (
            references.get("NPCParamStatus") == "resolved"
            and isinstance(npc_id, int)
            and npc_row is not None
        ):
            record["NPCParam"] = build_npc_snapshot(npc_id, npc_row)
            record["Souls"] = display_numeric(npc_row.get("fields", {}).get("getSoul"))
            stats["npc_resolved"] += 1
        else:
            record["NPCParam"] = "Null - error"
            record["Souls"] = "Null - error"
            stats["npc_missing"] += 1

        think_id = enemy.get("ThinkParamID")
        think_row = think_rows.get(think_id) if isinstance(think_id, int) else None
        if (
            references.get("ThinkParamStatus") == "resolved"
            and isinstance(think_id, int)
            and think_row is not None
        ):
            record["ThinkParam"] = build_think_snapshot(think_id, think_row)
            stats["think_resolved"] += 1
        else:
            record["ThinkParam"] = "Null - error"
            stats["think_missing"] += 1

        drop_tables: list[dict[str, Any]] = []
        if npc_row is not None:
            npc_fields = npc_row.get("fields", {})
            for npc_lot_slot in NPC_LOT_SLOTS:
                lot_id = npc_fields.get(f"ItemLotId{npc_lot_slot}")
                if lot_id in (None, 0, -1):
                    continue
                if not isinstance(lot_id, int):
                    raise BuildError(
                        f"{key}: ItemLotId{npc_lot_slot} inválido: {lot_id!r}"
                    )

                lot_row = itemlot_rows.get(lot_id)
                if lot_row is None:
                    drop_tables.append({
                        "Npc_ItemLot_Slot": npc_lot_slot,
                        "DROP_ID": lot_id,
                        "Drops": "Null - error",
                    })
                    stats["itemlot_missing"] += 1
                    continue

                table = build_drop_table(lot_id, npc_lot_slot, lot_row)
                drop_tables.append(table)
                stats["itemlot_resolved"] += 1
                stats["drop_tables"] += 1
                stats["drop_slots"] += len(table["Drops"])

        record["Drop_Tables"] = drop_tables

        # Orden de salida deliberado y estable.
        ordered = {field: record[field] for field in EXPECTED_OUTPUT_TOP_LEVEL_FIELDS}
        output.append(ordered)

    return output, stats


def validate_output_against_reference(
    output: list[dict[str, Any]],
    reference: dict[str, Any],
) -> dict[str, int]:
    ref_by_key = {
        entry["enemy_key"]: entry
        for entry in reference["maps"]["enemies"]
    }

    errors: list[str] = []
    checked_npc = 0
    checked_think = 0
    checked_lots = 0

    for index, record in enumerate(output):
        key = make_enemy_key(record)
        ref = ref_by_key[key]
        derived = ref.get("derived", {})
        refs = ref.get("references", {})

        # Derived fields must point back to the reference rows.
        npc = record.get("NPCParam")
        if isinstance(npc, dict):
            npc_ref = refs.get("NPCParam", {})
            compare_values(f"{key} NPC Param_Row_ID", npc.get("Param_Row_ID"), npc_ref.get("row_id"), errors)
            checked_npc += 1

            npc_id = record.get("NPCParamID")
            npc_row = next(
                row for row in reference["params"]["NPC_PARAM_ST"]["rows"]
                if row["id"] == npc_id
            )
            fields = npc_row["fields"]
            compare_displayed_to_raw(f"{key} Souls", record.get("Souls"), fields.get("getSoul"), errors, (0,))
            compare_displayed_to_raw(f"{key} BehaviorVariationId", npc.get("BehaviorVariationId"), fields.get("BehaviorVariationId"), errors, (0,))
            compare_values(f"{key} disableRespawn", npc.get("disableRespawn"), fields.get("disableRespawn"), errors)
            for i in NPC_LOT_SLOTS:
                compare_displayed_to_raw(
                    f"{key} ItemLotId{i}",
                    npc.get(f"ItemLotId{i}"),
                    fields.get(f"ItemLotId{i}"),
                    errors,
                    (0, -1),
                )
            if "getSoul" in derived:
                compare_values(f"{key} derived.getSoul", fields.get("getSoul"), derived.get("getSoul"), errors)
            if "BehaviorVariationId" in derived:
                compare_values(f"{key} derived.BehaviorVariationId", fields.get("BehaviorVariationId"), derived.get("BehaviorVariationId"), errors)
            if "disableRespawn" in derived:
                compare_values(f"{key} derived.disableRespawn", fields.get("disableRespawn"), derived.get("disableRespawn"), errors)

        think = record.get("ThinkParam")
        if isinstance(think, dict):
            think_ref = refs.get("ThinkParam", {})
            compare_values(f"{key} Think Param_Row_ID", think.get("Param_Row_ID"), think_ref.get("row_id"), errors)
            checked_think += 1
            if "logicId" in derived:
                compare_displayed_to_raw(f"{key} derived.logicId", think.get("logicId"), derived.get("logicId"), errors, (0,))
            if "battleGoalId" in derived:
                compare_displayed_to_raw(f"{key} derived.battleGoalId", think.get("battleGoalId"), derived.get("battleGoalId"), errors, (0,))

        # Drop validation compares row-level raw fields with every output slot.
        for table in record.get("Drop_Tables", []):
            if table.get("Drops") == "Null - error":
                continue
            lot_id = table.get("DROP_ID")
            ref_lots = refs.get("ItemLots", [])
            ref_lot = next((x for x in ref_lots if x.get("row_id") == lot_id), None)
            if ref_lot is None:
                # It may exist as a normal row but not be present in reference ItemLots only in unexpected input.
                errors.append(f"{key}: Drop table {lot_id} no está en references.ItemLots")
                continue

            checked_lots += 1
            ref_slots = {slot["slot"]: slot for slot in ref_lot.get("slots", [])}
            for drop in table.get("Drops", []):
                slot_num = drop.get("Drop_Slot")
                ref_slot = ref_slots.get(slot_num)
                if ref_slot is None:
                    errors.append(f"{key}: Drop {lot_id} slot {slot_num} ausente en referencia")
                    continue

                compare_displayed_to_raw(f"{key} lot {lot_id} slot {slot_num} item", drop.get("Item_ID"), ref_slot.get("item_id"), errors, (0, -1))
                compare_values(f"{key} lot {lot_id} slot {slot_num} category", drop.get("Item_Category_ID"), ref_slot.get("category_raw"), errors)
                compare_displayed_to_raw(f"{key} lot {lot_id} slot {slot_num} base", drop.get("Base_Point"), ref_slot.get("base_point"), errors, (0,))
                compare_displayed_to_raw(f"{key} lot {lot_id} slot {slot_num} quantity", drop.get("Item_Count"), ref_slot.get("quantity"), errors, (0,))
                expected_share = ref_slot.get("base_point_share")
                got_share = drop.get("Relative_Chance_Percent")
                expected_percent = expected_share * 100.0 if isinstance(expected_share, (int, float)) else expected_share
                if got_share == "Empty" and expected_percent == 0:
                    pass
                elif isinstance(expected_percent, (int, float)) and isinstance(got_share, (int, float)):
                    if abs(float(expected_percent) - float(got_share)) > 1e-6:
                        errors.append(
                            f"{key} lot {lot_id} slot {slot_num} share: "
                            f"got={got_share} expected={expected_percent}"
                        )
                elif got_share != expected_percent:
                    errors.append(
                        f"{key} lot {lot_id} slot {slot_num} share: "
                        f"got={got_share!r} expected={expected_percent!r}"
                    )

    if errors:
        preview = "\n".join(errors[:120])
        raise BuildError(
            "La validación del JSON generado contra ds3_reference_index.json falló:\n" +
            preview
        )

    return {
        "output_records": len(output),
        "npc_objects_checked": checked_npc,
        "think_objects_checked": checked_think,
        "itemlot_tables_checked": checked_lots,
    }


def build_report(
    game_root: Path,
    enemy_tables: Path,
    all_path: Path,
    ref_path: Path,
    data0_path: Path,
    output_path: Path,
    stats: dict[str, Any],
    validation: dict[str, Any],
    source_description: str = "",
) -> str:
    ref = load_json(ref_path)
    v = ref.get("validation", {})

    lines = [
        f"DS3 Enemy Database Builder v{VERSION}",
        "=" * 72,
        f"Game root: {game_root}",
        f"EnemyTables: {enemy_tables}",
        f"Input MSB source: {all_path}",
        f"MSB source mode: {source_description}",
        f"Reference index: {ref_path}",
        f"Data0 original index: {data0_path}",
        f"Output: {output_path}",
        "",
        "SNAPSHOT",
        f"  all_enemies SHA256: {sha256_file(all_path)}",
        f"  all_enemies expected SHA256: {ref.get('sources', {}).get('msb_enemies_json_sha256', 'N/A')}",
        f"  Data0 SHA256: {ref.get('sources', {}).get('data0_source', {}).get('sha256', 'N/A')}",
        "",
        "BUILD",
    ]
    for k, val in stats.items():
        lines.append(f"  {k}: {val}")

    lines.extend([
        "",
        "VALIDATION",
    ])
    for k, val in validation.items():
        lines.append(f"  {k}: {val}")

    lines.extend([
        "",
        "REFERENCE STATUS",
        f"  enemy_count: {v.get('enemy_count', 'N/A')}",
        f"  npc_param_refs: {v.get('npc_param_refs', 'N/A')}",
        f"  npc_param_missing_reference_count: {v.get('npc_param_missing_reference_count', 'N/A')}",
        f"  think_param_refs: {v.get('think_param_refs', 'N/A')}",
        f"  think_param_missing_reference_count: {v.get('think_param_missing_reference_count', 'N/A')}",
        f"  itemlot_missing_reference_count: {v.get('itemlot_missing_reference_count', 'N/A')}",
        "",
        "NOTA",
        "  Los 'Null - error' que correspondan a IDs presentes en las listas de",
        "  referencias faltantes son dependencias reales ausentes del snapshot;",
        "  no se convierten en datos inventados.",
    ])

    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Construye all_enemies completo a partir del índice DS3 verificado."
    )
    parser.add_argument(
        "--game-root",
        default=None,
        help="Ruta a DARK SOULS III\\Game. También puede definirse DS3_GAME_ROOT.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Reemplaza EnemyTables\\all_enemies.json después de validar. Crea backup.",
    )
    parser.add_argument(
        "--output-name",
        default="all_enemies_complete.json",
        help="Nombre de salida no destructiva (default: all_enemies_complete.json).",
    )
    return parser.parse_args()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Construye y opcionalmente reemplaza all_enemies.json usando el índice DS3 validado."
    )
    parser.add_argument(
        "--game-root",
        default=None,
        help="Ruta a DARK SOULS III\\Game. Por defecto: padre de PythonEngineDS3.",
    )
    parser.add_argument(
        "--mod-directory",
        default=None,
        help="Ruta absoluta o relativa a Game de la carpeta del mod.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Reemplaza EnemyTables\\all_enemies.json después de validar. Mantiene all_enemies.source.json.",
    )
    parser.add_argument(
        "--output-name",
        default=None,
        help="Nombre de salida no destructiva. Por defecto: all_enemies_complete.json.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = resolve_paths(args.game_root, args.mod_directory)

    print("=" * 78)
    print(f"DS3 Enemy Database Builder v{VERSION}")
    print("=" * 78)
    print(f"Game root: {paths.game_root}")
    print(f"PythonEngineDS3: {paths.package_dir}")
    print(f"Mod: {paths.mod_directory}")
    print(f"EnemyTables: {paths.enemy_tables}")

    if not paths.game_root.is_dir():
        raise BuildError(f"No existe Game root: {paths.game_root}")

    paths.enemy_tables.mkdir(parents=True, exist_ok=True)

    all_path = paths.all_enemies
    source_snapshot = paths.all_enemies_source
    complete_path = paths.enemy_tables / (args.output_name or engine_config.FINAL_COMPLETE_FILENAME)
    report_path = paths.enemy_tables / engine_config.BUILDER_REPORT_FILENAME

    reference_path = locate_reference_file(
        paths.package_dir,
        engine_config.REFERENCE_INDEX_FILENAME,
        paths.enemy_tables,
    )
    data0_path = locate_reference_file(
        paths.package_dir,
        engine_config.DATA0_JSON_FILENAME,
        paths.enemy_tables,
    )

    # Si todavía no hay snapshot estable, lo creamos antes de seleccionar la
    # fuente. Nunca se hace sobre la salida enriquecida después de --replace.
    if not source_snapshot.is_file() and not all_path.is_file() and paths.seed_all_enemies.is_file():
        shutil.copy2(paths.seed_all_enemies, all_path)

    if not source_snapshot.is_file() and all_path.is_file():
        shutil.copy2(all_path, source_snapshot)

    source_all_path, source_description = select_all_enemies_source(
        all_path,
        source_snapshot,
        load_json(reference_path),
    )

    print(f"MSB source: {source_all_path} ({source_description})")
    print(f"Reference: {reference_path}")
    print(f"Data0 JSON: {data0_path}")

    reference = load_json(reference_path)
    data0 = load_json(data0_path)
    all_enemies = load_json(source_all_path)

    if not isinstance(all_enemies, list):
        raise BuildError("La fuente MSB debe contener una lista.")
    if not isinstance(reference, dict):
        raise BuildError("ds3_reference_index.json debe ser un objeto.")
    if not isinstance(data0, dict):
        raise BuildError("data0_original.json debe ser un objeto.")

    print(f"Registros MSB: {len(all_enemies)}")

    hash_mode = validate_source_hashes(
        source_all_path,
        all_enemies,
        reference,
        data0,
        data0_path,
    )
    print(f"[OK] Snapshots compatibles: {hash_mode}.")

    output, stats = build_database(all_enemies, reference)
    print(
        "[OK] Enlaces construidos: "
        f"NPC={stats['npc_resolved']}, Think={stats['think_resolved']}, "
        f"ItemLot={stats['itemlot_resolved']}"
    )

    validation = validate_output_against_reference(output, reference)
    print("[OK] Validación de salida contra el índice de referencia completada.")

    write_json_atomic(complete_path, output)
    print(f"[OK] Salida completa: {complete_path}")

    output_for_report = complete_path

    if args.replace:
        # Mantener un snapshot inmutable del input que corresponde al índice.
        if not source_snapshot.is_file():
            shutil.copy2(source_all_path, source_snapshot)
        temp_replace = all_path.with_suffix(all_path.suffix + ".build.tmp")
        temp_replace.write_bytes(complete_path.read_bytes())
        temp_replace.replace(all_path)
        output_for_report = all_path
        print(f"[OK] Snapshot fuente: {source_snapshot}")
        print(f"[OK] Reemplazado: {all_path}")

    report = build_report(
        paths.game_root,
        paths.enemy_tables,
        source_all_path,
        reference_path,
        data0_path,
        output_for_report,
        stats,
        validation,
        source_description,
    )
    write_text_atomic(report_path, report)
    print(f"[OK] Reporte: {report_path}")

    print("=" * 78)
    print("FINALIZADO SIN ERRORES DE CONSISTENCIA")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"\nABORTADO: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"\nERROR NO CONTROLADO: {exc}", file=sys.stderr)
        raise SystemExit(3)
