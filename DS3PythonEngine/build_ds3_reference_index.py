from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from collections import defaultdict, Counter

import ds3_engine_config as engine_config

# ============================================================
# CONFIGURATION
# ============================================================
#
# Todas las rutas se calculan desde PythonEngineDS3. La configuración
# compartida está en ds3_engine_config.py.
#
# --game-root y --mod-directory permiten sobreescribir la autodetección.

TARGET_PARAMS = ["NPC_PARAM_ST", "NPC_THINK_PARAM_ST", "ITEMLOT_PARAM_ST"]

TARGET_PARAMS = ["NPC_PARAM_ST", "NPC_THINK_PARAM_ST", "ITEMLOT_PARAM_ST"]

NPC_FIELDS = [
    "BehaviorVariationId", "AiThinkId", "NameId", "Hp", "Mp", "getSoul",
    "ItemLotId1", "ItemLotId2", "ItemLotId3", "ItemLotId4", "ItemLotId5", "ItemLotId6",
    "disableRespawn",
]

THINK_FIELDS = ["logicId", "battleGoalId", "nearDist", "midDist", "farDist"]

LOT_FIELDS = [
    *(f"ItemLotId{i}" for i in range(1, 9)),
    *(f"LotItemCategory{i:02d}" for i in range(1, 9)),
    *(f"LotItemBasePoint{i:02d}" for i in range(1, 9)),
    *(f"cumulateLotPoint{i:02d}" for i in range(1, 9)),
    *(f"LotItemNum{i}" for i in range(1, 9)),
    "EnableLuckRaw", "cumulateResetRaw", "ClearCount", "cumulateNumFlagId", "cumulateNumMax", "getItemFlagId",
]

MSB_REFERENCE_SCHEMA = {
    "EnemyPart": {
        "ThinkParamID": {
            "source": "MSB3 Enemy TypeData",
            "relative_offset": "0x08",
            "size_bytes": 4,
            "encoding": "little-endian signed int32",
        },
        "NPCParamID": {
            "source": "MSB3 Enemy TypeData",
            "relative_offset": "0x0C",
            "size_bytes": 4,
            "encoding": "little-endian signed int32",
        },
        "TalkID": {
            "source": "MSB3 Enemy TypeData",
            "relative_offset": "0x10",
            "size_bytes": 4,
            "encoding": "little-endian signed int32",
        },
        "EntityID": {
            "source": "MSB3 Enemy EntityData",
            "relative_offset": "0x00",
            "size_bytes": 4,
            "encoding": "little-endian signed int32",
        },
    },
    "NPC_PARAM_ST": {
        "BehaviorVariationId": "0x00",
        "AiThinkId": "0x04",
        "NameId": "0x08",
        "Hp": "0x20",
        "Mp": "0x24",
        "getSoul": "0x28",
        "ItemLotId1": "0x2C",
        "ItemLotId2": "0x30",
        "ItemLotId3": "0x34",
        "ItemLotId4": "0x38",
        "ItemLotId5": "0x3C",
        "ItemLotId6": "0x40",
        "disableRespawn": "documented field; raw byte location depends on full PARAMDEF layout",
    },
    "NPC_THINK_PARAM_ST": {
        "logicId": "0x00",
        "battleGoalId": "0x04",
        "nearDist": "0x08",
        "midDist": "0x0C",
        "farDist": "0x10",
    },
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"No existe el JSON: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        raise RuntimeError(f"JSON inválido: {path}: {exc}") from exc


def prepare_paths(game_root: str | None = None, mod_directory: str | None = None):
    paths = engine_config.get_paths(
        game_root=game_root,
        mod_directory=mod_directory,
        script_dir=Path(__file__).resolve().parent,
    )
    paths.enemy_tables.mkdir(parents=True, exist_ok=True)
    return paths


def prepare_enemy_source(paths) -> tuple[Path, str]:
    """
    Selecciona una fuente MSB estable.

    Prioridad:
        1) all_enemies.source.json (snapshot preservado de una ejecución anterior)
        2) all_enemies.json existente
        3) all_enemies_seed.json incluido en el paquete

    Nunca sustituye silenciosamente un all_enemies.json existente por el seed.
    """
    source = paths.all_enemies_source
    current = paths.all_enemies
    seed = paths.seed_all_enemies

    if source.is_file():
        return source, "all_enemies.source.json (snapshot existente)"

    if current.is_file():
        shutil.copy2(current, source)
        return source, "all_enemies.json -> all_enemies.source.json (snapshot creado)"

    if seed.is_file():
        shutil.copy2(seed, current)
        shutil.copy2(seed, source)
        return source, "all_enemies_seed.json -> EnemyTables (snapshot inicial)"

    raise FileNotFoundError(
        "No se encontró la fuente de enemigos.\n"
        f"Esperado: {current}\n"
        f"Seed del paquete: {seed}"
    )


def validate_enemy_source(enemies: list[dict]) -> None:
    if not isinstance(enemies, list):
        raise RuntimeError("all_enemies.source.json debe contener una lista.")

    seen: set[str] = set()
    required = (
        "Part_Name", "NPCParamID", "ThinkParamID",
        "Map_ID", "Map_File",
    )

    for index, enemy in enumerate(enemies):
        if not isinstance(enemy, dict):
            raise RuntimeError(f"Enemigo #{index}: entrada no es un objeto.")
        for field in required:
            if field not in enemy:
                raise RuntimeError(f"Enemigo #{index}: falta el campo {field!r}.")
        map_file = enemy.get("Map_File")
        part_name = enemy.get("Part_Name")
        if not isinstance(map_file, str) or not isinstance(part_name, str):
            raise RuntimeError(f"Enemigo #{index}: Map_File/Part_Name inválidos.")
        key = f"{map_file}|{part_name}"
        if key in seen:
            raise RuntimeError(f"enemy_key duplicado: {key}")
        seen.add(key)
        if enemy.get("MSB_Part_Type") not in (None, 2):
            raise RuntimeError(
                f"{key}: MSB_Part_Type inesperado: {enemy.get('MSB_Part_Type')!r}"
            )


def find_param_nodes(root):
    found = {}

    def walk(x):
        if isinstance(x, dict):
            if x.get("kind") == "PARAM":
                header = x.get("header") or {}
                ptype = header.get("param_type")
                if ptype:
                    found.setdefault(ptype, x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(root)
    return found


def compact_row(row: dict, fields: list[str]) -> dict:
    out = {
        "index": row.get("index"),
        "id": row.get("id"),
        "data_offset": row.get("data_offset"),
        "data_end": row.get("data_end", "Null - error"),
        "data_size": row.get("data_size", "Null - error"),
    }
    if row.get("name") not in (None, ""):
        out["name"] = row.get("name")
    f = row.get("fields") or {}
    out["fields"] = {k: f.get(k, "Null - error") for k in fields}
    return out


def build_param_cache(param_nodes: dict):
    params = {}
    for ptype in TARGET_PARAMS:
        node = param_nodes.get(ptype)
        if not node:
            params[ptype] = {"status": "missing"}
            continue
        h = node.get("header") or {}
        fields = NPC_FIELDS if ptype == "NPC_PARAM_ST" else THINK_FIELDS if ptype == "NPC_THINK_PARAM_ST" else LOT_FIELDS
        rows = [compact_row(r, fields) for r in (node.get("rows") or [])]
        by_id = defaultdict(list)
        for r in rows:
            by_id[str(r["id"])].append(r["index"])

        parent_entry = None
        source_path = node.get("source_path")
        params[ptype] = {
            "status": "resolved",
            "bnd4_entry_index": None,
            "source_path": source_path,
            "row_count": h.get("row_count"),
            "row_size": h.get("detected_row_size"),
            "data_start": h.get("actual_data_start"),
            "param_type_offset": h.get("param_type_offset"),
            "paramdef_data_version": h.get("paramdef_data_version"),
            "paramdef_format_version": h.get("paramdef_format_version"),
            "row_header_size": h.get("row_header_size"),
            "row_headers_start": h.get("row_headers_start", "0x40"),
            "field_names": fields,
            "row_id_index": dict(by_id),
            "rows": rows,
        }
    return params


def path_entry_index(data0_root, source_path):
    if not source_path:
        return None
    for e in data0_root.get("bnd4", {}).get("entries", []):
        if e.get("name") and e.get("name") in source_path.split("::")[-1]:
            return e.get("index")
    # exact tail comparison
    tail = source_path.split("::")[-1]
    for e in data0_root.get("bnd4", {}).get("entries", []):
        if e.get("name") == tail:
            return e.get("index")
    return None


def make_row_lookup(params, ptype):
    rows = params[ptype].get("rows") or []
    return {r["id"]: r for r in rows}


def resolve_row(params, ptype, row_id):
    if not isinstance(row_id, int):
        return None, "invalid"
    p = params.get(ptype) or {}
    if p.get("status") != "resolved":
        return None, "param_missing"
    ids = p.get("row_id_index", {}).get(str(row_id))
    if not ids:
        return None, "missing"
    idx = ids[0]
    row = next((r for r in p["rows"] if r.get("index") == idx), None)
    return row, "resolved" if row else "missing"


def compact_lot_slots(lot_row: dict) -> list[dict]:
    if not lot_row:
        return []
    f = lot_row.get("fields") or {}
    slots = []
    for i in range(1, 9):
        bid = f.get(f"ItemLotId{i}")
        cat = f.get(f"LotItemCategory{i:02d}")
        bp = f.get(f"LotItemBasePoint{i:02d}")
        cum = f.get(f"cumulateLotPoint{i:02d}")
        num = f.get(f"LotItemNum{i}")
        # Keep all eight slots because a zero/empty slot is semantically useful.
        slots.append({
            "slot": i,
            "item_id": bid,
            "category_raw": cat,
            "base_point": bp,
            "cumulate_lot_point": cum,
            "quantity": num,
        })
    points = [s["base_point"] for s in slots if isinstance(s["base_point"], (int, float)) and s["base_point"] > 0]
    total = sum(points)
    for s in slots:
        bp = s["base_point"]
        s["base_point_share"] = (bp / total) if total and isinstance(bp, (int, float)) and bp > 0 else 0.0
    return slots


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Construye ds3_reference_index.json a partir de data0_original.json y all_enemies.source.json."
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = prepare_paths(args.game_root, args.mod_directory)

    data0_path = paths.data0_json
    enemy_source_path, source_description = prepare_enemy_source(paths)

    if not data0_path.is_file():
        raise RuntimeError(
            "No existe data0_original.json. Ejecuta primero ds3_data0_reader.py.\n"
            f"Esperado: {data0_path}"
        )

    data0 = load_json(data0_path)
    enemies = load_json(enemy_source_path)

    validate_enemy_source(enemies)

    # El reader anterior debe haber identificado los tres PARAM usados por
    # esta etapa. Si falta uno, abortamos aquí en vez de fabricar referencias.
    param_nodes = find_param_nodes(data0)
    params = build_param_cache(param_nodes)

    for ptype in TARGET_PARAMS:
        if params.get(ptype, {}).get("status") != "resolved":
            raise RuntimeError(
                f"{ptype} no está resuelto en data0_original.json; "
                "se cancela la construcción del índice."
            )
        if not params[ptype].get("rows"):
            raise RuntimeError(f"{ptype} no contiene filas.")

    # Add exact BND4 entry positions for the target params.
    for ptype, p in params.items():
        if p.get("status") == "resolved":
            p["bnd4_entry_index"] = path_entry_index(data0, p.get("source_path"))

    npc_lookup = make_row_lookup(params, "NPC_PARAM_ST")
    think_lookup = make_row_lookup(params, "NPC_THINK_PARAM_ST")
    lot_lookup = make_row_lookup(params, "ITEMLOT_PARAM_ST")

    enriched_enemies = []
    by_npc = defaultdict(list)
    by_think = defaultdict(list)
    by_lot = defaultdict(list)
    missing_npc = Counter()
    missing_think = Counter()
    missing_lot = Counter()

    for i, e in enumerate(enemies):
        npc_id = e.get("NPCParamID")
        think_id = e.get("ThinkParamID")
        map_file = e.get("Map_File")
        part = e.get("Part_Name")
        enemy_key = f"{map_file}|{part}"

        npc_row, npc_status = resolve_row(params, "NPC_PARAM_ST", npc_id)
        think_row, think_status = resolve_row(params, "NPC_THINK_PARAM_ST", think_id)

        npc_ref = None
        lot_refs = []
        if npc_row:
            npc_ref = {
                "param_type": "NPC_PARAM_ST",
                "row_id": npc_row["id"],
                "row_index": npc_row["index"],
                "data_offset": npc_row["data_offset"],
                "data_size": npc_row["data_size"],
            }
            for n in range(1, 7):
                lid = npc_row.get("fields", {}).get(f"ItemLotId{n}")
                if isinstance(lid, int) and lid > 0:
                    lot_row = lot_lookup.get(lid)
                    if lot_row:
                        ref = {
                            "field": f"ItemLotId{n}",
                            "row_id": lid,
                            "row_index": lot_row["index"],
                            "data_offset": lot_row["data_offset"],
                            "data_size": lot_row["data_size"],
                            "slots": compact_lot_slots(lot_row),
                        }
                        lot_refs.append(ref)
                        by_lot[str(lid)].append(i)
                    else:
                        lot_refs.append({
                            "field": f"ItemLotId{n}",
                            "row_id": lid,
                            "status": "missing",
                        })
                        missing_lot[lid] += 1
            by_npc[str(npc_row["id"])].append(i)
        elif isinstance(npc_id, int):
            missing_npc[npc_id] += 1

        if think_row:
            think_ref = {
                "param_type": "NPC_THINK_PARAM_ST",
                "row_id": think_row["id"],
                "row_index": think_row["index"],
                "data_offset": think_row["data_offset"],
                "data_size": think_row["data_size"],
            }
            by_think[str(think_row["id"])].append(i)
        else:
            think_ref = None
            if isinstance(think_id, int):
                missing_think[think_id] += 1

        rec = {
            "enemy_index": i,
            "enemy_key": enemy_key,
            "map": {
                "Map_ID": map_file.removesuffix(".msb.dcx") if isinstance(map_file, str) else e.get("Map_ID"),
                "Map_File": map_file,
                "Part_Name": part,
            },
            "MSB": {
                "EntityID": e.get("NPC_ID"),
                "NPCParamID": npc_id,
                "ThinkParamID": think_id,
                "TalkID": e.get("TalkID"),
                "Model_Name": e.get("Model_Name"),
                "MSB_File_Offset": e.get("MSB_File_Offset"),
                "Entity_Data_Offset": e.get("Entity_Data_Offset"),
                "Type_Data_Offset": e.get("Type_Data_Offset"),
                "MSB_Part_Type": e.get("MSB_Part_Type"),
                "MSB_Local_ID": e.get("MSB_Local_ID"),
            },
            "references": {
                "NPCParam": npc_ref,
                "NPCParamStatus": npc_status,
                "ThinkParam": think_ref,
                "ThinkParamStatus": think_status,
                "ItemLots": lot_refs,
            },
        }
        if npc_row:
            f = npc_row.get("fields") or {}
            rec["derived"] = {
                "getSoul": f.get("getSoul"),
                "disableRespawn": f.get("disableRespawn"),
                "BehaviorVariationId": f.get("BehaviorVariationId"),
            }
        if think_row:
            f = think_row.get("fields") or {}
            rec["derived"] = {
                **rec.get("derived", {}),
                "logicId": f.get("logicId"),
                "battleGoalId": f.get("battleGoalId"),
            }
        enriched_enemies.append(rec)

    out = {
        "tool": {
            "name": "DS3 Reference Index Builder",
            "version": "2.0-pipeline-configured",
            "mode": "read_only",
            "external_libraries": False,
        },
        "purpose": "Compact cross-reference cache for the future DS3 enemy editor. The MSB placement ThinkParamID is authoritative for the placed enemy; NPC_PARAM_ST is a separate referenced profile table.",
        "pipeline": {
            "game_root": str(paths.game_root),
            "python_engine_directory": str(paths.package_dir),
            "mod_directory": str(paths.mod_directory),
            "enemy_tables": str(paths.enemy_tables),
        },
        "sources": {
            "data0_json": str(data0_path),
            "data0_json_sha256": sha256_file(data0_path),
            "msb_enemies_json": str(enemy_source_path),
            "msb_enemies_json_sha256": sha256_file(enemy_source_path),
            "msb_source_description": source_description,
            "data0_source": data0.get("source", {}),
        },
        "routes": {
            "enemy_think": {
                "authoritative_source": "MSB3.Part.Enemy.ThinkParamID",
                "binary_reference": "Enemy.TypeData + 0x08",
                "do_not_replace_with": "NPC_PARAM_ST.AiThinkId",
            },
            "enemy_npc": {
                "authoritative_source": "MSB3.Part.Enemy.NPCParamID",
                "binary_reference": "Enemy.TypeData + 0x0C",
            },
            "npc_to_loot": {
                "authoritative_source": "NPC_PARAM_ST.ItemLotId1..6",
                "target_param": "ITEMLOT_PARAM_ST",
            },
            "think_to_ai": {
                "authoritative_source": "NPC_THINK_PARAM_ST.row_id",
                "useful_fields": ["logicId", "battleGoalId"],
            },
        },
        "schema": {
            "msb": MSB_REFERENCE_SCHEMA,
            "notes": [
                "The map placement ThinkParamID and NPCParamID are separate fields.",
                "NPC_PARAM_ST.AiThinkId is preserved in the param cache for completeness but is not used to resolve the map enemy ThinkParamID.",
                "All raw IDs and binary offsets are retained even when a referenced row is missing.",
            ],
        },
        "params": params,
        "maps": {
            "enemy_count": len(enriched_enemies),
            "enemies": enriched_enemies,
        },
        "reverse_index": {
            "enemies_by_npc_param_id": dict(by_npc),
            "enemies_by_think_param_id": dict(by_think),
            "enemies_by_itemlot_id": dict(by_lot),
        },
        "validation": {
            "enemy_count": len(enriched_enemies),
            "npc_param_refs": sum(1 for e in enriched_enemies if e["references"]["NPCParamStatus"] == "resolved"),
            "npc_param_unique_missing": sorted(missing_npc),
            "npc_param_missing_reference_count": sum(missing_npc.values()),
            "think_param_refs": sum(1 for e in enriched_enemies if e["references"]["ThinkParamStatus"] == "resolved"),
            "think_param_unique_missing": sorted(missing_think),
            "think_param_missing_reference_count": sum(missing_think.values()),
            "itemlot_unique_missing": sorted(missing_lot),
            "itemlot_missing_reference_count": sum(missing_lot.values()),
            "npc_param_ai_think_id_nonzero_rows": sum(
                1 for r in params["NPC_PARAM_ST"].get("rows", [])
                if isinstance(r.get("fields", {}).get("AiThinkId"), int) and r["fields"]["AiThinkId"] != 0
            ),
            "npc_param_rows": params["NPC_PARAM_ST"].get("row_count"),
            "npc_think_rows": params["NPC_THINK_PARAM_ST"].get("row_count"),
            "itemlot_rows": params["ITEMLOT_PARAM_ST"].get("row_count"),
        },
    }

    output_path = paths.reference_index
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("=" * 78)
    print("DS3 Reference Index Builder")
    print("=" * 78)
    print(f"Game root:    {paths.game_root}")
    print(f"Mod:          {paths.mod_directory}")
    print(f"EnemyTables:  {paths.enemy_tables}")
    print(f"Data0 JSON:   {data0_path}")
    print(f"MSB source:   {enemy_source_path}")
    print(f"Output:       {output_path}")
    print(f"Enemies:      {len(enriched_enemies)}")
    print(f"NPC refs OK:  {out['validation']['npc_param_refs']}")
    print(f"Think refs OK:{out['validation']['think_param_refs']}")
    print(f"ItemLot miss: {out['validation']['itemlot_missing_reference_count']}")
    print("Done.")
    return 0


if __name__ == "__main__":
    main()
