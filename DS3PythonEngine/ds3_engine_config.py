# -*- coding: utf-8 -*-
r"""
DS3 Python Engine - shared path configuration
=============================================

La carpeta de este paquete debe estar en:

    DARK SOULS III\Game\PythonEngineDS3\

Todos los scripts parten automáticamente de ese punto para localizar Game.

CONFIGURACIÓN:
    - Deja los overrides vacíos para usar autodetección.
    - Si tienes varios mods y la autodetección es ambigua, configura
      MOD_DIRECTORY_OVERRIDE con la ruta absoluta del mod o
      MOD_DIRECTORY_RELATIVE con una ruta relativa a Game.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

# ============================================================================
# CONFIGURACIÓN DEL USUARIO
# ============================================================================

# Si se deja vacío, Game = carpeta padre de PythonEngineDS3.
GAME_ROOT_OVERRIDE = ""

# Opcional. Si se deja vacío, se detecta automáticamente.
# Ejemplo:
# MOD_DIRECTORY_OVERRIDE = r"D:\steam\steamapps\common\DARK SOULS III\Game\ModEngine-2.1.0.0-win64\mod\MiMod"
MOD_DIRECTORY_OVERRIDE = ""

# Opcional y preferido sobre la autodetección del nombre del mod.
# Se interpreta relativo a Game.
# Ejemplo:
# MOD_DIRECTORY_RELATIVE = r"ModEngine-2.1.0.0-win64\mod\MiMod"
MOD_DIRECTORY_RELATIVE = ""

# Nombre de mod preferido cuando existe la carpeta estándar ModEngine\mod
# y hay varios hijos. Es solo un fallback; no obliga a que el mod se llame así.
PREFERRED_MOD_NAMES = ("MiMod",)

# Rutas raíz estándar de la carpeta "mod" de Mod Engine.
MOD_ROOT_RELATIVES = (
    Path("ModEngine-2.1.0.0-win64") / "mod",
    Path("mod"),
)

# Nombres de archivos.
DATA0_BDT_FILENAME = "Data0.bdt"
DATA0_BHD_FILENAME = "Data0.bhd"

ENEMY_TABLES_FOLDER_NAME = "EnemyTables"
ALL_ENEMIES_FILENAME = "all_enemies.json"
ALL_ENEMIES_SOURCE_FILENAME = "all_enemies.source.json"
DATA0_JSON_FILENAME = "data0_original.json"
REFERENCE_INDEX_FILENAME = "ds3_reference_index.json"
FINAL_COMPLETE_FILENAME = "all_enemies_complete.json"
BUILDER_REPORT_FILENAME = "all_enemies_builder_report.txt"

# Seed opcional incluido en el ZIP.
SEED_RELATIVE = Path("all_enemies_seed.json")

# Si no existe Data0.bhd, el lector lo registra como advertencia pero no falla.
REQUIRE_DATA0_BHD = False

# Autodetección de EnemyTables ya existentes.
AUTO_FIND_EXISTING_ENEMY_TABLES = True


# ============================================================================
# RUTAS
# ============================================================================

@dataclass(frozen=True)
class EnginePaths:
    package_dir: Path
    game_root: Path
    data0_bdt: Path
    data0_bhd: Path
    mod_directory: Path
    enemy_tables: Path
    all_enemies: Path
    all_enemies_source: Path
    data0_json: Path
    reference_index: Path
    final_complete: Path
    builder_report: Path
    seed_all_enemies: Path


class ConfigError(RuntimeError):
    pass


def _norm(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except Exception:
        return path.expanduser()


def package_directory() -> Path:
    return _norm(Path(__file__).resolve().parent)


def discover_game_root(explicit: str | None = None, script_dir: Path | None = None) -> Path:
    candidates: list[Path] = []

    if explicit:
        candidates.append(Path(explicit))

    env = os.environ.get("DS3_GAME_ROOT", "").strip()
    if env:
        candidates.append(Path(env))

    if GAME_ROOT_OVERRIDE:
        candidates.append(Path(GAME_ROOT_OVERRIDE))

    base = _norm(script_dir or package_directory())
    # La convención pedida: PythonEngineDS3 está directamente dentro de Game.
    candidates.append(base.parent)

    # Último recurso útil para ejecutar desde esta carpeta.
    candidates.append(Path.cwd())

    seen: set[str] = set()
    for candidate in candidates:
        p = _norm(candidate)
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.is_dir() and (p / "map").is_dir():
            return p

    # Si existe el path indicado pero no hay map, lo devolvemos para que el
    # error siguiente sea explícito.
    if candidates:
        return _norm(candidates[0])

    raise ConfigError("No se pudo determinar la carpeta Game.")


def _existing_enemy_tables(game_root: Path) -> list[Path]:
    found: list[Path] = []

    if not AUTO_FIND_EXISTING_ENEMY_TABLES:
        return found

    # Buscamos solo dentro de Game para no tocar otras instalaciones.
    try:
        for p in game_root.rglob(ENEMY_TABLES_FOLDER_NAME):
            if p.is_dir():
                if (p / ALL_ENEMIES_FILENAME).is_file() or (p / DATA0_JSON_FILENAME).is_file():
                    found.append(_norm(p))
    except OSError:
        pass

    # Unicidad estable.
    unique: dict[str, Path] = {}
    for p in found:
        unique[str(p).lower()] = p
    return sorted(unique.values(), key=lambda x: str(x).lower())


def discover_mod_directory(game_root: Path, explicit: str | None = None) -> Path:
    # 1) Override CLI / ENV.
    if explicit:
        p = Path(explicit)
        return _norm(p if p.is_absolute() else game_root / p)

    env = os.environ.get("DS3_MOD_DIRECTORY", "").strip()
    if env:
        p = Path(env)
        return _norm(p if p.is_absolute() else game_root / p)

    # 2) Configuración absoluta.
    if MOD_DIRECTORY_OVERRIDE:
        p = Path(MOD_DIRECTORY_OVERRIDE)
        return _norm(p if p.is_absolute() else game_root / p)

    # 3) Configuración relativa exacta.
    if MOD_DIRECTORY_RELATIVE:
        return _norm(game_root / Path(MOD_DIRECTORY_RELATIVE))

    # 4) Si ya existe EnemyTables, reutilizar exactamente esa ubicación.
    existing_tables = _existing_enemy_tables(game_root)
    if len(existing_tables) == 1:
        return existing_tables[0].parent
    if len(existing_tables) > 1:
        # Si hay varias, preferimos una que contenga el nombre de mod configurado.
        for preferred in PREFERRED_MOD_NAMES:
            for tables in existing_tables:
                if tables.parent.name.lower() == preferred.lower():
                    return tables.parent
        raise ConfigError(
            "Se encontraron varias carpetas EnemyTables y la ruta del mod es "
            "ambigua. Configura MOD_DIRECTORY_OVERRIDE o MOD_DIRECTORY_RELATIVE "
            "en ds3_engine_config.py."
        )

    # 5) Auto-detección de la raíz mod estándar.
    for rel in MOD_ROOT_RELATIVES:
        mod_root = _norm(game_root / rel)
        if not mod_root.is_dir():
            continue

        # Nombre de mod preferido.
        for preferred in PREFERRED_MOD_NAMES:
            candidate = mod_root / preferred
            if candidate.is_dir():
                return _norm(candidate)

        # Si solo hay un hijo de primer nivel, es inequívoco.
        try:
            children = [
                p for p in mod_root.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ]
        except OSError:
            children = []

        if len(children) == 1:
            return _norm(children[0])

        # Si la propia raíz mod ya contiene EnemyTables, usarla.
        if (mod_root / ENEMY_TABLES_FOLDER_NAME).is_dir():
            return _norm(mod_root)

        # No elegimos arbitrariamente entre varios mods.

    raise ConfigError(
        "No se pudo determinar la carpeta del mod.\n"
        "Configura MOD_DIRECTORY_OVERRIDE o MOD_DIRECTORY_RELATIVE en "
        "PythonEngineDS3\\ds3_engine_config.py."
    )


def get_paths(
    game_root: str | None = None,
    mod_directory: str | None = None,
    script_dir: Path | None = None,
) -> EnginePaths:
    package = _norm(script_dir or package_directory())
    game = discover_game_root(game_root, package)
    mod = discover_mod_directory(game, mod_directory)
    tables = _norm(mod / ENEMY_TABLES_FOLDER_NAME)

    return EnginePaths(
        package_dir=package,
        game_root=game,
        data0_bdt=_norm(game / DATA0_BDT_FILENAME),
        data0_bhd=_norm(game / DATA0_BHD_FILENAME),
        mod_directory=mod,
        enemy_tables=tables,
        all_enemies=_norm(tables / ALL_ENEMIES_FILENAME),
        all_enemies_source=_norm(tables / ALL_ENEMIES_SOURCE_FILENAME),
        data0_json=_norm(tables / DATA0_JSON_FILENAME),
        reference_index=_norm(tables / REFERENCE_INDEX_FILENAME),
        final_complete=_norm(tables / FINAL_COMPLETE_FILENAME),
        builder_report=_norm(tables / BUILDER_REPORT_FILENAME),
        seed_all_enemies=_norm(package / SEED_RELATIVE),
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
