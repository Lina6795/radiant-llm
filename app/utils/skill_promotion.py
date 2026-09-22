from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from utils.skill_security import run_tier_a

WORKING_EXAMPLE_ALLOWED_SUFFIXES = {".i", ".inp", ".yaml", ".yml", ".md", ".txt", ".csv", ".json"}
REUSABLE_RULE_FAMILIES = {
    "safety-pra-severe-accident/rules",
    "security-cyber-physical-protection/rules",
    "safeguards-mca-and-fuel-cycle-monitoring/rules",
    "geniv-reactors-and-fuel-cycles/rules",
    "digital-twin-monitoring-and-control/rules",
    "regulatory-standards-and-licensing/rules",
}
CHUNK_ORDER = ("scaffold", "populate", "document", "index")

PACK_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])?$")
RESERVED_PACK_SLUGS = frozenset(
    {
        "core",
        "safety-pra-severe-accident",
        "security-cyber-physical-protection",
        "safeguards-mca-and-fuel-cycle-monitoring",
        "geniv-reactors-and-fuel-cycles",
        "digital-twin-monitoring-and-control",
        "regulatory-standards-and-licensing",
        "working-examples",
        "working_examples",
        "user_skills",
        "index",
    }
)

STATUS_OK = "ok"
STATUS_DRY_RUN_OK = "dry_run_ok"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"
STATUS_BLOCKED = "blocked"
STATUS_BLOCKED_READONLY = "blocked_readonly"


def _skills_root(app_root: str | Path) -> Path:
    return Path(app_root).resolve() / "radiant_llm_skills"


def _chunk_mode(chunk: str | None) -> str:
    mode = (chunk or "all").strip().lower()
    if mode not in {"all", *CHUNK_ORDER}:
        raise ValueError(f"Unsupported chunk '{chunk}'. Use one of: all, {', '.join(CHUNK_ORDER)}.")
    return mode


def _should_run(step: str, chunk: str) -> bool:
    if chunk == "all":
        return True
    return step == chunk


def _next_step(last_step: str, chunk: str) -> Optional[str]:
    if chunk != "all":
        idx = CHUNK_ORDER.index(chunk)
        return CHUNK_ORDER[idx + 1] if idx + 1 < len(CHUNK_ORDER) else None
    idx = CHUNK_ORDER.index(last_step)
    return CHUNK_ORDER[idx + 1] if idx + 1 < len(CHUNK_ORDER) else None


def _build_result(
    *,
    status: str,
    operation: str,
    chunk_completed: Optional[str] = None,
    next_step: Optional[str] = None,
    created_paths: Optional[List[str]] = None,
    updated_indexes: Optional[List[str]] = None,
    rejected_paths: Optional[List[str]] = None,
    warnings: Optional[List[str]] = None,
    preview: Optional[Dict[str, Any]] = None,
    blocked_reason: Optional[str] = None,
    message: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": status,
        "operation": operation,
        "chunk_completed": chunk_completed,
        "next_step": next_step,
        "created_paths": list(created_paths or []),
        "updated_indexes": list(updated_indexes or []),
        "rejected_paths": list(rejected_paths or []),
        "warnings": list(warnings or []),
        "preview": preview or {},
        "blocked_reason": blocked_reason or "",
    }
    if message:
        result["message"] = message
    return result


def _validate_pack_slug(slug: str) -> Optional[str]:
    normalized = (slug or "").strip().lower()
    if not normalized:
        return "pack_slug is required."
    if normalized in RESERVED_PACK_SLUGS:
        return f"pack_slug '{normalized}' is reserved."
    if not PACK_SLUG_PATTERN.match(normalized):
        return (
            "pack_slug must be 3-40 characters, lowercase letters, digits, and hyphens only, "
            "and must start/end with a letter or digit."
        )
    return None


def _approval_blocked(user_approved: bool, dry_run: bool) -> bool:
    return not user_approved and not dry_run


def _is_writable(path: Path) -> bool:
    target = path.resolve()
    if not target.exists():
        parent = target.parent
        probe = parent if parent.exists() else target
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not probe.exists():
            return False
        target = probe
    if not os.access(target, os.W_OK):
        return False
    mode = target.stat().st_mode
    return bool(mode & stat.S_IWUSR)


def _resolve_user_skills_directory(skills_directory: str | Path | None) -> tuple[Optional[Path], Optional[str]]:
    if not skills_directory or not str(skills_directory).strip():
        return None, "skills_directory is not set. Configure it in Settings or via RADIANT_SKILLS_DIR."
    candidate = Path(skills_directory).expanduser().resolve(strict=False)
    if candidate.exists() and candidate.is_dir():
        return candidate, None
    return None, f"User-defined skills directory not found or not a directory: {candidate}"


def _scan_user_skill_content(body: str, label: str) -> tuple[bool, List[str]]:
    # run_tier_a returns None (passed) or ScanResult (blocked)
    result = run_tier_a(body)
    if result is not None:
        return False, [f"Blocked skill content in {label}: {result.reason}."]
    return True, []


def _normalize_paths(source_paths: Iterable[str]) -> List[Path]:
    normalized: List[Path] = []
    for source in source_paths:
        if not source:
            continue
        normalized.append(Path(source).expanduser().resolve())
    if not normalized:
        raise ValueError("At least one non-empty source path is required.")
    return normalized


def _collect_curated_files(source_paths: Iterable[Path]) -> tuple[List[Path], List[str]]:
    curated: List[Path] = []
    rejected: List[str] = []

    for source in source_paths:
        if not source.exists():
            rejected.append(f"{source} [missing]")
            continue

        files = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.is_file())

        for file_path in files:
            if file_path.suffix.lower() in WORKING_EXAMPLE_ALLOWED_SUFFIXES:
                curated.append(file_path)
            else:
                rejected.append(str(file_path))

    unique_curated: List[Path] = []
    seen: set[str] = set()
    for path in curated:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique_curated.append(path)

    return unique_curated, rejected


def _serialize_markdown_list(items: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in items if item)


def _write_text(path: Path, content: str, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _mkdir(path: Path, dry_run: bool) -> None:
    if dry_run:
        return
    path.mkdir(parents=True, exist_ok=True)


def _copy_file(src: Path, dst: Path, dry_run: bool) -> None:
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _insert_lines_before_header(path: Path, lines: List[str], header: str, dry_run: bool) -> bool:
    if not path.exists():
        return False

    content = path.read_text(encoding="utf-8")
    entry = "\n".join(lines).rstrip() + "\n"
    if entry.strip() in content:
        return False

    marker = f"\n{header}"
    updated = content.replace(marker, f"\n{entry}\n{header}", 1) if marker in content else content.rstrip() + "\n\n" + entry

    if not dry_run:
        path.write_text(updated, encoding="utf-8")
    return True


def _append_lines(path: Path, lines: List[str], dry_run: bool) -> bool:
    if not path.exists():
        return False

    content = path.read_text(encoding="utf-8")
    entry = "\n".join(lines).rstrip() + "\n"
    if entry.strip() in content:
        return False

    if not dry_run:
        path.write_text(content.rstrip() + "\n\n" + entry, encoding="utf-8")
    return True


def _working_example_index_lines(
    target_directory_name: str,
    primary_assets: List[str],
    summary: str,
) -> List[str]:
    lines = [f"- `{target_directory_name}/`"]
    lines += [f"  `{asset}`" for asset in primary_assets[:2]] if primary_assets else ["  `README.md`"]
    lines.append(f"  {summary}")
    return lines


def _top_level_index_lines(target_directory_name: str, summary: str) -> List[str]:
    return [f"- `working_examples/{target_directory_name}/` - {summary}"]


def _rule_family_index_path(skills_root: Path, target_family: str) -> Optional[Path]:
    module_slug = target_family.split("/")[0]
    index_path = skills_root / module_slug / "index.md"
    return index_path if index_path.parent.exists() else None


def create_user_skill_pack(
    pack_slug: str,
    skill_md_content: str,
    skills_directory: str | Path,
    *,
    user_approved: bool = False,
    dry_run: bool = True,
    allow_overwrite: bool = False,
    reference_files: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Create a new user skill pack at <skills_directory>/<pack_slug>/SKILL.md.

    Always call with dry_run=True first to preview. Only pass user_approved=True
    after the user has explicitly confirmed the shown proposal.
    """
    operation = "create_user_skill_pack"
    warnings: List[str] = []

    slug_error = _validate_pack_slug(pack_slug)
    if slug_error:
        return _build_result(status=STATUS_ERROR, operation=operation, message=slug_error)

    user_root, root_error = _resolve_user_skills_directory(skills_directory)
    if root_error or user_root is None:
        return _build_result(status=STATUS_ERROR, operation=operation, message=root_error or "skills_directory invalid.")

    if not (skill_md_content or "").strip():
        return _build_result(status=STATUS_ERROR, operation=operation, message="skill_md_content is required.")

    normalized_slug = pack_slug.strip().lower()
    pack_dir = user_root / normalized_slug
    skill_path = pack_dir / "SKILL.md"
    reference_files = dict(reference_files or {})
    preview_paths = [str(skill_path)] + [str(pack_dir / rp) for rp in sorted(reference_files)]

    if skill_path.exists():
        existing = skill_path.read_text(encoding="utf-8")
        if existing.strip() == skill_md_content.strip():
            return _build_result(
                status=STATUS_OK,
                operation=operation,
                created_paths=[str(skill_path)],
                warnings=["already_exists_identical"],
                preview={"paths": preview_paths, "pack_slug": normalized_slug},
            )
        if not allow_overwrite:
            return _build_result(
                status=STATUS_REJECTED,
                operation=operation,
                message=f"Skill pack already exists: {skill_path}. Set allow_overwrite=True after user approval.",
                preview={"paths": preview_paths, "pack_slug": normalized_slug},
            )

    is_safe, scan_warnings = _scan_user_skill_content(skill_md_content, normalized_slug)
    warnings.extend(scan_warnings)
    if not is_safe:
        return _build_result(
            status=STATUS_BLOCKED,
            operation=operation,
            warnings=warnings,
            blocked_reason="Skill content did not pass the security check.",
            preview={"paths": preview_paths, "pack_slug": normalized_slug},
        )

    if _approval_blocked(user_approved, dry_run):
        return _build_result(
            status=STATUS_REJECTED,
            operation=operation,
            message="Write rejected: set user_approved=True after explicit user approval, or use dry_run=True to preview.",
            preview={"paths": preview_paths, "pack_slug": normalized_slug, "skill_md_content": skill_md_content},
        )

    if dry_run:
        return _build_result(
            status=STATUS_DRY_RUN_OK,
            operation=operation,
            warnings=warnings,
            preview={"paths": preview_paths, "pack_slug": normalized_slug, "skill_md_content": skill_md_content},
        )

    if not _is_writable(user_root):
        return _build_result(
            status=STATUS_BLOCKED_READONLY,
            operation=operation,
            blocked_reason=f"User skills directory is not writable: {user_root}",
            preview={"paths": preview_paths, "pack_slug": normalized_slug},
        )

    created_paths: List[str] = []
    _mkdir(pack_dir, dry_run=False)
    created_paths.append(str(pack_dir))
    _write_text(skill_path, skill_md_content.strip() + "\n", dry_run=False)
    created_paths.append(str(skill_path))

    for rel_path, content in sorted(reference_files.items()):
        rel = Path(rel_path)
        if rel.is_absolute() or ".." in rel.parts:
            warnings.append(f"Skipped unsafe reference path: {rel_path}")
            continue
        ref_is_safe, ref_warnings = _scan_user_skill_content(content, rel_path)
        warnings.extend(ref_warnings)
        if not ref_is_safe:
            warnings.append(f"Skipped reference file that did not pass the security check: {rel_path}")
            continue
        ref_target = pack_dir / rel
        _write_text(ref_target, content.strip() + "\n", dry_run=False)
        created_paths.append(str(ref_target))

    if not skill_path.exists():
        return _build_result(
            status=STATUS_ERROR,
            operation=operation,
            message=f"Post-write verification failed: {skill_path}",
            warnings=warnings,
        )

    return _build_result(
        status=STATUS_OK,
        operation=operation,
        created_paths=created_paths,
        warnings=warnings,
        preview={"paths": preview_paths, "pack_slug": normalized_slug},
    )


def promote_working_example(
    source_paths: Iterable[str],
    target_directory_name: str,
    readme_title: str,
    readme_when_to_use: str,
    readme_demonstrates: Iterable[str],
    app_root: str | Path,
    readme_included_assets: Optional[Iterable[str]] = None,
    top_level_index_entry: bool = False,
    rename_map: Optional[Mapping[str, str]] = None,
    notes: Optional[Iterable[str]] = None,
    dry_run: bool = True,
    chunk: str = "all",
    user_approved: bool = False,
    allow_overwrite: bool = False,
) -> Dict[str, Any]:
    """Promote a curated reference bundle into radiant_llm_skills/working_examples/.

    Requires writable bundled skills tree. In Docker the bundled tree is typically
    read-only; use user skill packs for personal or team content instead.
    """
    operation = "promote_working_example"
    chunk_mode = _chunk_mode(chunk)
    skills_root = _skills_root(app_root)
    examples_root = skills_root / "working_examples"
    rename_map = dict(rename_map or {})
    notes_list = [n for n in (notes or []) if n]
    demonstrates = [item for item in readme_demonstrates if item]
    warnings: List[str] = []

    slug_error = _validate_pack_slug(target_directory_name)
    if slug_error:
        return _build_result(status=STATUS_ERROR, operation=operation, message=slug_error)

    if not readme_title.strip():
        return _build_result(status=STATUS_ERROR, operation=operation, message="readme_title is required.")
    if not readme_when_to_use.strip():
        return _build_result(status=STATUS_ERROR, operation=operation, message="readme_when_to_use is required.")
    if not demonstrates:
        return _build_result(status=STATUS_ERROR, operation=operation, message="At least one readme_demonstrates item is required.")

    if _approval_blocked(user_approved, dry_run):
        return _build_result(
            status=STATUS_REJECTED,
            operation=operation,
            message="Write rejected: set user_approved=True after explicit user approval, or use dry_run=True to preview.",
        )

    if not dry_run and not _is_writable(skills_root):
        return _build_result(
            status=STATUS_BLOCKED_READONLY,
            operation=operation,
            blocked_reason=f"Bundled skills directory is not writable: {skills_root}",
        )

    destination = examples_root / target_directory_name.strip().lower()
    readme_path = destination / "README.md"
    if readme_path.exists() and not allow_overwrite and not dry_run:
        return _build_result(
            status=STATUS_REJECTED,
            operation=operation,
            message=f"Working example already exists: {readme_path}. Set allow_overwrite=True after user approval.",
        )

    try:
        source_files, rejected_paths = _collect_curated_files(_normalize_paths(source_paths))
    except ValueError as exc:
        return _build_result(status=STATUS_ERROR, operation=operation, message=str(exc))

    preview = {
        "destination": str(destination),
        "readme_path": str(readme_path),
        "source_file_count": len(source_files),
    }

    if dry_run:
        return _build_result(
            status=STATUS_DRY_RUN_OK,
            operation=operation,
            chunk_completed="index",
            rejected_paths=rejected_paths,
            warnings=warnings,
            preview=preview,
        )

    copied_names: List[str] = []
    created_paths: List[str] = []
    updated_indexes: List[str] = []
    last_step = "scaffold"

    if _should_run("scaffold", chunk_mode):
        _mkdir(destination, dry_run)
        created_paths.append(str(destination))
        last_step = "scaffold"

    if _should_run("populate", chunk_mode):
        if not source_files:
            warnings.append("No curated companion files were accepted from source_paths.")
        for src in source_files:
            target_name = rename_map.get(src.name, src.name)
            dst = destination / target_name
            if dst.exists() and not allow_overwrite:
                return _build_result(
                    status=STATUS_REJECTED,
                    operation=operation,
                    message=f"Destination file already exists: {dst}. Set allow_overwrite=True after user approval.",
                    warnings=warnings,
                )
            _copy_file(src, dst, dry_run)
            copied_names.append(target_name)
            created_paths.append(str(dst))
        last_step = "populate"

    if _should_run("document", chunk_mode):
        included_assets = list(readme_included_assets or copied_names or ["README.md"])
        readme_lines = [
            f"# {readme_title}",
            "",
            readme_when_to_use.strip(),
            "",
            "## Included Assets",
            _serialize_markdown_list(f"`{asset}`" for asset in included_assets),
            "",
            "## What It Demonstrates",
            _serialize_markdown_list(demonstrates),
        ]
        if notes_list:
            readme_lines.extend(["", "## Notes", _serialize_markdown_list(notes_list)])
        _write_text(readme_path, "\n".join(readme_lines).rstrip() + "\n", dry_run)
        created_paths.append(str(readme_path))
        last_step = "document"

    if _should_run("index", chunk_mode):
        summary = readme_when_to_use.strip().splitlines()[0]
        primary_assets = [n for n in copied_names if n.endswith((".i", ".inp", ".yaml", ".yml"))]
        working_index = examples_root / "index.md"
        if _insert_lines_before_header(
            working_index,
            _working_example_index_lines(target_directory_name, primary_assets, summary),
            "## Retrieval Guidance",
            dry_run,
        ):
            updated_indexes.append(str(working_index))
        if top_level_index_entry:
            top_index = skills_root / "index.md"
            if _insert_lines_before_header(
                top_index,
                _top_level_index_lines(target_directory_name, summary),
                "## User skill packs (tier 2)",
                dry_run,
            ):
                updated_indexes.append(str(top_index))
        last_step = "index"

    return _build_result(
        status=STATUS_OK,
        operation=operation,
        chunk_completed=last_step,
        next_step=_next_step(last_step, chunk_mode),
        created_paths=created_paths,
        updated_indexes=updated_indexes,
        rejected_paths=rejected_paths,
        warnings=warnings,
        preview=preview,
    )


def promote_reusable_rule(
    target_family: str,
    target_file_name: str,
    source_material: str,
    summary: str,
    app_root: str | Path,
    index_entry: Optional[str] = None,
    evidence_paths: Optional[Iterable[str]] = None,
    dry_run: bool = True,
    chunk: str = "all",
    user_approved: bool = False,
    allow_overwrite: bool = False,
) -> Dict[str, Any]:
    """Add a reusable .md rule to a bundled module's rules/ subfolder.

    target_family must be one of REUSABLE_RULE_FAMILIES, e.g.
    'safety-pra-severe-accident/rules'.
    Requires writable bundled skills tree (not available in Docker :ro mounts).
    """
    operation = "promote_reusable_rule"
    chunk_mode = _chunk_mode(chunk)
    skills_root = _skills_root(app_root)
    warnings: List[str] = []

    if target_family not in REUSABLE_RULE_FAMILIES:
        return _build_result(
            status=STATUS_ERROR,
            operation=operation,
            message=(
                f"Unsupported target_family '{target_family}'. "
                f"Use one of: {', '.join(sorted(REUSABLE_RULE_FAMILIES))}."
            ),
        )
    if not target_file_name.strip():
        return _build_result(status=STATUS_ERROR, operation=operation, message="target_file_name is required.")
    if not source_material.strip():
        return _build_result(status=STATUS_ERROR, operation=operation, message="source_material is required.")
    if not summary.strip():
        return _build_result(status=STATUS_ERROR, operation=operation, message="summary is required.")

    if _approval_blocked(user_approved, dry_run):
        return _build_result(
            status=STATUS_REJECTED,
            operation=operation,
            message="Write rejected: set user_approved=True after explicit user approval, or use dry_run=True to preview.",
        )

    if not dry_run and not _is_writable(skills_root):
        return _build_result(
            status=STATUS_BLOCKED_READONLY,
            operation=operation,
            blocked_reason=f"Bundled skills directory is not writable: {skills_root}",
        )

    target_dir = skills_root / Path(target_family)
    target_path = target_dir / target_file_name
    if target_path.suffix.lower() != ".md":
        return _build_result(
            status=STATUS_ERROR,
            operation=operation,
            message="Reusable rules must use a .md file.",
        )

    evidence_list = [p for p in (evidence_paths or []) if p]
    document_lines = [source_material.strip()]
    if evidence_list:
        document_lines.extend(["", "## Supporting Evidence", _serialize_markdown_list(f"`{p}`" for p in evidence_list)])
    document_content = "\n".join(document_lines).rstrip() + "\n"

    if target_path.exists() and not allow_overwrite and not dry_run:
        existing = target_path.read_text(encoding="utf-8")
        if existing.strip() == document_content.rstrip():
            return _build_result(
                status=STATUS_OK,
                operation=operation,
                created_paths=[str(target_path)],
                warnings=["already_exists_identical"],
            )
        return _build_result(
            status=STATUS_REJECTED,
            operation=operation,
            message=f"Rule file already exists: {target_path}. Set allow_overwrite=True after user approval.",
        )

    preview = {"target_path": str(target_path), "target_family": target_family}

    if dry_run:
        return _build_result(
            status=STATUS_DRY_RUN_OK,
            operation=operation,
            chunk_completed="index",
            warnings=warnings,
            preview={**preview, "document_content": document_content},
        )

    created_paths: List[str] = []
    updated_indexes: List[str] = []
    last_step = "scaffold"

    if _should_run("scaffold", chunk_mode):
        _mkdir(target_dir, dry_run)
        created_paths.append(str(target_dir))
        last_step = "scaffold"

    if _should_run("populate", chunk_mode):
        _write_text(target_path, document_content, dry_run)
        created_paths.append(str(target_path))
        last_step = "populate"

    if _should_run("document", chunk_mode):
        last_step = "document"

    if _should_run("index", chunk_mode):
        index_path = _rule_family_index_path(skills_root, target_family)
        if index_path is not None:
            entry = index_entry or f"- `{target_file_name}` - {summary.strip()}"
            if _append_lines(index_path, [entry], dry_run):
                updated_indexes.append(str(index_path))
        else:
            warnings.append("No local index exists for this target family; no index was updated.")
        last_step = "index"

    return _build_result(
        status=STATUS_OK,
        operation=operation,
        chunk_completed=last_step,
        next_step=_next_step(last_step, chunk_mode),
        created_paths=created_paths,
        updated_indexes=updated_indexes,
        warnings=warnings,
        preview=preview,
    )


def run_skill_operation(
    operation: str,
    *,
    app_root: str | Path,
    skills_directory: str = "",
    user_approved: bool = False,
    dry_run: bool = True,
    source_paths: Optional[List[str]] = None,
    target_name: str = "",
    summary: str = "",
    readme_title: str = "",
    readme_when_to_use: str = "",
    readme_demonstrates: Optional[List[str]] = None,
    target_family: str = "",
    index_entry: Optional[str] = None,
    readme_included_assets: Optional[List[str]] = None,
    rename_map: Optional[Dict[str, str]] = None,
    notes: Optional[List[str]] = None,
    top_level_index_entry: bool = False,
    evidence_paths: Optional[List[str]] = None,
    source_material: str = "",
    pack_slug: str = "",
    skill_md_content: str = "",
    allow_overwrite: bool = False,
    reference_files: Optional[Dict[str, str]] = None,
    chunk: str = "all",
) -> Dict[str, Any]:
    """Unified dispatcher for skill promotion operations.

    Supported operations:
    - "create_user_skill_pack"  — write a user skill pack to skills_directory
    - "promote_working_example" — add a curated bundle to radiant_llm_skills/working_examples/
    - "promote_reusable_rule"   — add a .md rule to a bundled module's rules/ folder
    """
    normalized_operation = (operation or "").strip().lower()

    if normalized_operation == "create_user_skill_pack":
        return create_user_skill_pack(
            pack_slug=pack_slug or target_name,
            skill_md_content=skill_md_content,
            skills_directory=skills_directory,
            user_approved=user_approved,
            dry_run=dry_run,
            allow_overwrite=allow_overwrite,
            reference_files=reference_files,
        )

    if normalized_operation == "promote_working_example":
        return promote_working_example(
            source_paths=source_paths or [],
            target_directory_name=target_name,
            readme_title=readme_title,
            readme_when_to_use=readme_when_to_use,
            readme_demonstrates=readme_demonstrates or [],
            app_root=app_root,
            readme_included_assets=readme_included_assets,
            top_level_index_entry=top_level_index_entry,
            rename_map=rename_map,
            notes=notes,
            dry_run=dry_run,
            chunk=chunk,
            user_approved=user_approved,
            allow_overwrite=allow_overwrite,
        )

    if normalized_operation == "promote_reusable_rule":
        return promote_reusable_rule(
            target_family=target_family,
            target_file_name=target_name,
            source_material=source_material,
            summary=summary,
            app_root=app_root,
            index_entry=index_entry,
            evidence_paths=evidence_paths,
            dry_run=dry_run,
            chunk=chunk,
            user_approved=user_approved,
            allow_overwrite=allow_overwrite,
        )

    return _build_result(
        status=STATUS_ERROR,
        operation=normalized_operation or "unknown",
        message=(
            "Unsupported operation. Use 'create_user_skill_pack', "
            "'promote_working_example', or 'promote_reusable_rule'."
        ),
    )
