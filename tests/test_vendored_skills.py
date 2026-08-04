from pathlib import Path

from scripts.validate_skills import EXPECTED_SKILLS
from symphony_windows.skill import validate_skill_package


def test_vendored_profile_skills_are_compatible() -> None:
    skills_root = Path(__file__).resolve().parents[1] / "skills"
    actual = {path.name for path in skills_root.iterdir() if path.is_dir()}
    assert actual == EXPECTED_SKILLS
    results = [
        validate_skill_package(name, skills_root / name)
        for name in sorted(EXPECTED_SKILLS)
    ]
    assert all(len(result.content_hash) == 64 for result in results)
