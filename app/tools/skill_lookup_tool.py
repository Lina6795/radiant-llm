"""On-demand skill pack loader for the RADIANT-LLM agent.

The agent calls skill_lookup() via SkillLookupTool when it needs the full
content, scripts, and reference files of any skill pack — user-defined or
bundled — during a query turn.  Security scanning is cache-aware: a pack
already scanned by scan_user_skills_directory() costs nothing to look up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from utils import skill_core
from utils.radiant_skill_loader import resolve_bundled_skills_root
from utils.skill_scan_cache import content_hash, get_entry, load_cache, put_entry, save_cache
from utils.skill_security import run_tier_a

# Slug/alias → bundled folder name mapping. Add an entry here when a new
# bundled module is registered in the loader.
BUNDLED_MODULE_SLUGS: Dict[str, str] = {
    # Safety / PRA
    "safety":                               "safety-pra-severe-accident",
    "pra":                                  "safety-pra-severe-accident",
    "severe-accident":                      "safety-pra-severe-accident",
    "severe accident":                      "safety-pra-severe-accident",
    "probabilistic risk":                   "safety-pra-severe-accident",
    "safety-pra-severe-accident":           "safety-pra-severe-accident",

    # Security / cyber-physical
    "security":                             "security-cyber-physical-protection",
    "cyber":                                "security-cyber-physical-protection",
    "cybersecurity":                        "security-cyber-physical-protection",
    "cyber-physical":                       "security-cyber-physical-protection",
    "physical protection":                  "security-cyber-physical-protection",
    "insider threat":                       "security-cyber-physical-protection",
    "security-cyber-physical-protection":   "security-cyber-physical-protection",

    # Safeguards / MC&A
    "safeguards":                                      "safeguards-mca-and-fuel-cycle-monitoring",
    "mca":                                             "safeguards-mca-and-fuel-cycle-monitoring",
    "mc&a":                                            "safeguards-mca-and-fuel-cycle-monitoring",
    "material accountancy":                            "safeguards-mca-and-fuel-cycle-monitoring",
    "fuel-cycle":                                      "safeguards-mca-and-fuel-cycle-monitoring",
    "fuel cycle":                                      "safeguards-mca-and-fuel-cycle-monitoring",
    "safeguards-mca-and-fuel-cycle-monitoring":        "safeguards-mca-and-fuel-cycle-monitoring",

    # Gen IV / advanced reactors
    "geniv":                                "geniv-reactors-and-fuel-cycles",
    "gen-iv":                               "geniv-reactors-and-fuel-cycles",
    "gen iv":                               "geniv-reactors-and-fuel-cycles",
    "generation iv":                        "geniv-reactors-and-fuel-cycles",
    "advanced reactor":                     "geniv-reactors-and-fuel-cycles",
    "advanced reactors":                    "geniv-reactors-and-fuel-cycles",
    "smr":                                  "geniv-reactors-and-fuel-cycles",
    "microreactor":                         "geniv-reactors-and-fuel-cycles",
    "geniv-reactors-and-fuel-cycles":       "geniv-reactors-and-fuel-cycles",

    # Digital twin / monitoring
    "digital-twin":                              "digital-twin-monitoring-and-control",
    "digital twin":                              "digital-twin-monitoring-and-control",
    "monitoring":                                "digital-twin-monitoring-and-control",
    "anomaly-detection":                         "digital-twin-monitoring-and-control",
    "anomaly detection":                         "digital-twin-monitoring-and-control",
    "state estimation":                          "digital-twin-monitoring-and-control",
    "digital-twin-monitoring-and-control":       "digital-twin-monitoring-and-control",

    # Regulatory / licensing
    "regulatory":                                "regulatory-standards-and-licensing",
    "licensing":                                 "regulatory-standards-and-licensing",
    "standards":                                 "regulatory-standards-and-licensing",
    "nrc":                                       "regulatory-standards-and-licensing",
    "iaea":                                      "regulatory-standards-and-licensing",
    "compliance":                                "regulatory-standards-and-licensing",
    "regulatory-standards-and-licensing":        "regulatory-standards-and-licensing",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _read_skill(path: Path) -> tuple[str, str]:
    """Return (frontmatter_name, body). Body is the content after frontmatter."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            fm_text, body = parts[1], parts[2]
            name = ""
            for line in fm_text.splitlines():
                if line.lower().startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip("\"'")
                    break
            return name, body.strip()
    return "", raw


def _list_subdir(pack_dir: Path, subdir: str) -> List[str]:
    """Sorted absolute paths of files directly inside pack_dir/subdir."""
    d = pack_dir / subdir
    if not d.is_dir():
        return []
    return sorted(str(p.resolve()) for p in d.iterdir() if p.is_file())


def _fuzzy_match(query: str, candidates: List[str]) -> bool:
    """True if the normalised query is a substring of any normalised candidate."""
    def _norm(s: str) -> str:
        return s.lower().replace("-", " ").replace("_", " ")
    q = _norm(query)
    return any(q in _norm(c) for c in candidates)


def _format_output(pack_label: str, pack_dir: Path, body: str) -> str:
    scripts = _list_subdir(pack_dir, "scripts")
    refs    = _list_subdir(pack_dir, "reference")
    lines = [
        f"## Skill: {pack_label}",
        f"**Pack location:** {pack_dir.resolve()}",
        "",
        body.strip(),
    ]
    if scripts:
        lines += ["", "### scripts/ — run with PythonREPLTool"]
        lines += [f"- {p}" for p in scripts]
    if refs:
        lines += ["", "### reference/ — read with TextFileReaderTool"]
        lines += [f"- {p}" for p in refs]
    return "\n".join(lines)


# ─── Public function ───────────────────────────────────────────────────────────

def skill_lookup(
    pack_name: str,
    skills_directory: str,
    app_root: str,
    alert_sink: Optional[List] = None,
) -> str:
    """Retrieve the full content of a skill pack by name.

    Searches user-defined packs first, then bundled modules.  Returns the
    SKILL.md body with {PACK_ROOT} resolved to a real path, plus a listing
    of available scripts/ and reference/ files.
    """
    from utils.general_utilities import GeneralAlerts

    pack_name = (pack_name or "").strip()
    if not pack_name:
        return (
            "Error: pack_name is required. "
            "Check the Skill Packs Catalog for available names."
        )

    user_root   = Path(skills_directory).resolve() if skills_directory else None
    bundled_root = resolve_bundled_skills_root(app_root)
    available: List[str] = []

    # ── User-defined packs ────────────────────────────────────────────────────
    if user_root and user_root.exists():
        cache       = load_cache(user_root)
        cache_dirty = False

        for pack_dir in sorted(user_root.iterdir()):
            if not pack_dir.is_dir():
                continue
            skill_path = pack_dir / "SKILL.md"
            if not skill_path.is_file():
                continue

            fm_name, body = _read_skill(skill_path)
            label = fm_name or pack_dir.name
            available.append(label)

            if not _fuzzy_match(pack_name, [label, pack_dir.name]):
                continue

            # Matched — security scan (cache-aware)
            chash  = content_hash(body)
            cached = get_entry(cache, label, chash)

            if cached is not None:
                result_safe   = bool(cached.get("safe", False))
                result_reason = str(cached.get("reason", ""))
            else:
                tier_a = run_tier_a(body)
                result_safe   = tier_a is None
                result_reason = "" if tier_a is None else tier_a.reason
                put_entry(cache, label, chash, {
                    "safe":       result_safe,
                    "reason":     result_reason,
                    "confidence": 0.9 if result_safe else getattr(tier_a, "confidence", 1.0),
                    "tier":       "A",
                    "model_used": "",
                })
                cache_dirty = True

            if cache_dirty:
                save_cache(cache, user_root)

            if not result_safe:
                return (
                    f"[Skill blocked] '{label}' did not pass the security check: "
                    f"{result_reason}"
                )

            body = body.replace("{PACK_ROOT}", str(pack_dir.resolve()))
            if alert_sink is not None:
                GeneralAlerts(alert_sink, skill_core.consulting_skill_label(label), color="success")
            return _format_output(label, pack_dir, body)

        if cache_dirty:
            save_cache(cache, user_root)

    # ── Bundled modules ───────────────────────────────────────────────────────
    if bundled_root and bundled_root.exists():
        seen_paths: set[Path] = set()
        bundled_hits: List[tuple[Path, str]] = []

        slug_key    = pack_name.lower().replace(" ", "-")
        slug_folder = BUNDLED_MODULE_SLUGS.get(slug_key)
        if slug_folder:
            folder = bundled_root / slug_folder
            if folder.is_dir() and folder not in seen_paths:
                bundled_hits.append((folder, slug_folder))
                seen_paths.add(folder)

        for item in sorted(bundled_root.iterdir()):
            if not item.is_dir() or item in seen_paths:
                continue
            skill_path = item / "SKILL.md"
            if not skill_path.is_file():
                skill_path = item / "index.md"
            if not skill_path.is_file():
                continue
            fm_name, _ = _read_skill(skill_path)
            label = fm_name or item.name
            available.append(f"{label} (bundled)")
            if _fuzzy_match(pack_name, [label, item.name]):
                bundled_hits.append((item, item.name))
                seen_paths.add(item)

        for folder, slug in bundled_hits:
            skill_path = folder / "SKILL.md"
            if not skill_path.is_file():
                skill_path = folder / "index.md"
            if not skill_path.is_file():
                continue
            fm_name, body = _read_skill(skill_path)
            label = fm_name or slug
            body  = body.replace("{PACK_ROOT}", str(folder.resolve()))
            if alert_sink is not None:
                GeneralAlerts(alert_sink, skill_core.consulting_skill_label(label), color="info")
            return _format_output(label, folder, body)

    # ── No match ──────────────────────────────────────────────────────────────
    names = ", ".join(f"'{n}'" for n in available) if available else "none configured"
    return (
        f"No skill pack matching '{pack_name}' was found.\n"
        f"Available packs: {names}\n"
        "Check the Skill Packs Catalog or confirm the skills directory is set correctly in Settings."
    )
