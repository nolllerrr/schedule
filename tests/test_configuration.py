from pathlib import Path


def test_example_environment_does_not_contain_secrets() -> None:
    values: dict[str, str] = {}
    for line in Path(".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value

    assert values["BOT_TOKEN"] == ""
    assert values["ADMIN_TELEGRAM_IDS"] == ""

