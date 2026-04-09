"""Parse spec files with YAML frontmatter, compress, and validate."""

import re
from pathlib import Path

from .config import get_config

# Match YAML frontmatter delimited by ---
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def parse_frontmatter(spec_text):
    """
    Extract YAML frontmatter from a spec file.

    Returns (metadata_dict, body_text). If no frontmatter found, falls back
    to legacy parsing (## Target file header) for backwards compatibility.
    """
    import yaml

    match = _FRONTMATTER_RE.match(spec_text)
    if match:
        raw = match.group(1)
        meta = yaml.safe_load(raw) or {}
        body = spec_text[match.end():]
        return meta, body

    # Legacy fallback: parse ## Target file header
    meta = {}
    body = spec_text
    lines = spec_text.splitlines()
    for i, line in enumerate(lines):
        if "## Target file" in line and i + 1 < len(lines):
            meta["target"] = lines[i + 1].strip()
        if "## Test file" in line and i + 1 < len(lines):
            meta["test"] = lines[i + 1].strip()

    # Derive task/name from caller (not available here — set by load_spec)
    return meta, body


def parse_target_file(spec_text):
    """Extract target file path from spec (frontmatter or legacy header)."""
    meta, _ = parse_frontmatter(spec_text)
    return meta.get("target", "")


def load_spec(spec_file):
    """
    Load a spec file and return a structured dict.

    Returns:
        {
            "path": Path,
            "task_name": str,        # e.g., "task-001-models"
            "task_id": str,          # e.g., "001"
            "target": str,           # e.g., "src/models/user.py"
            "test": str,             # e.g., "tests/test_001_models.py"
            "dependencies": list,    # e.g., ["task-001-models"]
            "raw_text": str,         # full spec text
            "body": str,             # spec body (without frontmatter)
        }
    """
    spec_file = Path(spec_file)
    task_name = spec_file.stem
    raw_text = spec_file.read_text()
    meta, body = parse_frontmatter(raw_text)

    # Derive task_id from filename: task-001-name -> 001
    task_id = ""
    parts = task_name.split("-", 2)
    if len(parts) >= 2:
        task_id = parts[1]

    # Derive test file if not in frontmatter
    test = meta.get("test", "")
    if not test:
        slug = task_name.replace("task-", "", 1).replace("-", "_")
        test = f"tests/test_{slug}.py"

    return {
        "path": spec_file,
        "task_name": task_name,
        "task_id": meta.get("task", task_id),
        "target": meta.get("target", parse_target_file(raw_text)),
        "test": test,
        "dependencies": meta.get("dependencies") or [],
        "raw_text": raw_text,
        "body": body,
    }


def validate_specs(specs_dir):
    """
    Validate all spec files in the specs directory.

    Returns (errors, warnings) where each is a list of strings.
    """
    specs_dir = Path(specs_dir)
    errors = []
    warnings = []

    spec_files = sorted(specs_dir.glob("task-*.md"))
    if not spec_files:
        errors.append("No spec files found in specs/")
        return errors, warnings

    seen_targets = {}
    all_task_names = set()

    for sf in spec_files:
        spec = load_spec(sf)
        task_name = spec["task_name"]
        all_task_names.add(task_name)

        # Check target file is specified
        if not spec["target"]:
            errors.append(f"{task_name}: missing target file (add YAML frontmatter or ## Target file header)")

        # Check target is within src/
        if spec["target"] and not spec["target"].startswith("src/"):
            warnings.append(f"{task_name}: target '{spec['target']}' is outside src/")

        # Check for duplicate targets
        if spec["target"] in seen_targets:
            warnings.append(
                f"{task_name}: target '{spec['target']}' also used by {seen_targets[spec['target']]}"
            )
        if spec["target"]:
            seen_targets[spec["target"]] = task_name

        # Check test file exists
        test_path = Path(spec["test"])
        if not test_path.exists():
            errors.append(f"{task_name}: test file '{spec['test']}' does not exist")

        # Check token budget
        cfg = get_config()
        limits = cfg.get("spec_limits", {})
        soft = limits.get("soft_limit_chars", 12_000)
        hard = limits.get("hard_limit_chars", 16_000)

        if len(spec["raw_text"]) > hard:
            warnings.append(
                f"{task_name}: spec is {len(spec['raw_text'])} chars (hard limit {hard}) — "
                f"likely to overflow the model's effective context window; split this task"
            )
        elif len(spec["raw_text"]) > soft:
            warnings.append(
                f"{task_name}: spec is {len(spec['raw_text'])} chars (soft limit {soft}) — "
                f"approaching the context budget; consider splitting"
            )

    # Check dependencies reference valid tasks
    for sf in spec_files:
        spec = load_spec(sf)
        for dep in spec["dependencies"]:
            if dep not in all_task_names:
                errors.append(f"{spec['task_name']}: dependency '{dep}' not found in specs")

    # Check for dependency cycles
    cycle_errors = _check_cycles(spec_files)
    errors.extend(cycle_errors)

    return errors, warnings


def topological_sort(spec_files):
    """
    Sort spec files in dependency order using topological sort.

    Falls back to alphabetical order if no dependencies are declared.
    Raises ValueError if a cycle is detected.
    """
    specs = {load_spec(sf)["task_name"]: load_spec(sf) for sf in spec_files}

    # Build adjacency: task -> list of tasks it depends on
    graph = {name: spec["dependencies"] for name, spec in specs.items()}

    # Kahn's algorithm
    in_degree = {name: 0 for name in graph}
    dependents = {name: [] for name in graph}

    for name, deps in graph.items():
        for dep in deps:
            if dep in dependents:
                dependents[dep].append(name)
                in_degree[name] += 1

    queue = sorted([n for n, d in in_degree.items() if d == 0])
    result = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        for dependent in sorted(dependents.get(node, [])):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(result) != len(specs):
        missing = set(specs.keys()) - set(result)
        raise ValueError(f"Dependency cycle detected involving: {', '.join(sorted(missing))}")

    # Return the original Path objects in sorted order
    path_map = {load_spec(sf)["task_name"]: sf for sf in spec_files}
    return [path_map[name] for name in result]


def _check_cycles(spec_files):
    """Check for dependency cycles. Returns list of error strings."""
    try:
        topological_sort(spec_files)
        return []
    except ValueError as e:
        return [str(e)]
