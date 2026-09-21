# -*- coding: utf-8 -*-
r"""
DS3 Enemy Randomizer v2
=======================

Objetivo
--------
Crear Randomized_enemies.json como copia modificada de
EnemyTables\\all_enemies_complete.json y aplicar esa misma planificación a los
MSB3 reales de Dark Souls III.

Flujo real de una ejecución:
    all_enemies_complete.json
        -> plan de donantes por mapa
        -> Randomized_enemies.json
        -> localizar cada *.msb.dcx en Game\\map\\mapstudio (o baseline de mod)
        -> descomprimir DCX -> leer PARTS_PARAM_ST / MODEL_PARAM_ST
        -> localizar Enemy Parts por Part_Name + offsets estructurales
        -> cambiar solamente ModelIndex / ThinkParamID / NPCParamID
        -> recomprimir en DCX conservando la cabecera original
        -> escribir en <MOD>\\map\\mapstudio\*.msb.dcx

No modifica:
    Data0.bdt, Data0.bhd, EMEVD, ESD, SFX, PARAM ni archivos fuera de
    <MOD>\\map\\mapstudio.

Protecciones importantes
------------------------
1. La fuente de datos es all_enemies_complete.json, no Randomized_enemies.json.
2. El baseline de cada mapa es el archivo que ya exista en el mod; de lo
   contrario se busca primero en mods de menor prioridad y finalmente en
   Game\\map\\mapstudio.
3. Antes de escribir, todos los mapas se validan en memoria.
4. Se comparan Part_Name, MSB_File_Offset, Entity_Data_Offset y
   Type_Data_Offset contra el JSON original. Esto permite conservar otras
   modificaciones del MSB, pero impide aplicar offsets de una estructura
   incompatible.
5. Solo se modifican 12 bytes por Enemy Part:
       entry + 0x10       ModelIndex
       TypeData + 0x08    ThinkParamID
       TypeData + 0x0C    NPCParamID
6. Los archivos existentes reciben backup antes de ser reemplazados.
7. Si cualquier mapa falla, no se escribe ningún mapa ni JSON nuevo.
8. Cada ejecución crea un manifest con hashes y conserva el baseline de todos
   los mapas, evitando randomizaciones acumulativas.

Uso:
    Randomize.bat
    Randomize.bat --seed 12345
    Randomize.bat --dry-run

Requisitos:
    Python 3.10+ (biblioteca estándar solamente)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import struct
import sys
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# ============================================================================
# CONFIGURACIÓN DIRECTAMENTE EN ESTE SCRIPT
# ============================================================================

GAME_ROOT_OVERRIDE = ""
MOD_DIRECTORY_OVERRIDE = ""

SOURCE_JSON_FILENAME = "all_enemies_complete.json"
FALLBACK_PACKAGED_COMPLETE = "all_enemies_complete_reference.json"
FALLBACK_PACKAGED_ALL_SHA256 = "7a65c638b86ad416b2f645d3719449c0fdbe1741dcdc04941495cc6ed2c4d2f6"
EXPECTED_ENEMY_COUNT = 2947
EXPECTED_MAP_COUNT = 23
OUTPUT_JSON_FILENAME = "Randomized_enemies.json"
REPORT_FILENAME = "RandomizeEnemies_report.txt"
MANIFEST_FILENAME = "RandomizeEnemies_manifest.json"
BACKUP_FOLDER_NAME = "RandomizeBackups"

# Randomización por mapa: el donor debe proceder del mismo MSB lógico.
RANDOMIZATION_SCOPE = "same_map"

# NPCs con TalkID se dejan fuera para no convertir personajes conversacionales
# en enemigos ordinarios.
EXCLUDE_NONEMPTY_TALK_ID = True

# Para una randomización jugable, se exigen estas tres piezas.
REQUIRE_VALID_NPC = True
REQUIRE_VALID_THINK = True
REQUIRE_VALID_MODEL = True

# Se puede desactivar desde consola con --dry-run.
APPLY_BY_DEFAULT = True

# Los nombres y offsets estructurales deben coincidir exactamente.
STRICT_STRUCTURE = True

# El objetivo de la modificación es que todos los mapas que aparecen en el
# JSON con enemigos sean preparados, incluso si un mapa tiene pocos enemigos.
WRITE_ALL_ENEMY_MAPS = True

# Solo se admite la variante DCX DFLT conocida por los MSB DS3 examinados.
DCX_COMPRESSION_MAGIC = b"DFLT"

PARTS_TYPE_ENEMY = 2

# Categorías solamente para información del JSON/diagnóstico, no se usan para
# decidir ModelIndex.
ITEM_CATEGORY_NAMES = {
    0xFFFFFFFF: "Empty",
    0x00000000: "Weapon",
    0x10000000: "Armor",
    0x20000000: "Ring",
    0x40000000: "Good",
}

try:
    from ds3_engine_config import get_paths as shared_get_paths
except Exception:
    shared_get_paths = None


class RandomizerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    package: Path
    game: Path
    mod: Path
    enemy_tables: Path
    map_vanilla: Path
    map_mod: Path
    source_json: Path
    randomized_json: Path
    report: Path
    manifest: Path
    backup_root: Path


def norm(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except Exception:
        return path.expanduser()


def script_dir() -> Path:
    return norm(Path(__file__).resolve().parent)


def game_root_fallback() -> Path:
    if GAME_ROOT_OVERRIDE:
        return norm(Path(GAME_ROOT_OVERRIDE))
    env = os.environ.get("DS3_GAME_ROOT", "").strip()
    if env:
        return norm(Path(env))
    return norm(script_dir().parent)


def discover_mod(game: Path) -> Path:
    if MOD_DIRECTORY_OVERRIDE:
        p = Path(MOD_DIRECTORY_OVERRIDE)
        return norm(p if p.is_absolute() else game / p)

    env = os.environ.get("DS3_MOD_DIRECTORY", "").strip()
    if env:
        p = Path(env)
        return norm(p if p.is_absolute() else game / p)

    # Prefer exactly one existing EnemyTables, because the previous pipeline
    # already placed the reference JSONs there.
    tables: list[Path] = []
    try:
        for p in game.rglob("EnemyTables"):
            if p.is_dir() and (p / SOURCE_JSON_FILENAME).is_file():
                tables.append(norm(p))
    except OSError:
        pass

    unique: dict[str, Path] = {str(p).lower(): p for p in tables}
    tables = sorted(unique.values(), key=lambda p: str(p).lower())
    if len(tables) == 1:
        return tables[0].parent

    # Standard Mod Engine 2 locations.
    roots = [
        game / "ModEngine-2.1.0.0-win64" / "mod",
        game / "mod",
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
        except OSError:
            children = []
        preferred = [p for p in children if p.name.lower() == "mimod"]
        candidates.extend(preferred)
        if len(children) == 1:
            candidates.extend(children)

    unique_candidates = {str(norm(p)).lower(): norm(p) for p in candidates}
    if len(unique_candidates) == 1:
        return next(iter(unique_candidates.values()))
    if len(unique_candidates) > 1:
        paths_text = "\n".join(
            str(p) for p in sorted(unique_candidates.values(), key=lambda p: str(p).lower())
        )
        raise RandomizerError(
            "No se puede decidir qué mod modificar porque hay varias rutas posibles.\n"
            "Configura MOD_DIRECTORY_OVERRIDE en RandomizeEnemies.py o DS3_MOD_DIRECTORY.\n\n"
            + paths_text
        )


    # No inventamos una ruta de mod. Esta ruta solo se crea si el usuario la
    # configura explícitamente o si el pipeline ya la creó.
    raise RandomizerError(
        "No se encontró la carpeta del mod con EnemyTables.\n"
        "Configura MOD_DIRECTORY_OVERRIDE en RandomizeEnemies.py o DS3_MOD_DIRECTORY."
    )


def resolve_paths(args: argparse.Namespace) -> Paths:
    package = script_dir()
    if shared_get_paths is not None:
        try:
            shared = shared_get_paths(
                game_root=args.game_root or GAME_ROOT_OVERRIDE or None,
                mod_directory=args.mod_directory or MOD_DIRECTORY_OVERRIDE or None,
                script_dir=package,
            )
            game = norm(shared.game_root)
            mod = norm(shared.mod_directory)
        except Exception:
            game = norm(Path(args.game_root)) if args.game_root else game_root_fallback()
            mod = norm(Path(args.mod_directory)) if args.mod_directory else discover_mod(game)
    else:
        game = norm(Path(args.game_root)) if args.game_root else game_root_fallback()
        mod = norm(Path(args.mod_directory)) if args.mod_directory else discover_mod(game)

    tables = norm(mod / "EnemyTables")
    return Paths(
        package=package,
        game=game,
        mod=mod,
        enemy_tables=tables,
        map_vanilla=norm(game / "map" / "mapstudio"),
        map_mod=norm(mod / "map" / "mapstudio"),
        source_json=norm(tables / SOURCE_JSON_FILENAME),
        randomized_json=norm(tables / OUTPUT_JSON_FILENAME),
        report=norm(tables / REPORT_FILENAME),
        manifest=norm(tables / MANIFEST_FILENAME),
        backup_root=norm(package / BACKUP_FOLDER_NAME),
    )


# ============================================================================
# JSON / DISPLAY
# ============================================================================


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> Any:
    if not path.is_file():
        raise RandomizerError(f"Archivo no encontrado: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RandomizerError(f"JSON inválido: {path}: {exc}") from exc


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=4), encoding="utf-8")
    tmp.replace(path)


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    tmp.replace(path)



def validate_source_snapshot(records: Any, label: str, reference_records: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if not isinstance(records, list) or not all(isinstance(x, dict) for x in records):
        raise RandomizerError(f"{label} no es una lista válida de registros.")
    if not records:
        raise RandomizerError(f"{label} está vacío.")
    enemy_maps = {relative_map(x) for x in records if x.get("MSB_Part_Type") == PARTS_TYPE_ENEMY and isinstance(x.get("Map_File"), str)}
    if len(records) != EXPECTED_ENEMY_COUNT or len(enemy_maps) != EXPECTED_MAP_COUNT:
        if reference_records is not None:
            ref_maps = {relative_map(x) for x in reference_records if x.get("MSB_Part_Type") == PARTS_TYPE_ENEMY and isinstance(x.get("Map_File"), str)}
            ref_keys = [(relative_map(x), x.get("Part_Name")) for x in reference_records if x.get("MSB_Part_Type") == PARTS_TYPE_ENEMY]
            keys = [(relative_map(x), x.get("Part_Name")) for x in records if x.get("MSB_Part_Type") == PARTS_TYPE_ENEMY]
            if ref_maps == enemy_maps and ref_keys == keys:
                return records
        raise RandomizerError(
            f"{label} no corresponde al snapshot esperado: records={len(records)} mapas={len(enemy_maps)}; "
            f"se esperan {EXPECTED_ENEMY_COUNT} registros y {EXPECTED_MAP_COUNT} mapas."
        )
    return records


def load_source_json(paths: Paths) -> tuple[Path, list[dict[str, Any]], str]:
    all_path = paths.enemy_tables / "all_enemies.json"
    all_records: list[dict[str, Any]] | None = None
    all_hash = None
    if all_path.is_file():
        all_hash = sha256_file(all_path).lower()
        raw = load_json(all_path)
        if isinstance(raw, list) and all(isinstance(x, dict) for x in raw):
            all_records = raw

    local = paths.source_json
    if local.is_file():
        local_records = load_json(local)
        try:
            validate_source_snapshot(local_records, str(local), all_records)
            return local, local_records, "all_enemies_complete.json"
        except RandomizerError as local_error:
            # Si complete está incompleto pero all_enemies coincide exactamente
            # con el snapshot que produjo el paquete, podemos reconstruir una
            # copia integrada conocida y segura.
            packaged = paths.package / FALLBACK_PACKAGED_COMPLETE
            if packaged.is_file() and all_records is not None and all_hash == FALLBACK_PACKAGED_ALL_SHA256:
                packaged_records = load_json(packaged)
                validate_source_snapshot(packaged_records, str(packaged), all_records)
                return packaged, packaged_records, "packaged_integrated_snapshot_fallback"
            raise RandomizerError(
                f"{local} existe pero no cubre el snapshot completo. {local_error}"
            )

    packaged = paths.package / FALLBACK_PACKAGED_COMPLETE
    if packaged.is_file() and all_records is not None and all_hash == FALLBACK_PACKAGED_ALL_SHA256:
        packaged_records = load_json(packaged)
        validate_source_snapshot(packaged_records, str(packaged), all_records)
        return packaged, packaged_records, "packaged_integrated_snapshot_fallback"

    if all_records is not None:
        validate_source_snapshot(all_records, str(all_path))
        return all_path, all_records, "all_enemies_msbs_only_fallback"

    raise RandomizerError(
        "No existe all_enemies_complete.json ni un all_enemies.json válido en EnemyTables."
    )

def is_empty(value: Any) -> bool:
    return value in (None, "", "Empty", "Null - error")


def valid_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def relative_map(record: dict[str, Any]) -> str:
    value = record.get("Map_File")
    if not isinstance(value, str) or not value:
        raise RandomizerError("Registro sin Map_File válido.")
    return value.replace("/", "\\")


def enemy_key(record: dict[str, Any]) -> str:
    return f"{relative_map(record)}|{record.get('Part_Name')}"


def eligible(record: dict[str, Any]) -> tuple[bool, str]:
    if record.get("MSB_Part_Type") != PARTS_TYPE_ENEMY:
        return False, "not_enemy"
    if not isinstance(record.get("Map_File"), str):
        return False, "missing_map"
    if not isinstance(record.get("Part_Name"), str) or is_empty(record.get("Part_Name")):
        return False, "missing_part_name"
    if EXCLUDE_NONEMPTY_TALK_ID and not is_empty(record.get("TalkID")):
        return False, "talk_id"
    if REQUIRE_VALID_NPC and not valid_int(record.get("NPCParamID")):
        return False, "invalid_npc"
    if REQUIRE_VALID_THINK and not valid_int(record.get("ThinkParamID")):
        return False, "invalid_think"
    if REQUIRE_VALID_MODEL and (not isinstance(record.get("Model_Name"), str) or is_empty(record.get("Model_Name"))):
        return False, "invalid_model"
    return True, "eligible"


def group_records(records: list[dict[str, Any]]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for i, rec in enumerate(records):
        if rec.get("MSB_Part_Type") != PARTS_TYPE_ENEMY:
            continue
        if not isinstance(rec.get("Map_File"), str):
            continue
        out.setdefault(relative_map(rec), []).append(i)
    return out


def derange(records: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    if len(records) <= 1:
        return records[:]
    original = [enemy_key(r) for r in records]
    donors = records[:]
    for _ in range(200):
        rng.shuffle(donors)
        if all(enemy_key(donors[i]) != original[i] for i in range(len(records))):
            return donors
    return records[1:] + records[:1]


def make_plan(records: list[dict[str, Any]], seed: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rng = random.Random(seed)
    output = [dict(x) for x in records]
    by_map: dict[str, list[int]] = {}
    skipped: dict[str, int] = {}
    donor_log: list[dict[str, Any]] = []

    for i, rec in enumerate(records):
        ok, reason = eligible(rec)
        if ok:
            by_map.setdefault(relative_map(rec), []).append(i)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1

    for map_file, indices in sorted(by_map.items()):
        donors = derange([records[i] for i in indices], rng)
        for target_index, donor in zip(indices, donors):
            target = records[target_index]
            new = dict(target)
            for field in ("NPCParamID", "ThinkParamID", "Model_Name", "Model_Index"):
                new[field] = donor.get(field)
            # Copiamos solamente información que representa al donor. No se
            # toca EntityID/TalkID/part name/map/offsets del target.
            for field in ("NPCParam", "ThinkParam", "Souls", "Drop_Tables"):
                if field in donor:
                    new[field] = donor[field]
            output[target_index] = new
            donor_log.append({
                "map_file": map_file,
                "target_part": target.get("Part_Name"),
                "source_part": donor.get("Part_Name"),
                "old_model": target.get("Model_Name"),
                "new_model": donor.get("Model_Name"),
                "old_npc": target.get("NPCParamID"),
                "new_npc": donor.get("NPCParamID"),
                "old_think": target.get("ThinkParamID"),
                "new_think": donor.get("ThinkParamID"),
            })

    stats = {
        "input_records": len(records),
        "enemy_records": sum(len(v) for v in group_records(records).values()),
        "map_count": len(by_map),
        "eligible_records": sum(len(v) for v in by_map.values()),
        "randomized_records": len(donor_log),
        "skipped": skipped,
        "donor_log": donor_log,
    }
    return output, stats


# ============================================================================
# MSB3 / DCX
# ============================================================================


def ensure_range(data: bytes | bytearray, offset: int, size: int, label: str = "") -> None:
    if offset < 0 or offset + size > len(data):
        suffix = f" ({label})" if label else ""
        raise RandomizerError(f"Lectura fuera de rango: offset=0x{offset:X}, size=0x{size:X}{suffix}")


def i32(data: bytes | bytearray, offset: int) -> int:
    ensure_range(data, offset, 4)
    return int.from_bytes(data[offset:offset + 4], "little", signed=True)


def u32(data: bytes | bytearray, offset: int) -> int:
    ensure_range(data, offset, 4)
    return int.from_bytes(data[offset:offset + 4], "little", signed=False)


def i64(data: bytes | bytearray, offset: int) -> int:
    ensure_range(data, offset, 8)
    return int.from_bytes(data[offset:offset + 8], "little", signed=True)


def put_i32(data: bytearray, offset: int, value: int) -> None:
    ensure_range(data, offset, 4)
    data[offset:offset + 4] = int(value).to_bytes(4, "little", signed=True)


def read_utf16z(data: bytes | bytearray, offset: int) -> str:
    if offset <= 0 or offset >= len(data):
        raise RandomizerError(f"UTF-16 offset inválido: 0x{offset:X}")
    end = offset
    while end + 1 < len(data):
        if data[end:end + 2] == b"\x00\x00":
            break
        end += 2
    raw = data[offset:end]
    try:
        return raw.decode("utf-16-le", errors="strict")
    except UnicodeDecodeError as exc:
        raise RandomizerError(f"UTF-16 inválido @ 0x{offset:X}: {exc}") from exc


@dataclass(frozen=True)
class SectionInfo:
    name: str
    offset: int
    count: int
    entries: tuple[int, ...]
    next_offset: int


@dataclass(frozen=True)
class EnemyLocation:
    part_name: str
    entry_offset: int
    entity_data_offset: int
    type_data_offset: int
    model_index: int
    entity_id: int
    local_id: int
    npc_param_id: int
    think_param_id: int
    talk_id: int


@dataclass(frozen=True)
class ParsedMSB:
    msb: bytes
    sections: tuple[SectionInfo, ...]
    model_names: tuple[str, ...]
    model_to_indices: dict[str, tuple[int, ...]]
    enemies: tuple[EnemyLocation, ...]


def parse_section_header(data: bytes, offset: int) -> SectionInfo:
    ensure_range(data, offset, 16, "MSB section header")
    count = i32(data, offset + 4)
    name_offset = i64(data, offset + 8)
    if count < 1 or count > 1_000_000:
        raise RandomizerError(f"MSB offsetCount sospechoso @ 0x{offset:X}: {count}")
    table = offset + 16
    entries = tuple(i64(data, table + i * 8) for i in range(count - 1))
    next_offset = i64(data, table + (count - 1) * 8)
    return SectionInfo(read_utf16z(data, name_offset), offset, count, entries, next_offset)


def find_sections(data: bytes) -> tuple[SectionInfo, ...]:
    if not data.startswith(b"MSB "):
        raise RandomizerError(f"Magic MSB inválido: {data[:4]!r}")
    sections: list[SectionInfo] = []
    offset = 0x10
    for _ in range(32):
        sec = parse_section_header(data, offset)
        sections.append(sec)
        if sec.next_offset == 0:
            return tuple(sections)
        if sec.next_offset <= offset:
            raise RandomizerError(f"nextParamOffset inválido: 0x{sec.next_offset:X}")
        offset = sec.next_offset
    raise RandomizerError("MSB contiene más de 32 secciones o un ciclo de secciones.")


def parse_models(data: bytes, section: SectionInfo) -> tuple[tuple[str, ...], dict[str, tuple[int, ...]]]:
    names: list[str] = []
    by_name: dict[str, list[int]] = {}
    for index, entry in enumerate(section.entries):
        ensure_range(data, entry, 0x14, "MODEL_PARAM_ST entry")
        name_rel = i64(data, entry)
        name = read_utf16z(data, entry + name_rel)
        names.append(name)
        by_name.setdefault(name, []).append(index)
    return tuple(names), {k: tuple(v) for k, v in by_name.items()}


def parse_enemy_parts(data: bytes, section: SectionInfo) -> tuple[EnemyLocation, ...]:
    out: list[EnemyLocation] = []
    for entry in section.entries:
        ensure_range(data, entry, 0xC0, "Enemy Part")
        part_type = u32(data, entry + 0x08)
        if part_type != PARTS_TYPE_ENEMY:
            continue
        part_name = read_utf16z(data, entry + i64(data, entry))
        local_id = i32(data, entry + 0x0C)
        model_index = i32(data, entry + 0x10)
        entity_data_offset = entry + i64(data, entry + 0xB0)
        type_data_offset = entry + i64(data, entry + 0xB8)
        ensure_range(data, entity_data_offset, 4, "EntityData")
        ensure_range(data, type_data_offset, 0x14, "TypeData")
        out.append(EnemyLocation(
            part_name=part_name,
            entry_offset=entry,
            entity_data_offset=entity_data_offset,
            type_data_offset=type_data_offset,
            model_index=model_index,
            entity_id=i32(data, entity_data_offset),
            local_id=local_id,
            npc_param_id=i32(data, type_data_offset + 0x0C),
            think_param_id=i32(data, type_data_offset + 0x08),
            talk_id=i32(data, type_data_offset + 0x10),
        ))
    return tuple(out)


def decompress_dcx(raw: bytes) -> bytes:
    if raw.startswith(b"MSB "):
        return raw
    if not raw.startswith(b"DCX\x00"):
        raise RandomizerError(f"Mapa no es MSB ni DCX: {raw[:8]!r}")
    if len(raw) < 0x4C:
        raise RandomizerError("DCX demasiado pequeño.")
    if raw[0x28:0x2C] != DCX_COMPRESSION_MAGIC:
        raise RandomizerError(f"DCX no soportado: {raw[0x28:0x2C]!r}; se requiere DFLT.")
    compressed_size = int.from_bytes(raw[0x20:0x24], "big", signed=False)
    if raw[0x44:0x48] != b"DCA\x00":
        raise RandomizerError("DCX DFLT inválido: falta DCA en 0x44.")
    payload_offset = 0x4C
    ensure_range(raw, payload_offset, compressed_size, "DCX payload")
    payload = raw[payload_offset:payload_offset + compressed_size]
    try:
        out = zlib.decompress(payload)
    except zlib.error:
        try:
            out = zlib.decompress(payload, -15)
        except zlib.error as exc:
            raise RandomizerError(f"No se pudo descomprimir DCX: {exc}") from exc
    if not out.startswith(b"MSB "):
        raise RandomizerError(f"DCX descomprimido no contiene MSB: {out[:8]!r}")
    return out


def original_zlib_level(raw: bytes) -> int:
    # DFLT header byte @ 0x30 suele contener el nivel empleado al comprimir.
    # Solo aceptamos niveles válidos; si no, usamos 9.
    if len(raw) > 0x30 and raw[0x30] in range(1, 10):
        return int(raw[0x30])
    return 9


def compress_dcx_like(raw_source: bytes, msb: bytes) -> bytes:
    if raw_source.startswith(b"MSB "):
        return msb
    if not raw_source.startswith(b"DCX\x00"):
        raise RandomizerError("No se puede conservar formato: fuente no es DCX/MSB.")
    if len(raw_source) < 0x4C or raw_source[0x28:0x2C] != DCX_COMPRESSION_MAGIC:
        raise RandomizerError("Solo se admite DCX DFLT para recomprimir MSB DS3.")

    level = original_zlib_level(raw_source)
    compressed = zlib.compress(msb, level)
    header = bytearray(raw_source[:0x4C])
    # DCS: uncompressed size y compressed size son uint32 big-endian.
    header[0x1C:0x20] = struct.pack(">I", len(msb))
    header[0x20:0x24] = struct.pack(">I", len(compressed))
    # Reflejamos el nivel real usado por zlib en la cabecera DFLT.
    header[0x30] = level
    return bytes(header) + compressed


def parse_msb_payload(raw: bytes) -> ParsedMSB:
    msb = decompress_dcx(raw)
    sections = find_sections(msb)
    model_section = next((s for s in sections if s.name == "MODEL_PARAM_ST"), None)
    parts_section = next((s for s in sections if s.name == "PARTS_PARAM_ST"), None)
    if model_section is None or parts_section is None:
        missing = []
        if model_section is None:
            missing.append("MODEL_PARAM_ST")
        if parts_section is None:
            missing.append("PARTS_PARAM_ST")
        raise RandomizerError("MSB sin secciones requeridas: " + ", ".join(missing))
    models, model_to_indices = parse_models(msb, model_section)
    enemies = parse_enemy_parts(msb, parts_section)
    return ParsedMSB(msb, sections, models, model_to_indices, enemies)


# ============================================================================
# VALIDACIÓN JSON <-> MSB
# ============================================================================


def parse_hex_offset(value: Any) -> int | None:
    if isinstance(value, str) and value.lower().startswith("0x"):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None


def expected_names_by_map(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if rec.get("MSB_Part_Type") != PARTS_TYPE_ENEMY:
            continue
        if not isinstance(rec.get("Map_File"), str) or not isinstance(rec.get("Part_Name"), str):
            continue
        out.setdefault(relative_map(rec), []).append(rec)
    return out


def validate_map_structure(parsed: ParsedMSB, expected: list[dict[str, Any]], rel_map: str) -> dict[str, EnemyLocation]:
    actual: dict[str, EnemyLocation] = {}
    duplicates: list[str] = []
    for loc in parsed.enemies:
        if loc.part_name in actual:
            duplicates.append(loc.part_name)
        else:
            actual[loc.part_name] = loc

    expected_names = [r["Part_Name"] for r in expected]
    if len(expected_names) != len(set(expected_names)):
        raise RandomizerError(f"{rel_map}: Part_Name duplicado en JSON de referencia.")
    if duplicates:
        raise RandomizerError(f"{rel_map}: Part_Name duplicado en MSB: {duplicates[:10]}")

    missing = sorted(set(expected_names) - set(actual))
    extra = sorted(set(actual) - set(expected_names))
    if STRICT_STRUCTURE and (missing or extra or len(expected_names) != len(actual)):
        parts = []
        if missing:
            parts.append(f"faltantes={missing[:20]}")
        if extra:
            parts.append(f"extra={extra[:20]}")
        parts.append(f"json={len(expected_names)} msb={len(actual)}")
        raise RandomizerError(f"{rel_map}: Enemy Parts incompatibles; " + "; ".join(parts))

    by_name = {r["Part_Name"]: r for r in expected}
    for name, loc in actual.items():
        exp = by_name.get(name)
        if exp is None:
            if STRICT_STRUCTURE:
                raise RandomizerError(f"{rel_map}: Enemy Part inesperado: {name}")
            continue
        checks = {
            "MSB_File_Offset": loc.entry_offset,
            "Entity_Data_Offset": loc.entity_data_offset,
            "Type_Data_Offset": loc.type_data_offset,
        }
        for field, actual_value in checks.items():
            expected_value = parse_hex_offset(exp.get(field))
            if expected_value is None:
                raise RandomizerError(f"{rel_map}|{name}: JSON no contiene {field} hex válido.")
            if actual_value != expected_value:
                raise RandomizerError(
                    f"{rel_map}|{name}: {field} no coincide: MSB=0x{actual_value:X} JSON=0x{expected_value:X}"
                )
    return actual


def validate_models_for_plan(parsed: ParsedMSB, planned: dict[str, dict[str, Any]], rel_map: str) -> None:
    for part_name, rec in planned.items():
        model_name = rec.get("Model_Name")
        if not isinstance(model_name, str) or is_empty(model_name):
            raise RandomizerError(f"{rel_map}|{part_name}: donor sin Model_Name válido.")
        indices = parsed.model_to_indices.get(model_name, ())
        if not indices:
            raise RandomizerError(f"{rel_map}|{part_name}: modelo {model_name!r} no existe en MODEL_PARAM_ST.")


# ============================================================================
# MOD ENGINE 2 / SOURCES
# ============================================================================


def decode_toml_string(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace("\\\\", "\\").replace('\\"', '"')
    return value


def find_me2_config(game: Path) -> Path | None:
    candidates = [
        game / "ModEngine-2.1.0.0-win64" / "config_darksouls3.toml",
        game / "config_darksouls3.toml",
    ]
    for p in candidates:
        if p.is_file():
            return p
    for root in (game / "ModEngine-2.1.0.0-win64", game):
        if not root.is_dir():
            continue
        try:
            for p in root.glob("config_darksouls3.toml"):
                if p.is_file():
                    return p
        except OSError:
            pass
    return None


def configured_mods(game: Path) -> list[Path]:
    config = find_me2_config(game)
    if config is None:
        return []
    text = config.read_text(encoding="utf-8", errors="replace")
    pat = re.compile(r"\{\s*enabled\s*=\s*(true|false).*?path\s*=\s*\"([^\"]+)\"\s*\}", re.I | re.S)
    out: list[Path] = []
    for enabled, raw in pat.findall(text):
        if enabled.lower() != "true":
            continue
        p = Path(decode_toml_string(raw))
        if not p.is_absolute():
            p = config.parent / p
        out.append(norm(p))
    return out


def same_path(a: Path, b: Path) -> bool:
    return str(norm(a)).lower() == str(norm(b)).lower()


def choose_source(paths: Paths, rel_map: str, previous: dict[str, Any] | None) -> tuple[Path, str]:
    target = norm(paths.map_mod / Path(rel_map))

    # Si la ejecución anterior dejó intacta la salida, usar su baseline original.
    if previous and target.is_file():
        previous_hash = str(previous.get("output_sha256", "")).lower()
        baseline_rel = previous.get("baseline_backup")
        if previous_hash and sha256_file(target).lower() == previous_hash and isinstance(baseline_rel, str):
            baseline = norm(paths.package / baseline_rel)
            if baseline.is_file():
                baseline_hash = str(previous.get("baseline_sha256", "")).lower()
                if not baseline_hash or sha256_file(baseline).lower() == baseline_hash:
                    return baseline, "previous_run_baseline"

    # Preservar primero cualquier modificación que ya forme parte del mod destino.
    if target.is_file():
        return target, "target_mod_existing"

    mods = configured_mods(paths.game)
    target_index = next((i for i, p in enumerate(mods) if same_path(p, paths.mod)), None)
    candidates = mods[:target_index] if target_index is not None else mods
    for mod in reversed(candidates):
        candidate = norm(mod / "map" / "mapstudio" / Path(rel_map))
        if candidate.is_file():
            return candidate, "lower_priority_mod"

    vanilla = norm(paths.map_vanilla / Path(rel_map))
    if vanilla.is_file():
        return vanilla, "vanilla_game"

    # Variación no comprimida como último recurso de lectura, pero la salida
    # conserva el mismo formato de la fuente.
    if rel_map.lower().endswith(".msb.dcx"):
        alt = norm(paths.map_vanilla / Path(rel_map[:-4]))
        if alt.is_file():
            return alt, "vanilla_game_uncompressed"

    raise RandomizerError(f"No se encontró el MSB fuente para {rel_map}.")


# ============================================================================
# BACKUPS / MANIFEST
# ============================================================================


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 2, "maps": {}, "runs": []}
    data = load_json(path)
    if not isinstance(data, dict):
        raise RandomizerError("RandomizeEnemies_manifest.json no es un objeto.")
    data.setdefault("maps", {})
    data.setdefault("runs", [])
    return data


def backup_path(paths: Paths, run_id: str, kind: str, rel_map: str) -> Path:
    return norm(paths.backup_root / run_id / kind / "map" / "mapstudio" / Path(rel_map))


def copy_backup(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def rel_to_package(paths: Paths, p: Path) -> str:
    return str(norm(p).relative_to(paths.package)).replace("/", "\\")


# ============================================================================
# APPLY / WRITE
# ============================================================================


def apply_plan_to_map(
    raw_source: bytes,
    parsed: ParsedMSB,
    expected: list[dict[str, Any]],
    planned_by_part: dict[str, dict[str, Any]],
    rel_map: str,
) -> tuple[bytes, dict[str, Any]]:
    actual = validate_map_structure(parsed, expected, rel_map)
    validate_models_for_plan(parsed, planned_by_part, rel_map)
    msb = bytearray(parsed.msb)
    changes: list[dict[str, Any]] = []

    for exp in expected:
        part_name = exp["Part_Name"]
        loc = actual[part_name]
        target = planned_by_part.get(part_name)
        # Los Enemy Parts no elegibles (por ejemplo con TalkID activo o sin una
        # referencia resoluble) se conservan exactamente como estan en el MSB.
        if target is None:
            continue

        new_npc = target.get("NPCParamID")
        new_think = target.get("ThinkParamID")
        model_name = target.get("Model_Name")
        if not valid_int(new_npc) or not valid_int(new_think) or not isinstance(model_name, str):
            raise RandomizerError(f"{rel_map}|{part_name}: plan incompleto.")

        indices = parsed.model_to_indices.get(model_name, ())
        if not indices:
            raise RandomizerError(f"{rel_map}|{part_name}: modelo {model_name!r} no está en el MSB.")

        donor_model_index = target.get("Model_Index")
        new_model_index = donor_model_index if valid_int(donor_model_index) and donor_model_index in indices else indices[0]

        # ÚNICAS mutaciones permitidas.
        put_i32(msb, loc.entry_offset + 0x10, new_model_index)
        put_i32(msb, loc.type_data_offset + 0x08, new_think)
        put_i32(msb, loc.type_data_offset + 0x0C, new_npc)

        changes.append({
            "Part_Name": part_name,
            "MSB_File_Offset": f"0x{loc.entry_offset:X}",
            "Type_Data_Offset": f"0x{loc.type_data_offset:X}",
            "old_ModelIndex": loc.model_index,
            "new_ModelIndex": new_model_index,
            "new_Model_Name": model_name,
            "old_ThinkParamID": loc.think_param_id,
            "new_ThinkParamID": new_think,
            "old_NPCParamID": loc.npc_param_id,
            "new_NPCParamID": new_npc,
        })

    packed = compress_dcx_like(raw_source, bytes(msb))
    # Validación inmediata de integridad: descomprimir y volver a comprobar el
    # número/estructura de Enemy Parts antes de aceptar el buffer de salida.
    check = parse_msb_payload(packed)
    validate_map_structure(check, expected, rel_map)
    return packed, {
        "enemy_count": len(check.enemies),
        "change_count": len(changes),
        "changes": changes,
    }


# ============================================================================
# MAIN
# ============================================================================


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Randomiza enemigos de DS3 a partir de all_enemies_complete.json")
    p.add_argument("--game-root", default=None)
    p.add_argument("--mod-directory", default=None)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    paths = resolve_paths(args)
    apply = args.apply or (APPLY_BY_DEFAULT and not args.dry_run)
    if args.dry_run:
        apply = False
    seed = args.seed if args.seed is not None else int.from_bytes(os.urandom(8), "little") & 0x7FFFFFFF

    print("=" * 78)
    print("DS3 Enemy Randomizer v2")
    print("=" * 78)
    print(f"Game:        {paths.game}")
    print(f"Mod:         {paths.mod}")
    print(f"EnemyTables: {paths.enemy_tables}")
    print(f"Source JSON: {paths.source_json}")
    print(f"Mode:        {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Seed:        {seed}")
    print()

    if not paths.game.is_dir():
        raise RandomizerError(f"Game no existe: {paths.game}")
    if not paths.map_vanilla.is_dir():
        raise RandomizerError(f"No existe Game\\map\\mapstudio: {paths.map_vanilla}")
    if not paths.source_json.is_file():
        raise RandomizerError(
            f"No existe {SOURCE_JSON_FILENAME}. Ejecuta primero el pipeline de EnemyTables.\n"
            f"Ruta esperada: {paths.source_json}"
        )

    source_path_used, source_records, source_mode = load_source_json(paths)
    paths = Paths(paths.package, paths.game, paths.mod, paths.enemy_tables, paths.map_vanilla, paths.map_mod, source_path_used, paths.randomized_json, paths.report, paths.manifest, paths.backup_root)

    randomized, plan = make_plan(source_records, seed)
    expected_by_map = expected_names_by_map(source_records)
    planned_by_map: dict[str, dict[str, dict[str, Any]]] = {}
    for rec in randomized:
        ok, _ = eligible(rec)
        if ok:
            planned_by_map.setdefault(relative_map(rec), {})[rec["Part_Name"]] = rec

    print(f"Registros JSON:       {len(source_records)}")
    print(f"Mapas con enemigos:   {len(expected_by_map)}")
    print(f"Enemigos elegibles:   {plan['eligible_records']}")
    print(f"Enemigos randomizados:{plan['randomized_records']}")
    print()

    manifest = load_manifest(paths.manifest)
    previous_maps = manifest.get("maps", {})
    if not isinstance(previous_maps, dict):
        raise RandomizerError("Manifest maps inválido.")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{seed}"
    source_hash = sha256_file(paths.source_json)

    # ------------------------------------------------------------------------
    # FASE 1: RESOLVER + VALIDAR TODOS LOS MAPAS (NO ESCRIBE NADA)
    # ------------------------------------------------------------------------
    sources: dict[str, tuple[Path, str]] = {}
    prepared: dict[str, dict[str, Any]] = {}

    for rel_map in sorted(expected_by_map):
        prev = previous_maps.get(rel_map)
        source, source_kind = choose_source(paths, rel_map, prev if isinstance(prev, dict) else None)
        raw = source.read_bytes()
        parsed = parse_msb_payload(raw)
        validate_map_structure(parsed, expected_by_map[rel_map], rel_map)
        validate_models_for_plan(parsed, planned_by_map.get(rel_map, {}), rel_map)
        sources[rel_map] = (source, source_kind)
        packed, map_info = apply_plan_to_map(
            raw,
            parsed,
            expected_by_map[rel_map],
            planned_by_map.get(rel_map, {}),
            rel_map,
        )
        prepared[rel_map] = {
            "source": source,
            "source_kind": source_kind,
            "target": norm(paths.map_mod / Path(rel_map)),
            "packed": packed,
            "map_info": map_info,
            "target_existed": norm(paths.map_mod / Path(rel_map)).is_file(),
        }
        print(f"[VALIDADO] {rel_map} -> {map_info['change_count']} cambios")

    if not prepared:
        raise RandomizerError("No se encontraron mapas con Enemy Parts.")

    # ------------------------------------------------------------------------
    # FASE 2: COMMIT ATÓMICO
    # ------------------------------------------------------------------------
    rollback: dict[str, Path | None] = {}
    baselines: dict[str, Path] = {}
    written: list[str] = []

    if apply:
        paths.mod.mkdir(parents=True, exist_ok=True)
        paths.map_mod.mkdir(parents=True, exist_ok=True)
        paths.enemy_tables.mkdir(parents=True, exist_ok=True)
        paths.backup_root.mkdir(parents=True, exist_ok=True)

        try:
            # 1) Backup de archivos que ya existían en el mod.
            for rel_map, item in prepared.items():
                target = item["target"]
                if item["target_existed"]:
                    rb = backup_path(paths, run_id, "rollback", rel_map)
                    copy_backup(target, rb)
                    rollback[rel_map] = rb
                else:
                    rollback[rel_map] = None

            # 2) Baseline reproducible de cada fuente usada.
            for rel_map, item in prepared.items():
                base = backup_path(paths, run_id, "baseline", rel_map)
                copy_backup(item["source"], base)
                baselines[rel_map] = base

            # 3) Escribir TODOS los mapas, no solo los que ya estaban en el mod.
            for rel_map, item in prepared.items():
                target = item["target"]
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(target.suffix + ".tmp")
                tmp.write_bytes(item["packed"])
                tmp.replace(target)
                # Registrar inmediatamente el target: si la validación posterior
                # falla, rollback también debe restaurar este archivo.
                written.append(rel_map)
                # Comprobación de lectura desde el archivo ya escrito.
                check = parse_msb_payload(target.read_bytes())
                validate_map_structure(check, expected_by_map[rel_map], rel_map)
                item["output_sha256"] = sha256_file(target)

        except Exception:
            # Rollback global: ningún mapa queda parcialmente aplicado.
            for rel_map in reversed(written):
                target = prepared[rel_map]["target"]
                rb = rollback.get(rel_map)
                try:
                    if rb is not None and rb.is_file():
                        tmp = target.with_suffix(target.suffix + ".rollback.tmp")
                        shutil.copy2(rb, tmp)
                        tmp.replace(target)
                    elif target.exists():
                        target.unlink()
                except Exception as exc:
                    raise RandomizerError(f"Rollback falló en {rel_map}: {exc}") from exc
            raise

    else:
        for item in prepared.values():
            item["output_sha256"] = None

    # ------------------------------------------------------------------------
    # FASE 3: JSON + MANIFEST + REPORTE
    # ------------------------------------------------------------------------
    # Se escribe al final, una vez validado el commit completo (o el dry-run).
    write_json_atomic(paths.randomized_json, randomized)

    run = {
        "run_id": run_id,
        "timestamp_local": datetime.now().isoformat(timespec="seconds"),
        "seed": seed,
        "mode": "apply" if apply else "dry-run",
        "source_json": str(source_path_used),
        "source_mode": source_mode,
        "source_json_sha256": source_hash,
        "scope": RANDOMIZATION_SCOPE,
        "maps": {},
    }

    for rel_map, item in prepared.items():
        base = baselines.get(rel_map)
        entry = {
            "source_kind": item["source_kind"],
            "source_path": str(item["source"]),
            "source_sha256": sha256_file(item["source"]),
            "target_path": str(item["target"]),
            "target_existed": item["target_existed"],
            "output_sha256": item.get("output_sha256"),
            "baseline_backup": rel_to_package(paths, base) if base else None,
            "baseline_sha256": sha256_file(base) if base else None,
            "enemy_count": item["map_info"]["enemy_count"],
            "change_count": item["map_info"]["change_count"],
        }
        run["maps"][rel_map] = entry
        if apply:
            previous_maps[rel_map] = entry

    manifest["version"] = 2
    manifest["last_run_id"] = run_id
    manifest["source_json_sha256"] = source_hash
    manifest["maps"] = previous_maps
    manifest.setdefault("runs", []).append(run)
    manifest["runs"] = manifest["runs"][-20:]
    write_json_atomic(paths.manifest, manifest)

    report: list[str] = [
        "DS3 Enemy Randomizer v2",
        "=" * 78,
        f"Game: {paths.game}",
        f"Mod: {paths.mod}",
        f"EnemyTables: {paths.enemy_tables}",
        f"Source JSON: {source_path_used}",
        f"Source mode: {source_mode}",
        f"Source SHA256: {source_hash}",
        f"Mode: {'APPLY' if apply else 'DRY-RUN'}",
        f"Seed: {seed}",
        "",
        "PLAN",
        f"  input_records: {plan['input_records']}",
        f"  enemy_records: {plan['enemy_records']}",
        f"  map_count: {plan['map_count']}",
        f"  eligible_records: {plan['eligible_records']}",
        f"  randomized_records: {plan['randomized_records']}",
        "",
        "MAPS",
    ]
    for rel_map, item in sorted(prepared.items()):
        report.append(
            f"  {rel_map}: source={item['source_kind']} changes={item['map_info']['change_count']}"
        )
    report.extend([
        "",
        "SKIPPED",
    ])
    for k, v in sorted(plan["skipped"].items()):
        report.append(f"  {k}: {v}")
    report.extend([
        "",
        "WRITE POLICY",
        "  Output asset root: <MOD>\\map\\mapstudio",
        "  Only *.msb.dcx / *.msb maps containing enemies are processed.",
        "  Changed bytes per randomized Enemy Part: ModelIndex + ThinkParamID + NPCParamID.",
        "  Data0.bdt: untouched.",
        "  Data0.bhd: untouched.",
        "  Event: untouched.",
        "  Script/Talk: untouched.",
        "  Sfx: untouched.",
        "  Existing mod MSBs: used as baseline and backed up before replacement.",
        "",
        "SAFETY",
        "  All map structures are validated before any map is written.",
        "  Output is decompressed and reparsed after each write.",
        "  A failure triggers rollback of maps already committed in this run.",
    ])
    write_text_atomic(paths.report, "\n".join(report) + "\n")

    print()
    print("=" * 78)
    print("RESULTADO")
    print("=" * 78)
    print(f"Randomized_enemies.json: {paths.randomized_json}")
    print(f"Maps procesados:        {len(prepared)}")
    print(f"Maps escritos:          {len(written) if apply else 0}")
    print(f"Enemigos randomizados:  {plan['randomized_records']}")
    print(f"Reporte:                {paths.report}")
    print(f"Manifest:               {paths.manifest}")
    print("Proceso terminado.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RandomizerError as exc:
        print("\nERROR:")
        print(exc)
        raise SystemExit(2)
    except Exception as exc:
        print("\nERROR NO CONTROLADO:")
        print(repr(exc))
        raise SystemExit(3)
