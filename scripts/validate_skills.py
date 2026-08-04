from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from symphony_windows.skill import SkillError, validate_skill_package  # noqa: E402


EXPECTED_SKILLS = {
    "fskill-analysis-tech",
    "fskill-code-java-guide",
    "fskill-code-review",
    "fskill-knowledge-query",
    "fskill-test-explore",
    "fskill-test-verify",
    "fskill-tools-db",
}


def main() -> int:
    skills_root = ROOT / "skills"
    actual = {path.name for path in skills_root.iterdir() if path.is_dir()}
    if actual != EXPECTED_SKILLS:
        missing = sorted(EXPECTED_SKILLS - actual)
        unexpected = sorted(actual - EXPECTED_SKILLS)
        print(f"vendored Skill set mismatch; missing={missing}, unexpected={unexpected}")
        return 1
    try:
        results = [
            validate_skill_package(name, skills_root / name)
            for name in sorted(EXPECTED_SKILLS)
        ]
    except SkillError as error:
        print(f"vendored Skill validation failed: {error}")
        return 1
    print("vendored Skill validation passed")
    for result in results:
        print(
            f"  {result.name}: references={len(result.referenced_files)} "
            f"sha256={result.content_hash[:12]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
